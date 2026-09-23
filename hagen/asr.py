# -*- coding: utf-8 -*-
"""Распознавание речи моделями GigaAM.

Две модели, обе — GigaAM v3 в готовом ONNX (репозиторий istupakov/gigaam-v3-onnx),
считает их пакет onnx-asr на onnxruntime; torch программе не нужен:
  v3_e2e_ctc   — быстрая (движок «fast»): быстрый старт, без отметок времени
                 по словам;
  v3_e2e_rnnt  — точная («ox_fp32» — полные веса, «ox_int8» — сжатые), с
                 отметками времени по словам: по ним текст точно сшивается с
                 разметкой говорящих.

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
from typing import Any

import numpy as np

from . import config

log = logging.getLogger("hagen.asr")

SR = 16000
MAX_CHUNK_S = 24.0          # предел GigaAM: у неё жёсткие 25 с
MAX_CHUNK_EN_S = 120.0      # у Parakeet такого предела нет, но память не бесконечна

#: Английская модель. GigaAM знает только русский (это написано в её карточке),
#: а многоязычная модель Сбера сама называет своё качество на английском
#: «умеренным»: 21-26 % ошибок против 6 % у Parakeet. Поэтому английский путь —
#: отдельная модель NVIDIA Parakeet TDT 0.6B v2 на том же onnxruntime.
EN_DIR = config.MODELS_DIR / "onnx-asr"
EN_REPO = "istupakov/parakeet-tdt-0.6b-v2-onnx"
EN_MODEL = "nemo-parakeet-tdt-0.6b-v2"
#: Шаг сетки отметок времени: window_step 0,01 с × subsampling 8.
EN_FRAME_S = 0.08

_lock = threading.RLock()
# Замок на само распознавание: две дорожки эфира, диктовка и файл считают по
# очереди, а не делят ядра между собой, — иначе все они тормозили бы разом.
# Скорость позволяет: распознавание в 10-20 раз быстрее речи, две дорожки по
# очереди занимают около 10 % процессорного времени.
_infer_lock = threading.Lock()
_en_cache: dict[str, Any] = {}
#: Модели GigaAM в памяти, по движку, и когда каждую трогали в последний раз.
#: Сторож ниже выгружает после простоя ту, что не служит эфиру: точная с
#: полными весами занимает около 0,9 ГБ, а нужна не всегда.
_ox_cache: dict[str, Any] = {}
_ox_used: dict[str, float] = {}
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


def _onnx_session_options():
    import onnxruntime as rt

    opts = rt.SessionOptions()
    opts.graph_optimization_level = rt.GraphOptimizationLevel.ORT_ENABLE_ALL
    opts.intra_op_num_threads = _threads()
    opts.inter_op_num_threads = 1
    opts.execution_mode = rt.ExecutionMode.ORT_SEQUENTIAL
    opts.log_severity_level = 3
    return opts


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


# ------------------------------------------------------- модели GigaAM в onnx-asr

#: Обе русские модели — из одного репозитория, в папке рядом с английской. Замер
#: на записях владельца (18.09): точная с полными весами даёт текст слово в слово
#: как исходная модель на torch, сжатые — 98,1 % слов при трети памяти; быстрая
#: в этом же экспорте — 98,6-98,8 % слов против прежнего экспорта из пакета
#: gigaam (23.09) и на 20 % быстрее. В сборку кладётся только рекомендованный
#: вариант (recommended_part), остальное качается кнопкой.
OX_DIR = EN_DIR / "gigaam-v3"
OX_REPO = "istupakov/gigaam-v3-onnx"
#: Движок → (имя модели в onnx-asr, сжатие весов; None — полные).
OX_MODELS: dict[str, tuple[str, str | None]] = {
    "fast": ("gigaam-v3-e2e-ctc", None),
    "ox_fp32": ("gigaam-v3-e2e-rnnt", None),
    "ox_int8": ("gigaam-v3-e2e-rnnt", "int8"),
}
#: Движки точной модели: с отметками времени по словам.
OX_ENGINES = ("ox_fp32", "ox_int8")
#: Шаг сетки отметок времени GigaAM: окно 10 мс × прореживание 4.
GIGAAM_FRAME_S = 0.04


def ox_files(engine: str) -> list[str]:
    """Файлы, без которых движок не поедет. Имена — как в репозитории."""
    model, quant = OX_MODELS[engine]
    stem = "v3_e2e_ctc" if model.endswith("ctc") else "v3_e2e_rnnt"
    suffix = (".%s" % quant) if quant else ""
    parts = [""] if stem.endswith("ctc") else ["_encoder", "_decoder", "_joint"]
    return ["config.json", stem + "_vocab.txt"] + [stem + p + suffix + ".onnx" for p in parts]


def ox_available(engine: str) -> tuple[bool, str]:
    """На месте ли файлы движка. Вторым значением — почему нет."""
    try:
        import onnx_asr  # noqa: F401
    except Exception:
        return False, "Нет пакета onnx-asr."
    missing = [n for n in ox_files(engine) if not (OX_DIR / n).exists()]
    if missing:
        return False, ("Файлы модели «%s» не скачаны (%d из %d). Папка: %s"
                       % (ENGINE_TITLES.get(engine, engine), len(missing), len(ox_files(engine)), OX_DIR))
    return True, "Готова."


def _load_ox(engine: str):
    """Модель движка из памяти или с диска; отметка «трогали» обновляется."""
    with _lock:
        got = _ox_cache.get(engine)
        if got is not None:
            _ox_used[engine] = time.time()
            return got
        ok, why = ox_available(engine)
        if not ok:
            raise RuntimeError(why)
        import onnx_asr

        name, quant = OX_MODELS[engine]
        t0 = time.time()
        model = onnx_asr.load_model(
            name,
            path=str(OX_DIR),
            quantization=quant,
            sess_options=_onnx_session_options(),
            providers=["CPUExecutionProvider"],
        )
        if engine in OX_ENGINES:
            model = model.with_timestamps()
        _ox_cache[engine] = model
        _ox_used[engine] = time.time()
        log.info("модель «%s» загружена за %.1f c, потоков %d",
                 ENGINE_TITLES.get(engine, engine), time.time() - t0, _threads())
    _ensure_janitor()
    return model


def _transcribe_ox(pcm: np.ndarray, engine: str) -> Result:
    """Распознать кусок движком. Слова со временем — только у точной."""
    model = _load_ox(engine)
    arr = np.ascontiguousarray(np.asarray(pcm, dtype=np.float32).reshape(-1))
    with _infer_lock:
        res = model.recognize(arr, sample_rate=SR)
    text = str(getattr(res, "text", res) or "")
    if engine not in OX_ENGINES:
        return Result(text)
    words = _words_from_tokens(getattr(res, "tokens", None), getattr(res, "timestamps", None),
                               arr.shape[0] / float(SR), frame_s=GIGAAM_FRAME_S, tight=True)
    return Result(text, words)


def preload_precise() -> None:
    """Начать загрузку модели голосового ввода в фоне, если её ещё нет в памяти.

    Диктовка зовёт это в момент, когда человек начал говорить: пока он говорит
    свои несколько секунд, модель успевает загрузиться, и распознавание после
    «стоп» не ждёт. Загрузка берёт только _lock, не замок распознавания, так что
    ничему не мешает; повторный вызов во время загрузки просто подождёт её.
    Грузится модель, выбранная для голосового ввода (live_route).
    """
    engine = live_route()["voice"]
    with _lock:
        if engine in _ox_cache:
            _ox_used[engine] = time.time()
            return

    def run() -> None:
        try:
            _load_ox(engine)
        except Exception as err:
            log.warning("фоновая загрузка модели голосового ввода не удалась: %s", err)

    threading.Thread(target=run, name="asr-preload", daemon=True).start()


def idle_minutes() -> float:
    """Через сколько минут простоя выгружать модель. 0 = не выгружать."""
    try:
        got = float(config.get("precise_idle_min") or 0)
    except (TypeError, ValueError):
        return 0.0
    return got if got > 0 else 0.0


def release_idle(minutes: float | None = None) -> list[str]:
    """Выгрузить модели, к которым давно не обращались.

    Возвращает движки выгруженного. Модель, на которой прямо сейчас идёт
    распознавание, не трогает: замок распознавания берётся без ожидания, и
    занятая модель просто доживёт до следующего круга. Ссылку, уже взятую
    чужим потоком, выгрузка не ломает — память освободится, когда тот поток
    закончит.

    Модель, которая служит эфиру, не выгружается никогда: окно показывало бы
    «готова», а первая фраза записи ждала бы загрузку.
    """
    limit = idle_minutes() if minutes is None else float(minutes)
    if limit <= 0:
        return []
    keep = _live_keeps()
    dropped: list[str] = []
    with _lock:
        now = time.time()
        for engine, used in list(_ox_used.items()):
            if engine == keep or now - used < limit * 60.0:
                continue
            if not _infer_lock.acquire(blocking=False):
                continue
            try:
                if _ox_cache.pop(engine, None) is not None:
                    dropped.append(engine)
                _ox_used.pop(engine, None)
            finally:
                _infer_lock.release()
    if dropped:
        import gc

        gc.collect()
        log.info("модель выгружена из памяти после %g мин простоя: %s",
                 limit, ", ".join(ENGINE_TITLES.get(e, e) for e in dropped))
    return dropped


def _ensure_janitor() -> None:
    """Поднять сторож простоя. Поток, а не процесс: Kaspersky не даёт порождать
    фоновые процессы."""
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


def max_chunk_seconds(lang: str = "ru") -> float:
    """Предел длины куска. У русской и английской моделей он разный."""
    return MAX_CHUNK_EN_S if str(lang).lower() == "en" else MAX_CHUNK_S


# ---------------------------------------------------------------- выбор моделей

#: Движки распознавания и скачиваемая часть (needs.PARTS), без которой движок
#: не поедет:
#:   fast    — быстрая модель v3_e2e_ctc;
#:   ox_fp32 / ox_int8 — точная v3_e2e_rnnt, полные или сжатые веса.
ENGINE_PARTS = {"fast": "fast", "ox_fp32": "precise_ox_fp32", "ox_int8": "precise_ox_int8"}
ENGINE_TITLES = {"fast": "быстрая", "ox_fp32": "точная (полные веса)",
                 "ox_int8": "точная (сжатые веса)"}
#: Роли: звонки и разговоры (эфир), голосовой ввод (диктовка, голосовые
#: заметки) и файлы (видео, файлы с диска, «Перечитать точнее»).
ROLES = ("live", "voice", "files")
ROLE_TITLES = {"live": "звонки", "voice": "голосовой ввод", "files": "файлы и видео"}

#: Что ставит «Сбросить всё», кладёт сборка для другого человека и советует
#: подсказка: одна точная модель — в памяти одна модель вместо двух. Веса
#: сжатые (решение 23.09): в 3,5 раза меньше памяти и на 19 % быстрее, слова
#: совпадают с полными на 98 %; полные — слово в слово как исходная модель.
RECOMMENDED: dict[str, Any] = {"asr_count": 1, "asr_single": "precise",
                               "asr_weights": "int8", "live_draft": False}

#: Как распознавание устроено в этом запуске программы. Решается один раз — при
#: прогреве или на первой фразе — и до перезапуска не меняется: иначе посреди
#: записи фразы пошли бы разными моделями, а в памяти оказались бы обе.
_live_route: dict[str, Any] | None = None
#: Свой замок, а не _lock: тот держится всю загрузку модели, и начало записи
#: ждало бы, пока грузится, например, модель для видео.
_route_lock = threading.Lock()


def _precise_engine(choice: dict[str, Any] | None = None) -> str:
    """Движок точной модели: по весам из настроек. choice — свой набор
    параметров вместо настроек."""
    get = choice.get if choice is not None else config.get
    return "ox_fp32" if str(get("asr_weights") or "int8") == "fp32" else "ox_int8"


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


def engine_ready(engine: str) -> tuple[bool, str]:
    """На месте ли файлы движка. Вторым значением — почему нет."""
    if engine not in OX_MODELS:
        return False, "неизвестный движок %s" % engine
    ok, why = ox_available(engine)
    if not ok and "не скачаны" in why:
        why = "%s модель не скачана" % ("быстрая" if engine == "fast" else "точная")
    return ok, why


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
    order = (["fast", "ox_int8", "ox_fp32"] if engine != "fast"
             else [_precise_engine(), "ox_int8", "ox_fp32"])
    for alt in order:
        if alt != engine and engine_ready(alt)[0]:
            return alt
    return "fast"


def _pick_live_route() -> dict[str, Any]:
    """Решить, как пойдёт распознавание в этом запуске.

    Нет файлов нужного движка — роль берёт первый готовый, а причина видна в
    окне (live_state) и в журнале: запись не должна ломаться ни при каких
    условиях.
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
    onnxruntime; why — почему работает не то, что выбрано.
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
    """Движок, который служит эфиру: его сторож простоя не трогает."""
    route = _live_route
    return route.get("live") if route else None


def part_for(role: str) -> str:
    """Какая скачиваемая часть нужна роли в этом запуске."""
    return ENGINE_PARTS[live_route()[role]]


def precise_part() -> str:
    """Часть, нужная файлам, видео и «Перечитать точнее»."""
    return part_for("files")


def prepare_all() -> list[tuple[str, bool]]:
    """Подготовить заранее всё, что нужно выбору в настройках (run.py --prepare).

    Каждая часть качается тем же путём, что и кнопка «Скачать нужное».
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
    if engine not in OX_MODELS:
        raise ValueError("неизвестный движок распознавания: %s" % engine)
    return _transcribe_ox(arr, engine)


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
        out["precise_part"] = ENGINE_PARTS.get(route.get("files")) or recommended_part()
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
            transcribe_precise(silence, words=True)
            info["precise"] = round(time.time() - t0, 2)
        except Exception as err:
            info["precise_error"] = str(err)
    return info
