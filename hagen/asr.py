# -*- coding: utf-8 -*-
"""Распознавание речи моделями GigaAM.

Две модели под две задачи:
  v3_e2e_ctc   — живой эфир, запускается через onnxruntime (быстрый старт, мало памяти)
  v3_e2e_rnnt  — файлы и «перечитать точнее», запускается через torch,
                 потому что только этот путь отдаёт отметки времени по словам,
                 а они нужны, чтобы точно сшить текст с разметкой говорящих.

Обе модели «e2e»: сами ставят знаки препинания, заглавные буквы и нормализуют числа.
Аудио подаём массивом 16 кГц моно float32, без временных файлов.
"""
from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Any

import numpy as np

from . import config

log = logging.getLogger("hagen.asr")

SR = 16000
MAX_CHUNK_S = 24.0          # предел GigaAM: у неё жёсткие 25 с
MAX_CHUNK_EN_S = 120.0      # у Parakeet такого предела нет, но память не бесконечна
CKPT_DIR = config.MODELS_DIR / "gigaam"
ONNX_DIR = config.MODELS_DIR / "onnx"

#: Английская модель. GigaAM знает только русский (это написано в её карточке),
#: а многоязычная модель Сбера сама называет своё качество на английском
#: «умеренным»: 21-26 % ошибок против 6 % у Parakeet. Поэтому английский путь —
#: отдельная модель NVIDIA Parakeet TDT 0.6B v2 на том же onnxruntime, без torch.
EN_DIR = config.MODELS_DIR / "onnx-asr"
EN_MODEL = "nemo-parakeet-tdt-0.6b-v2"
#: Шаг сетки отметок времени: window_step 0,01 с × subsampling 8.
EN_FRAME_S = 0.08

_lock = threading.RLock()
# Замок на само распознавание. Модель и препроцессор общие для всех дорожек,
# а torch-препроцессор и sentencepiece не рассчитаны на одновременный вызов из
# нескольких потоков: это валит процесс в нативном коде без трассировки.
# Скорость позволяет: распознавание в 20 раз быстрее речи, две дорожки по очереди
# занимают около 10 % процессорного времени.
_infer_lock = threading.Lock()
_onnx_cache: dict[str, Any] = {}
_torch_cache: dict[str, Any] = {}
_en_cache: dict[str, Any] = {}
#: Когда точную модель трогали в последний раз, по имени. Сторож ниже выгружает
#: её после простоя: вместе с torch она занимает около 1,3 ГБ, а нужна не всегда.
_torch_used: dict[str, float] = {}
_janitor: threading.Thread | None = None
#: Как часто сторож просыпается. Реже минуты незачем: выгрузка мгновенная.
_JANITOR_STEP_S = 60.0


class Word:
    __slots__ = ("text", "start", "end")

    def __init__(self, text: str, start: float, end: float):
        self.text = text
        self.start = float(start)
        self.end = float(end)

    def as_dict(self) -> dict[str, Any]:
        return {"text": self.text, "start": round(self.start, 3), "end": round(self.end, 3)}


class Result:
    __slots__ = ("text", "words")

    def __init__(self, text: str, words: list[Word] | None = None):
        self.text = (text or "").strip()
        self.words = words or []

    def __bool__(self) -> bool:
        return bool(self.text)


def _threads() -> int:
    try:
        return max(1, min(12, int(config.get("onnx_threads") or 7)))
    except Exception:
        return 7


# ---------------------------------------------------------------- ONNX


def _onnx_session_options():
    import onnxruntime as rt

    opts = rt.SessionOptions()
    opts.graph_optimization_level = rt.GraphOptimizationLevel.ORT_ENABLE_ALL
    opts.intra_op_num_threads = _threads()
    opts.inter_op_num_threads = 1
    opts.execution_mode = rt.ExecutionMode.ORT_SEQUENTIAL
    opts.log_severity_level = 3
    return opts


def onnx_ready(name: str) -> bool:
    if not (ONNX_DIR / (name + ".yaml")).exists():
        return False
    if "rnnt" in name:
        need = ["_encoder.onnx", "_decoder.onnx", "_joint.onnx"]
        return all((ONNX_DIR / (name + s)).exists() for s in need)
    return (ONNX_DIR / (name + ".onnx")).exists()


_gigaam_clean = False


