# -*- coding: utf-8 -*-
"""Распознавание речи моделями GigaAM.

Две модели:
  v3_e2e_ctc   — быстрая, через onnxruntime (быстрый старт, мало памяти), без
                 отметок времени по словам;
  v3_e2e_rnnt  — точная, через torch или через onnx-asr (полные или сжатые
                 веса), с отметками времени по словам: по ним текст точно
                 сшивается с разметкой говорящих.

Какая модель чем занимается — звонки, голосовой ввод, файлы — выбирает человек
в «Настройки → Модели» (решение 21.09): одна модель на всё или две. Кто зовёт
transcribe_live и transcribe_precise, об этом не знает — это решает live_route.

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

    def words_at(self, offset: float) -> list[dict[str, Any]]:
        """Слова со временем от начала записи: offset — где в ней начался кусок."""
        return [{"text": w.text, "start": offset + w.start, "end": offset + w.end}
                for w in self.words]


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
    Грузится модель, выбранная для голосового ввода (live_route).
    """
    engine = live_route()["voice"]
    if engine == "fast":
        name = str(config.get("live_model") or "v3_e2e_ctc")
        with _lock:
            if name in _onnx_cache:
                return

        def load() -> Any:
            return _load_onnx(name)
    elif engine in OX_ENGINES:
        quant = OX_ENGINES[engine]
        with _lock:
            if quant in _ox_cache:
                return

        def load() -> Any:
            return _load_ox(quant)
    else:
        name = _precise_name()
        with _lock:
            if name in _torch_cache:
                _torch_used[name] = time.time()
                return

        def load() -> Any:
            return _load_torch(name)

    def run() -> None:
        try:
            load()
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

    Модель, которая служит эфиру (режим одной модели), не выгружается никогда:
    окно показывало бы «готова», а первая фраза записи ждала бы загрузку.
    """
    limit = idle_minutes() if minutes is None else float(minutes)
    if limit <= 0:
        return []
    keep = _live_keeps()
    dropped: list[str] = []
    with _lock:
        now = time.time()
        for name, used in list(_torch_used.items()):
            if name == keep or now - used < limit * 60.0:
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


def _transcribe_torch(pcm: np.ndarray, name: str, word_timestamps: bool = True,
                      threads: int = 0) -> Result:
    """threads — перед распознаванием поставить torch столько потоков; 0 — не трогать.

    Число потоков у torch одно на процесс, и его переставляют другие: silero-vad
    при подключении ставит один поток (silero_vad/model.py), разметка говорящих —
    свои. Без закрепления точная модель считала бы в один поток или в девять —
    смотря что случилось раньше (замер 18.09).
    """
    import torch

    model = _load_torch(name)
    arr = np.ascontiguousarray(np.asarray(pcm, dtype=np.float32).reshape(-1))
    wav = torch.from_numpy(arr).unsqueeze(0)
    length = torch.full([1], wav.shape[-1], dtype=torch.long)
    with _infer_lock, torch.inference_mode():
        if threads > 0 and torch.get_num_threads() != threads:
            torch.set_num_threads(threads)
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


def english_files() -> list[str]:
    """Файлы английской модели в EN_DIR — те, что кладёт скачивание."""
    return list(_EN_FILES) + ["config.json"]


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
                       total_s: float, frame_s: float = EN_FRAME_S,
                       tight: bool = False) -> list[Word]:
    """Собрать слова из потокенных отметок времени.

    onnx-asr отдаёт отметки НЕ по словам, а по токенам модели, и слово почти
    всегда состоит из нескольких. Начало слова помечено пробелом в начале
    токена: сам пакет при чтении словаря заменяет им маркер SentencePiece «▁»
    (onnx_asr/asr.py, чтение vocab). Поэтому склеиваем так: токен с пробелом
    закрывает предыдущее слово и открывает новое, остальные дописываются.
    Конец слова — начало следующего; у последнего — плюс шаг сетки frame_s.

    tight — конец слова не дальше его последнего токена плюс шаг сетки. Иначе
    слово перед паузой растягивается на всю паузу, его середина уезжает, и
    разметка отдаёт его не тому голосу: на записях 18.09 так было с 2,5 % слов.
    """
    if not tokens or not stamps:
        return []
    out: list[Word] = []
    text = ""
    start = 0.0
    last = 0.0
    for token, stamp in zip(tokens, stamps):
        piece = str(token)
        if piece.startswith(" ") or not text:
            if text.strip():
                end = min(float(stamp), last + frame_s) if tight else float(stamp)
                out.append(Word(text.strip(), start, max(start, end)))
            text = piece.lstrip()
            start = float(stamp)
        else:
            text += piece
        last = float(stamp)
    if text.strip():
        tail = min(float(total_s) if total_s else 0.0, float(stamps[-1]) + frame_s)
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


# ------------------------------------------------ точная модель через onnx-asr

#: Та же точная модель v3_e2e_rnnt, но в движке onnx-asr, а не torch. Замер на
#: записях владельца (18.09): fp32 даёт текст слово в слово как torch, int8 —
#: 98,1 % слов при трети памяти, а в эфире фраза в 24 с выходит за 0,8 с против
#: 4,9 с у torch (20.09). Файлы лежат рядом с английской моделью; в сборку
#: кладётся только рекомендованный вариант (recommended_part).
OX_DIR = EN_DIR / "gigaam-v3"
OX_MODEL = "gigaam-v3-e2e-rnnt"
OX_REPO = "istupakov/gigaam-v3-onnx"
#: Движок → сжатие весов. None — полные веса (fp32).
OX_ENGINES: dict[str, str | None] = {"ox_fp32": None, "ox_int8": "int8"}
#: Шаг сетки отметок времени GigaAM: окно 10 мс × прореживание 4.
GIGAAM_FRAME_S = 0.04
_ox_cache: dict[Any, Any] = {}


def ox_files(quant: str | None) -> list[str]:
    """Файлы, без которых точная модель в onnx-asr не поедет. Имена — как в репозитории."""
    suffix = (".%s" % quant) if quant else ""
    return ["config.json", "v3_e2e_rnnt_vocab.txt"] + [
        "v3_e2e_rnnt_%s%s.onnx" % (part, suffix) for part in ("encoder", "decoder", "joint")]


def ox_available(quant: str | None) -> tuple[bool, str]:
    """Готова ли точная модель в onnx-asr. Вторым значением — почему нет."""
    try:
        import onnx_asr  # noqa: F401
    except Exception:
        return False, "Нет пакета onnx-asr."
    missing = [n for n in ox_files(quant) if not (OX_DIR / n).exists()]
    if missing:
        return False, ("Файлы точной модели для onnx-asr не скачаны (%d из %d). Папка: %s"
                       % (len(missing), len(ox_files(quant)), OX_DIR))
    return True, "Готова."


def _load_ox(quant: str | None):
    with _lock:
        got = _ox_cache.get(quant)
        if got is not None:
            return got
        ok, why = ox_available(quant)
        if not ok:
            raise RuntimeError(why)
        import onnx_asr

        t0 = time.time()
        model = onnx_asr.load_model(
            OX_MODEL,
            path=str(OX_DIR),
            quantization=quant,
            sess_options=_onnx_session_options(),
            providers=["CPUExecutionProvider"],
        ).with_timestamps()
        _ox_cache[quant] = model
        log.info("точная модель через onnx-asr (%s) загружена за %.1f c, потоков %d",
                 quant or "fp32", time.time() - t0, _threads())
        return model


def _transcribe_ox(pcm: np.ndarray, quant: str | None) -> Result:
    model = _load_ox(quant)
    arr = np.ascontiguousarray(np.asarray(pcm, dtype=np.float32).reshape(-1))
    with _infer_lock:
        res = model.recognize(arr, sample_rate=SR)
    words = _words_from_tokens(getattr(res, "tokens", None), getattr(res, "timestamps", None),
                               arr.shape[0] / float(SR), frame_s=GIGAAM_FRAME_S, tight=True)
    return Result(str(getattr(res, "text", "") or ""), words)


def max_chunk_seconds(lang: str = "ru") -> float:
    """Предел длины куска. У русской и английской моделей он разный."""
    return MAX_CHUNK_EN_S if str(lang).lower() == "en" else MAX_CHUNK_S


# ---------------------------------------------------------------- выбор моделей

#: Движки распознавания и скачиваемая часть (needs.PARTS), без которой движок
#: не поедет:
#:   fast    — быстрая модель v3_e2e_ctc через ONNX из пакета gigaam;
#:   torch   — точная v3_e2e_rnnt через torch;
#:   ox_fp32 / ox_int8 — точная через onnx-asr, полные или сжатые веса.
ENGINE_PARTS = {"fast": "fast", "torch": "precise",
                "ox_fp32": "precise_ox_fp32", "ox_int8": "precise_ox_int8"}
ENGINE_TITLES = {"fast": "быстрая", "torch": "точная (torch)",
                 "ox_fp32": "точная (onnx-asr, полные веса)",
                 "ox_int8": "точная (onnx-asr, сжатые веса)"}
#: Роли: звонки и разговоры (эфир), голосовой ввод (диктовка, голосовые
#: заметки) и файлы (видео, файлы с диска, «Перечитать точнее»).
ROLES = ("live", "voice", "files")
ROLE_TITLES = {"live": "звонки", "voice": "голосовой ввод", "files": "файлы и видео"}

#: Что ставит «Сбросить всё», кладёт сборка для другого человека и советует
#: подсказка (решение 21.09): одна точная модель на onnx-asr с полными весами —
#: текст слово в слово как у torch (замер 18.09), а в памяти одна модель
#: вместо двух.
RECOMMENDED: dict[str, Any] = {"asr_count": 1, "asr_single": "precise",
                               "asr_engine": "onnx_asr", "asr_weights": "fp32",
                               "live_draft": False}

#: Как распознавание устроено в этом запуске программы. Решается один раз — при
#: прогреве или на первой фразе — и до перезапуска не меняется: иначе посреди
#: записи фразы пошли бы разными моделями, а в памяти оказались бы обе.
_live_route: dict[str, Any] | None = None
#: Свой замок, а не _lock: тот держится всю загрузку модели, и начало записи
#: ждало бы, пока грузится, например, модель для видео.
_route_lock = threading.Lock()


def _precise_name() -> str:
    return str(config.get("offline_model") or "v3_e2e_rnnt")


def _precise_engine(choice: dict[str, Any] | None = None) -> str:
    """Движок точной модели: torch или onnx-asr с нужными весами. choice — свой
    набор параметров вместо настроек."""
    get = choice.get if choice is not None else config.get
    if str(get("asr_engine") or "onnx_asr") == "torch":
        return "torch"
    return "ox_int8" if str(get("asr_weights") or "fp32") == "int8" else "ox_fp32"


def recommended_part() -> str:
    """Часть, которую ставит «Сбросить всё» и кладёт сборка для другого человека."""
    return ENGINE_PARTS[_precise_engine(RECOMMENDED)]


def chosen() -> dict[str, Any]:
    """Что выбрано в настройках: движок на каждую роль, черновик и перечитка.

    Одна модель — все роли на ней. Две — звонки и голосовой ввод выбираются
    отдельно, а файлы всегда идут точной: им важнее качество и время слов, чем
    скорость. «Перечитать точнее» есть, только когда звонки идут быстрой, —
    перечитывать той же моделью незачем. Черновик (бегущий текст фразы) — только
    у быстрой модели: точной он не по силам.
    """
    precise = _precise_engine()

    def pick(value: Any) -> str:
        return precise if str(value) == "precise" else "fast"

    if int(config.get("asr_count") or 1) == 1:
        engine = pick(config.get("asr_single") or "precise")
        out = {"live": engine, "voice": engine, "files": engine, "reread": False}
    else:
        live = pick(config.get("asr_calls") or "fast")
        out = {"live": live, "voice": pick(config.get("asr_voice") or "precise"),
               "files": precise, "reread": live == "fast" and bool(config.get("asr_reread"))}
    out["drafts"] = out["live"] == "fast" and bool(config.get("live_draft"))
    return out


def _fast_ready() -> bool:
    name = str(config.get("live_model") or "v3_e2e_ctc")
    return onnx_ready(name) and (CKPT_DIR / (name + "_tokenizer.model")).exists()


def engine_ready(engine: str) -> tuple[bool, str]:
    """На месте ли файлы движка. Вторым значением — почему нет."""
    if engine in OX_ENGINES:
        return ox_available(OX_ENGINES[engine])
    if engine == "torch":
        from . import needs

        if not needs.precise_ready():
            return False, "точная модель не скачана"
    if engine == "fast" and not _fast_ready():
        return False, "быстрая модель не скачана"
    return True, ""


def needed_parts(want: dict[str, Any] | None = None) -> list[str]:
    """Какие скачиваемые части нужны выбору (по умолчанию — тому, что в настройках)."""
    want = want or chosen()
    return sorted({ENGINE_PARTS[want[role]] for role in ROLES})


def running_parts() -> list[str]:
    """Какие части держит работающая сейчас программа: их удалять нельзя."""
    route = _live_route
    if route is None:
        return []
    return sorted({ENGINE_PARTS[route[role]] for role in ROLES})


def describe(route: dict[str, Any]) -> str:
    """Выбор или работающий маршрут словами — для окна и сводки записи."""
    if route["live"] == route["voice"] == route["files"]:
        return "Одна модель: " + ENGINE_TITLES[route["live"]]
    return "Звонки — %s; голосовой ввод — %s; файлы — %s" % tuple(
        ENGINE_TITLES[route[role]] for role in ROLES)


def choice_key(route: dict[str, Any]) -> str:
    """Чем идёт каждая роль — одной строкой: так сравнивается выбор с работающим."""
    return ",".join("%s=%s" % (role, route[role]) for role in ROLES)


def _fallback(engine: str) -> str:
    """Чем заменить движок, у которого нет файлов: первым готовым из очереди."""
    order = (["fast", "torch", "ox_int8", "ox_fp32"] if engine != "fast"
             else [_precise_engine(), "torch", "ox_int8", "ox_fp32"])
    for alt in order:
        if alt != engine and engine_ready(alt)[0]:
            return alt
    return "fast"


def _pick_live_route() -> dict[str, Any]:
    """Решить, как пойдёт распознавание в этом запуске.

    Нет файлов нужного движка — роль берёт первый готовый, а причина видна в
    окне (live_state) и в журнале: запись не должна ломаться ни при каких
    условиях. Точная модель в torch считает в заданное число потоков: без этого
    число потоков — какое оставили другие (silero-vad ставит один, разметка —
    свои), и одна и та же фраза считалась бы то секунду, то пять (замер 18.09).
    """
    want = chosen()
    route = dict(want)
    notes = []
    for role in ROLES:
        ok, why = engine_ready(want[role])
        if ok:
            continue
        alt = _fallback(want[role])
        route[role] = alt
        notes.append("%s: %s, пока идут моделью «%s»" % (ROLE_TITLES[role], why,
                                                        ENGINE_TITLES[alt]))
    # Галочка видна, только когда быстрая выбрана для звонков. Если быстрая
    # лишь подменяет недостающую точную, галочка не в силе.
    route["drafts"] = bool(want["drafts"]) and route["live"] == "fast"
    route["reread"] = bool(want["reread"]) and route["files"] != route["live"]
    route["threads"] = _threads()
    route["title"] = describe(route)
    route["key"] = choice_key(route)
    route["why"] = ""
    if notes:
        route["why"] = ("Не всё скачано — " + "; ".join(notes)
                        + ". Скачать: «Настройки → Модели».")
        log.warning("%s", route["why"])
    log.info("распознавание: %s", route["title"])
    return route


def live_route() -> dict[str, Any]:
    """Как идёт распознавание в этом запуске.

    live, voice, files — движки звонков, голосового ввода и файлов; drafts —
    черновик в эфире; reread — есть ли «Перечитать точнее»; threads — потоки
    torch; why — почему работает не то, что выбрано.
    """
    global _live_route
    with _route_lock:
        if _live_route is None:
            _live_route = _pick_live_route()
        return dict(_live_route)


def _live_fallback(err: Exception) -> None:
    """Точная модель в эфире отказала: до перезапуска эфир идёт быстрой.

    Лучше фраза быстрой моделью, чем потерянная фраза. Отказ запоминается,
    чтобы не пробовать точную заново на каждой фразе.
    """
    global _live_route
    why = "Точная модель в эфире не сработала (%s), эфир идёт на быстрой." % str(err)[:200]
    log.error("%s", why, exc_info=True)
    with _route_lock:
        route = dict(_live_route or _pick_live_route())
        route.update({"live": "fast", "drafts": False, "why": why})
        _live_route = route


def _live_keeps() -> str | None:
    """Имя torch-модели, которая служит эфиру: её сторож простоя не трогает."""
    route = _live_route
    if route and route.get("live") == "torch":
        return _precise_name()
    return None


def part_for(role: str) -> str:
    """Какая скачиваемая часть нужна роли в этом запуске."""
    return ENGINE_PARTS[live_route()[role]]


def precise_part() -> str:
    """Часть, нужная файлам, видео и «Перечитать точнее»."""
    return part_for("files")


def prepare_all() -> list[tuple[str, bool]]:
    """Подготовить заранее всё, что нужно выбору в настройках (run.py --prepare).

    Каждая часть качается тем же путём, что и кнопка «Скачать нужное»: быстрая
    — скачивается и переводится в ONNX, точная — только скачивается.
    """
    from . import needs

    out = []
    for key in needed_parts():
        try:
            ok = bool(needs.install(key).get("ready"))
        except Exception as err:
            log.warning("не подготовлено «%s»: %s", key, err)
            ok = False
        out.append((needs.PARTS[key]["title"], ok))
    return out


def _run(engine: str, arr: np.ndarray, words: bool = True) -> Result:
    """Распознать кусок выбранным движком. Время слов дают все, кроме быстрой."""
    if engine in OX_ENGINES:
        return _transcribe_ox(arr, OX_ENGINES[engine])
    if engine == "torch":
        return _transcribe_torch(arr, _precise_name(), word_timestamps=words,
                                 threads=_threads())
    name = config.get("live_model") or "v3_e2e_ctc"
    try:
        return _transcribe_onnx(arr, name)
    except Exception as err:
        # Запасной путь через torch — только если веса быстрой модели лежат на
        # диске: иначе gigaam молча качал бы 420 МБ посреди звонка.
        if not (CKPT_DIR / (name + ".ckpt")).exists():
            raise
        log.warning("ONNX путь не сработал (%s), переключаюсь на torch", err)
        return _transcribe_torch(arr, name, word_timestamps=False)


# ---------------------------------------------------------------- публичный API


def transcribe_live(pcm: np.ndarray) -> Result:
    """Распознавание для эфира. Кусок не длиннее 24 с.

    Каким движком — решает live_route: быстрая модель без времени слов или
    точная со временем слов (тот же экземпляр, что у файлов и диктовки, если
    роли выбраны одинаково). Вызывающему всё равно, какая модель внутри.
    """
    arr = np.asarray(pcm, dtype=np.float32).reshape(-1)
    if arr.size < int(0.12 * SR):
        return Result("")
    arr = arr[: int(MAX_CHUNK_S * SR)]
    engine = live_route()["live"]
    if engine != "fast":
        try:
            return _run(engine, arr)
        except Exception as err:
            _live_fallback(err)
    return _run("fast", arr)


def transcribe_precise(pcm: np.ndarray, words: bool = True,
                       lang: str = "ru", role: str = "files") -> Result:
    """Распознавание файлов, «перечитать точнее» (role="files") и голосового
    ввода (role="voice") — моделью, выбранной для этой роли.

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
    arr = arr[: int(MAX_CHUNK_S * SR)]
    engine = live_route()["voice" if role == "voice" else "files"]
    return _run(engine, arr, words=words)


def _center(word: dict[str, Any]) -> float:
    return (float(word["start"]) + float(word["end"])) / 2.0


def stitch_point(words: list[dict[str, Any]], joint: float, overlap: float) -> float:
    """Где кончается кусок, разрезанный без паузы в момент joint (секунды).

    Следующий кусок начат на overlap раньше разреза, так что последние overlap
    секунд этого куска распознаны дважды. Точку сшивки ищем в промежутке между
    словами поближе к середине нахлёста: там у обоих кусков есть звук с обеих
    сторон. Слово, которое задел разрез, в этот кусок не попадает никогда — его
    целиком возьмёт следующий. Промежутка нет — середина нахлёста.
    """
    target = joint - overlap / 2.0
    lo, hi = joint - overlap + 0.2, joint - 0.3
    best: float | None = None
    for a, b in zip(words, words[1:]):
        gap = (float(a["end"]) + float(b["start"])) / 2.0
        if lo <= gap <= hi and (best is None or abs(gap - target) < abs(best - target)):
            best = gap
    return best if best is not None else target


def words_before(words: list[dict[str, Any]], t: float) -> list[dict[str, Any]]:
    """Слова, чья середина раньше точки сшивки t, — они остаются первому куску."""
    return [w for w in words if _center(w) < t]


def words_after(words: list[dict[str, Any]], t: float) -> list[dict[str, Any]]:
    """Слова, чья середина не раньше точки сшивки t, — они достаются второму куску."""
    return [w for w in words if _center(w) >= t]