def _import_gigaam_clean() -> None:
    """Подключить gigaam.encoder заранее, в отдельном потоке с пустым стеком.

    Зачем. gigaam.encoder при подключении пробует flash_attn, не находит и
    сохраняет ошибку в глобальную IMPORT_FLASH_ERR — вместе со следом вызовов.
    Подключается модуль лениво, прямо внутри конструктора модели, поэтому в
    след попадал сам конструктор, а с ним и модель (self) и все вызвавшие кадры.
    Первая загруженная модель оставалась в памяти навсегда: выгрузка по простою
    ничего не освобождала, а повторная загрузка добавляла ещё 0,9 ГБ сверху
    (найдено 14.09 замером на настоящей модели). Из пустого потока в след
    попадают только служебные кадры самого потока.
    """
    global _gigaam_clean
    if _gigaam_clean:
        return

    def run() -> None:
        try:
            import gigaam.encoder  # noqa: F401
        except Exception as err:
            # Настоящую ошибку покажет сама загрузка модели — здесь только след.
            log.debug("gigaam.encoder заранее не подключился: %s", err)

    t = threading.Thread(target=run, name="asr-import", daemon=True)
    t.start()
    t.join()
    _gigaam_clean = True


def export_onnx(name: str, force: bool = False) -> bool:
    """Разовый экспорт модели в ONNX. Требует torch и скачанный чекпойнт."""
    if onnx_ready(name) and not force:
        return True
    import torch

    _import_gigaam_clean()
    import gigaam

    ONNX_DIR.mkdir(parents=True, exist_ok=True)
    CKPT_DIR.mkdir(parents=True, exist_ok=True)
    log.info("экспорт %s в ONNX, это разовая операция на несколько минут", name)
    torch.set_num_threads(_threads())
    model = gigaam.load_model(
        name, device="cpu", fp16_encoder=False, download_root=str(CKPT_DIR)
    )
    try:
        model.to_onnx(dir_path=str(ONNX_DIR))
    finally:
        del model
    return onnx_ready(name)


def _load_onnx(name: str):
    """Сессии onnxruntime + конфиг + препроцессор + токенизатор."""
    with _lock:
        got = _onnx_cache.get(name)
        if got is not None:
            return got
        if not onnx_ready(name):
            if not export_onnx(name):
                raise RuntimeError("не удалось подготовить ONNX для %s" % name)

        import hydra
        import omegaconf
        import onnxruntime as rt

        cfg = omegaconf.OmegaConf.load(str(ONNX_DIR / (name + ".yaml")))
        # GigaAM записывает в этот файл АБСОЛЮТНЫЙ путь к словарю — тот, что был
        # при экспорте. После переноса папки на другой компьютер путь перестаёт
        # существовать, ONNX молча не грузится, и эфир сваливается на torch —
        # втрое медленнее и незаметно. Поэтому путь всегда пересобираем от
        # текущего расположения проекта.
        try:
            tok = CKPT_DIR / (name + "_tokenizer.model")
            if tok.exists():
                cfg.decoding.model_path = str(tok)
        except Exception:
            log.warning("не удалось подставить путь к словарю для %s", name)
        opts = _onnx_session_options()
        providers = ["CPUExecutionProvider"]

        def sess(p: Path):
            return rt.InferenceSession(str(p), providers=providers, sess_options=opts)

        if "rnnt" in name:
            sessions = [
                sess(ONNX_DIR / (name + "_encoder.onnx")),
                sess(ONNX_DIR / (name + "_decoder.onnx")),
                sess(ONNX_DIR / (name + "_joint.onnx")),
            ]
        else:
            sessions = [sess(ONNX_DIR / (name + ".onnx"))]

        preprocessor = hydra.utils.instantiate(cfg.preprocessor)
        tokenizer = hydra.utils.instantiate(cfg.decoding).tokenizer
        got = {
            "sessions": sessions,
            "cfg": cfg,
            "preprocessor": preprocessor,
            "tokenizer": tokenizer,
        }
        _onnx_cache[name] = got
        log.info("ONNX %s загружен, потоков %d", name, _threads())
        return got


def _transcribe_onnx(pcm: np.ndarray, name: str) -> Result:
    from gigaam.onnx_utils import infer_onnx

    bundle = _load_onnx(name)
    arr = np.ascontiguousarray(np.asarray(pcm, dtype=np.float32).reshape(-1))
    with _infer_lock:
        texts = infer_onnx(
            [arr],
            bundle["cfg"],
            bundle["sessions"],
            preprocessor=bundle["preprocessor"],
            tokenizer=bundle["tokenizer"],
            batch_size=1,
            progress=False,
        )
    txt = texts[0] if texts else ""
    return Result(str(txt))


# ---------------------------------------------------------------- torch


def _load_torch(name: str):
    with _lock:
        got = _torch_cache.get(name)
        if got is not None:
            _torch_used[name] = time.time()
            return got
        import torch

        _import_gigaam_clean()
        import gigaam

        torch.set_num_threads(_threads())
        CKPT_DIR.mkdir(parents=True, exist_ok=True)
        t0 = time.time()
        model = gigaam.load_model(
            name, device="cpu", fp16_encoder=False, download_root=str(CKPT_DIR)
        )
        _torch_cache[name] = model
        _torch_used[name] = time.time()
        log.info("torch-модель %s загружена за %.1f c", name, time.time() - t0)
    _ensure_janitor()
    return model


def preload_precise() -> None:
    """Начать загрузку точной модели в фоне, если её ещё нет в памяти.

    Диктовка зовёт это в момент, когда человек начал говорить: пока он говорит
    свои несколько секунд, модель успевает загрузиться, и распознавание после
    «стоп» не ждёт. Загрузка берёт только _lock, не замок распознавания, так что
    ничему не мешает; повторный вызов во время загрузки просто подождёт её.
    """
    name = config.get("offline_model") or "v3_e2e_rnnt"
    with _lock:
        if name in _torch_cache:
            _torch_used[name] = time.time()
            return

    def run() -> None:
        try:
            _load_torch(name)
        except Exception as err:
            log.warning("фоновая загрузка точной модели не удалась: %s", err)

    threading.Thread(target=run, name="asr-preload", daemon=True).start()


def idle_minutes() -> float:
    """Через сколько минут простоя выгружать точную модель. 0 = не выгружать."""
    try:
        got = float(config.get("precise_idle_min") or 0)
    except (TypeError, ValueError):
        return 0.0
    return got if got > 0 else 0.0


def release_idle(minutes: float | None = None) -> list[str]:
    """Выгрузить точные модели, к которым давно не обращались.

    Возвращает имена выгруженного. Модель, на которой прямо сейчас идёт
    распознавание, не трогает: замок распознавания берётся без ожидания, и
    занятая модель просто доживёт до следующего круга. Ссылку, уже взятую
    чужим потоком, выгрузка не ломает — память освободится, когда тот поток
    закончит.
    """
    limit = idle_minutes() if minutes is None else float(minutes)
    if limit <= 0:
        return []
    dropped: list[str] = []
    with _lock:
        now = time.time()
        for name, used in list(_torch_used.items()):
            if now - used < limit * 60.0:
                continue
            if not _infer_lock.acquire(blocking=False):
                continue
            try:
                if _torch_cache.pop(name, None) is not None:
                    dropped.append(name)
                _torch_used.pop(name, None)
            finally:
                _infer_lock.release()
    if dropped:
        import gc

        gc.collect()
        log.info("точная модель выгружена из памяти после %g мин простоя: %s",
                 limit, ", ".join(dropped))
    return dropped


def _ensure_janitor() -> None:
    """Поднять сторож простоя. Поток, а не процесс: Kaspersky не даёт порождать
    фоновые процессы (см. грабли в отчёте)."""
    global _janitor
    with _lock:
        if _janitor is not None and _janitor.is_alive():
            return

        def loop() -> None:
            while True:
                time.sleep(_JANITOR_STEP_S)
                try:
                    release_idle()
                except Exception as err:      # сторож не должен ронять службу
                    log.warning("сторож простоя моделей: %s", err)

        _janitor = threading.Thread(target=loop, name="asr-idle", daemon=True)
        _janitor.start()


def _transcribe_torch(pcm: np.ndarray, name: str, word_timestamps: bool = True) -> Result:
    import torch

    model = _load_torch(name)
    arr = np.ascontiguousarray(np.asarray(pcm, dtype=np.float32).reshape(-1))
    wav = torch.from_numpy(arr).unsqueeze(0)
    length = torch.full([1], wav.shape[-1], dtype=torch.long)
    with _infer_lock, torch.inference_mode():
        enc, enc_len = model.forward(wav, length)
        text, words = model._decode(enc, enc_len, length, bool(word_timestamps))[0]
    out_words = []
    for w in words or []:
        out_words.append(Word(getattr(w, "text", ""), getattr(w, "start", 0.0), getattr(w, "end", 0.0)))
    return Result(str(text), out_words)