def words_text(words: list[dict[str, Any]]) -> str:
    """Текст из слов. Знаки препинания и заглавные у модели внутри слов, так что
    текст куска, собранный из его слов, совпадает с тем, что дала модель."""
    return " ".join(str(w.get("text") or "").strip() for w in words if w.get("text")).strip()


def transcribe_spans(
    pcm: np.ndarray,
    spans: list[dict[str, float]],
    precise: bool = True,
    words: bool = True,
    progress=None,
    lang: str = "ru",
    role: str = "files",
) -> list[dict[str, Any]]:
    """Распознать набор участков, вернуть реплики с абсолютным временем.

    spans — в отсчётах: [{"start": n, "end": n}, ...]
    lang="en" уводит участки в английскую модель, см. transcribe_precise.
    role — чья модель: файлов («files») или голосового ввода («voice»).

    Разрез без паузы (vad.mark_joints): у куска перед разрезом есть "cut", у
    следующего — "joint", и он начат раньше разреза. Такие куски сшиваются по
    времени слов: первому остаются слова до точки сшивки (stitch_point),
    второму — после неё, и слово на стыке выходит целым и ровно один раз. Нет
    времени слов (быстрая модель) — сшить нечем: второй кусок тогда
    распознаётся с самого разреза, без нахлёста, как раньше.
    """
    arr = np.asarray(pcm, dtype=np.float32).reshape(-1)
    out: list[dict[str, Any]] = []
    total = max(1, len(spans))
    keep_from: float | None = None      # точка сшивки с предыдущим куском, секунды
    prev_words = True                   # были ли слова у предыдущего куска
    for i, sp in enumerate(spans):
        a, b = int(sp["start"]), int(sp["end"])
        joint = sp.get("joint")
        if joint is not None and not prev_words:
            a = int(joint)              # сшить нечем — без нахлёста
        a = max(0, a)
        b = min(arr.shape[0], b)
        if b - a < int(0.12 * SR):
            keep_from, prev_words = None, True
            continue
        piece = arr[a:b]
        res = (transcribe_precise(piece, words=words, lang=lang, role=role) if precise
               else transcribe_live(piece))
        off = a / float(SR)
        text = res.text
        got = res.words_at(off)
        start, end = off, b / float(SR)
        if keep_from is not None and got:
            got = words_after(got, keep_from)
            text = words_text(got)
            start = max(off, keep_from)
        keep_from = None
        prev_words = bool(res.words) or not res.text
        cut = sp.get("cut")
        if cut is not None and got and i + 1 < len(spans):
            overlap = (int(cut) - int(spans[i + 1]["start"])) / float(SR)
            if overlap > 0:
                keep_from = stitch_point(got, int(cut) / float(SR), overlap)
                got = words_before(got, keep_from)
                text = words_text(got)
                end = keep_from
        if progress is not None:
            try:
                progress((i + 1) / float(total))
            except Exception:
                pass
        if not text:
            continue
        out.append({
            "start": start,
            "end": end,
            "text": text,
            "words": got,
        })
    return out