# ---------------------------------------------------------------- английский

#: Файлы весов, без которых английский путь не поедет. Имена именно такие, как
#: их отдаёт репозиторий istupakov/parakeet-tdt-0.6b-v2-onnx.
_EN_FILES = ("encoder-model.int8.onnx", "decoder_joint-model.int8.onnx",
             "nemo128.onnx", "vocab.txt")


def english_available() -> tuple[bool, str]:
    """Готов ли английский путь. Вторым значением — почему нет, человеческим языком."""
    try:
        import onnx_asr  # noqa: F401
    except Exception:
        return False, ("Английская модель не установлена. Нужен пакет onnx-asr: "
                       "скажите мне, и я его поставлю.")
    missing = [n for n in _EN_FILES if not (EN_DIR / n).exists()]
    if missing:
        return False, ("Файлы английской модели не скачаны (%d из %d). Папка: %s"
                       % (len(missing), len(_EN_FILES), EN_DIR))
    return True, "Готова: Parakeet TDT 0.6B, английский, на этом компьютере."


def _load_english():
    """Загрузить английскую модель. Держим одну на процесс, как и остальные."""
    with _lock:
        got = _en_cache.get(EN_MODEL)
        if got is not None:
            return got
        ok, why = english_available()
        if not ok:
            raise RuntimeError(why)
        import onnx_asr

        t0 = time.time()
        model = onnx_asr.load_model(
            EN_MODEL,
            path=str(EN_DIR),
            quantization="int8",
            sess_options=_onnx_session_options(),
            providers=["CPUExecutionProvider"],
        ).with_timestamps()
        _en_cache[EN_MODEL] = model
        log.info("английская модель %s загружена за %.1f c, потоков %d",
                 EN_MODEL, time.time() - t0, _threads())
        return model


def _words_from_tokens(tokens: list[str] | None, stamps: list[float] | None,
                       total_s: float) -> list[Word]:
    """Собрать слова из потокенных отметок времени.

    onnx-asr отдаёт отметки НЕ по словам, а по токенам модели, и слово почти
    всегда состоит из нескольких. Начало слова помечено пробелом в начале
    токена: сам пакет при чтении словаря заменяет им маркер SentencePiece «▁»
    (onnx_asr/asr.py, чтение vocab). Поэтому склеиваем так: токен с пробелом
    закрывает предыдущее слово и открывает новое, остальные дописываются.
    Конец слова — начало следующего; у последнего — плюс шаг сетки.
    """
    if not tokens or not stamps:
        return []
    out: list[Word] = []
    text = ""
    start = 0.0
    for token, stamp in zip(tokens, stamps):
        piece = str(token)
        if piece.startswith(" ") or not text:
            if text.strip():
                out.append(Word(text.strip(), start, float(stamp)))
            text = piece.lstrip()
            start = float(stamp)
        else:
            text += piece
    if text.strip():
        tail = min(float(total_s) if total_s else 0.0, float(stamps[-1]) + EN_FRAME_S)
        out.append(Word(text.strip(), start, max(start, tail)))
    return out


def _transcribe_english(pcm: np.ndarray) -> Result:
    model = _load_english()
    arr = np.ascontiguousarray(np.asarray(pcm, dtype=np.float32).reshape(-1))
    with _infer_lock:
        res = model.recognize(arr, sample_rate=SR)
    text = str(getattr(res, "text", "") or "")
    words = _words_from_tokens(getattr(res, "tokens", None),
                               getattr(res, "timestamps", None),
                               arr.shape[0] / float(SR))
    return Result(text, words)


def max_chunk_seconds(lang: str = "ru") -> float:
    """Предел длины куска. У русской и английской моделей он разный."""
    return MAX_CHUNK_EN_S if str(lang).lower() == "en" else MAX_CHUNK_S


# ---------------------------------------------------------------- публичный API


def transcribe_live(pcm: np.ndarray) -> Result:
    """Быстрое распознавание для эфира. Кусок не длиннее 24 с."""
    name = config.get("live_model") or "v3_e2e_ctc"
    arr = np.asarray(pcm, dtype=np.float32).reshape(-1)
    if arr.size < int(0.12 * SR):
        return Result("")
    arr = arr[: int(MAX_CHUNK_S * SR)]
    try:
        return _transcribe_onnx(arr, name)
    except Exception as err:
        log.warning("ONNX путь не сработал (%s), переключаюсь на torch", err)
        return _transcribe_torch(arr, name, word_timestamps=False)


def transcribe_precise(pcm: np.ndarray, words: bool = True,
                       lang: str = "ru") -> Result:
    """Точное распознавание для файлов и кнопки «перечитать точнее».

    lang="en" уводит в английскую модель. Автоопределения языка нет намеренно:
    в словаре английской модели нет кириллицы, и русская запись дала бы на
    выходе правдоподобный английский бред вместо честной ошибки. Язык выбирает
    человек в разделе «Видео»; живая запись с микрофона сюда не попадает и
    всегда идёт по русскому пути.
    """
    arr = np.asarray(pcm, dtype=np.float32).reshape(-1)
    if arr.size < int(0.12 * SR):
        return Result("")
    if str(lang).lower() == "en":
        return _transcribe_english(arr[: int(MAX_CHUNK_EN_S * SR)])
    name = config.get("offline_model") or "v3_e2e_rnnt"
    arr = arr[: int(MAX_CHUNK_S * SR)]
    if words:
        return _transcribe_torch(arr, name, word_timestamps=True)
    try:
        return _transcribe_onnx(arr, name)
    except Exception as err:
        log.warning("ONNX путь не сработал (%s), переключаюсь на torch", err)
        return _transcribe_torch(arr, name, word_timestamps=False)


def transcribe_spans(
    pcm: np.ndarray,
    spans: list[dict[str, float]],
    precise: bool = True,
    words: bool = True,
    progress=None,
    lang: str = "ru",
) -> list[dict[str, Any]]:
    """Распознать набор участков, вернуть реплики с абсолютным временем.

    spans — в отсчётах: [{"start": n, "end": n}, ...]
    lang="en" уводит участки в английскую модель, см. transcribe_precise.
    """
    arr = np.asarray(pcm, dtype=np.float32).reshape(-1)
    out: list[dict[str, Any]] = []
    total = max(1, len(spans))
    for i, sp in enumerate(spans):
        a, b = int(sp["start"]), int(sp["end"])
        a = max(0, a)
        b = min(arr.shape[0], b)
        if b - a < int(0.12 * SR):
            continue
        piece = arr[a:b]
        res = (transcribe_precise(piece, words=words, lang=lang) if precise
               else transcribe_live(piece))
        if not res.text:
            continue
        off = a / float(SR)
        out.append({
            "start": off,
            "end": b / float(SR),
            "text": res.text,
            "words": [
                {"text": w.text, "start": off + w.start, "end": off + w.end}
                for w in res.words
            ],
        })
        if progress is not None:
            try:
                progress((i + 1) / float(total))
            except Exception:
                pass
    return out


#: Чем кончился последний прогрев модели эфира: "loading" / "ready" / "error".
#: Раньше об этом знало только разовое событие в окно — окно, открытое ПОСЛЕ
#: прогрева (из трея, после переподключения), навсегда оставалось с надписью
#: «модель загружается…».
_live_state: dict[str, Any] = {"state": "loading", "error": "", "seconds": None}


def live_state() -> dict[str, Any]:
    """Состояние модели эфира: загружается, готова или не загрузилась и почему."""
    return dict(_live_state)


def warmup(live: bool = True, precise: bool = False) -> dict[str, Any]:
    """Прогреть модели, чтобы первая фраза не ждала загрузку."""
    info: dict[str, Any] = {}
    silence = np.zeros(int(0.5 * SR), dtype=np.float32)
    if live:
        t0 = time.time()
        _live_state.update({"state": "loading", "error": "", "seconds": None})
        try:
            transcribe_live(silence)
            info["live"] = round(time.time() - t0, 2)
            _live_state.update({"state": "ready", "error": "", "seconds": info["live"]})
        except Exception as err:
            info["live_error"] = str(err)
            _live_state.update({"state": "error", "error": str(err)[:300], "seconds": None})
    if precise:
        t0 = time.time()
        try:
            # words=True — тот же путь, которым точную модель зовут на деле
            # (файлы, «перечитать точнее», диктовка): torch. При words=False
            # прогрев ушёл бы в ONNX, которого для точной модели в папке нет, и
            # запуск программы встал бы на несколько минут разового экспорта.
            transcribe_precise(silence, words=True)
            info["precise"] = round(time.time() - t0, 2)
        except Exception as err:
            info["precise_error"] = str(err)
    return info