#: Чем кончился последний прогрев модели эфира: "loading" / "ready" / "error".
#: Раньше об этом знало только разовое событие в окно — окно, открытое ПОСЛЕ
#: прогрева (из трея, после переподключения), навсегда оставалось с надписью
#: «модель загружается…».
_live_state: dict[str, Any] = {"state": "loading", "error": "", "seconds": None}


def live_state() -> dict[str, Any]:
    """Состояние модели эфира: загружается, готова или не загрузилась и почему.

    Когда выбор моделей уже сделан (после прогрева), сверху — что работает на
    деле: models — одной строкой, key — чем идёт каждая роль, reread — есть ли
    «Перечитать точнее», precise_part — какую часть спрашивать перед ней, и
    почему работает не то, что выбрано (note): человек должен видеть это в
    окне, а не искать в журнале.
    """
    out = dict(_live_state)
    route = _live_route
    if route:
        out["models"] = route.get("title")
        out["key"] = route.get("key")
        out["reread"] = bool(route.get("reread"))
        out["precise_part"] = ENGINE_PARTS.get(route.get("files")) or "precise"
        if route.get("why"):
            out["note"] = route["why"]
    return out


def warmup(live: bool = True, precise: bool = False) -> dict[str, Any]:
    """Прогреть модели, чтобы первая фраза не ждала загрузку.

    live греет модель эфира — какая она, решает live_route: в режиме одной
    модели это точная, и отдельно греть её не нужно.
    """
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
