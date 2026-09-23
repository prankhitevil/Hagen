# -*- coding: utf-8 -*-
"""Разделение говорящих (диаризация) — дверь к движку разметки.

Работает ПОСЛЕ записи, в фоновой задаче. Движков два, модель у обоих одна —
community-1, и результат одинаковый:
  * diar_pyannote — через pyannote.audio; нужен токен Hugging Face;
  * diar_onnx — те же сети в ONNX и та же группировка VBx на numpy; токен,
    сеть и pyannote не нужны.
Какой из них работает, решает релиз: файл release.json в корне программы
(`{"diarize": "onnx"}` или `"pyannote"`), его пишет сборка. Переключателя в
интерфейсе нет: у одного релиза — один движок.

Здесь — всё, что от движка не зависит. Главная идея оптимизации: разметка —
самое медленное место всего конвейера. Поэтому перед запуском мы выбрасываем
тишину: Silero VAD даёт участки речи, мы склеиваем ТОЛЬКО их в один массив (с
вставкой 0.25 с тишины между участками, чтобы модель не сшивала реплики разных
людей), гоняем движок по склейке и пересчитываем времена обратно в шкалу
оригинальной записи. На обычном совещании речи 60-75 % длительности, то есть
экономим треть-половину времени.

Дальше — разнесение реплик стенограммы по голосам и хранение разметки.
"""
from __future__ import annotations

import io
import json
import logging
import threading
import time
import uuid
from types import ModuleType
from typing import Any, Callable, Iterable, Sequence

import numpy as np

from . import config, jobs, release, store, vad

log = logging.getLogger("hagen.diarize")

SR = 16000

# Если доля речи неизвестна — считаем по типовому совещанию.
DEFAULT_SPEECH_RATIO = 0.75

# Меньше этого количества речи запускать разметку бессмысленно.
MIN_SPEECH_S = 5.0
# Тишина между склеенными участками речи.
GAP_S = 0.25

# Веса шагов для шкалы прогресса и подписи по-русски. Поиск речи идёт по всей
# записи, а не по одной речи: на полуторачасовой встрече это две минуты, и без
# своего места в шкале полоска всё это время стояла на нуле (22.09).
_STEPS: tuple[tuple[str, float, str], ...] = (
    ("speech", 0.18, "поиск речи"),
    ("segmentation", 0.35, "разметка речи"),
    ("speaker_counting", 0.10, "подсчёт голосов"),
    ("embeddings", 0.45, "голосовые отпечатки"),
    ("discrete_diarization", 0.10, "сборка разметки"),
)

# Доля «чужой» речи внутри реплики (от размеченной речи реплики), после
# которой реплику надо разрезать по границам говорящих. Второе правило —
# длинный сам по себе кусок другого голоса, настройка split_min_piece_s.
SPLIT_RATIO = 0.35
# Короче этого куска резать не будем — иначе стенограмма рассыпается на слова.
MIN_PIECE_S = 0.7

#: Какой движок в каком релизе. Нет файла или в нём мусор — pyannote, как было
#: до появления второго движка (см. release.py).
RELEASE_PATH = release.PATH
ENGINES = release.ENGINES
DEFAULT_ENGINE = release.DEFAULT_ENGINE

_engine_name: str | None = None
_engine_lock = threading.Lock()


class DiarizeError(RuntimeError):
    """Понятная пользователю ошибка диаризации."""


class DiarizeCancelled(RuntimeError):
    """Задачу отменили из интерфейса."""


# ---------------------------------------------------------------- движок


def release_engine() -> str:
    """Движок, который назначил релиз (release.json)."""
    return release.diarize_engine(RELEASE_PATH)


def engine_name() -> str:
    """Имя движка этого процесса: назначенный релизом или use_engine."""
    global _engine_name
    with _engine_lock:
        if _engine_name is None:
            _engine_name = release_engine()
        return _engine_name


def use_engine(name: str | None) -> None:
    """Сменить движок в этом процессе — для проверок и инструментов сравнения.

    Программа сама его не зовёт: движок назначает релиз. None — вернуться к
    назначенному релизом.
    """
    global _engine_name
    if name is not None and name not in ENGINES:
        raise ValueError("нет такого движка разметки: %r" % name)
    with _engine_lock:
        _engine_name = name


def engine() -> ModuleType:
    """Модуль движка. Импорт лёгкий: тяжёлое каждый движок грузит при первой разметке."""
    if engine_name() == "onnx":
        from . import diar_onnx as mod
    else:
        from . import diar_pyannote as mod
    return mod


def needs_token() -> bool:
    """Нужен ли движку этого релиза токен Hugging Face (для настроек)."""
    return bool(getattr(engine(), "NEEDS_TOKEN", False))


# ---------------------------------------------------------------- готовность


def available() -> tuple[bool, str]:
    """Готова ли диаризация. Возвращает (готово, причина-по-русски)."""
    return engine().ready()


def _threads() -> int:
    try:
        return max(1, min(16, int(config.get("diarize_threads") or 10)))
    except Exception:
        return 10


def _setting(key: str) -> float | None:
    """Число из настроек; пусто или мусор — None («как у модели»)."""
    raw = config.get(key)
    if raw in (None, ""):
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def tuning() -> dict[str, float | None]:
    """Ручки разметки из «Продвинутых» — одни на оба движка. None — как у модели."""
    return {
        "step_s": _setting("diarize_window_step_s"),
        "threshold": _setting("diarize_cluster_threshold"),
        "Fb": _setting("diarize_cluster_fb"),
        "min_voice_s": _setting("diarize_min_voice_s"),
    }


def _loaded(progress: Callable | None = None) -> ModuleType:
    """Движок с загруженной моделью. Ошибки — понятные человеку."""
    eng = engine()
    ok, reason = eng.ready()
    if not ok:
        raise DiarizeError(reason)
    try:
        eng.load(_threads(), progress)
    except DiarizeCancelled:
        raise
    except Exception as err:
        raise DiarizeError(str(err)) from err
    return eng


def pipeline_model() -> str:
    """Имя модели движка этого релиза."""
    return engine().model_name()


#: Короче этого куска отпечаток голоса не считается: модели нужен хоть какой-то
#: кусок речи, на обрывке в четверть секунды получается шум.
MIN_EMB_S = 0.6


def embed_spans(pcm: Any, spans: Sequence[dict[str, float]], sr: int = 16000,
                progress: Callable | None = None) -> list[Any]:
    """Отпечаток голоса для каждого куска речи. Пусто там, где кусок короток.

    Берётся та же модель отпечатков, что и у разметки, — отдельную не грузим.
    Считается по одному куску: их обычно десятки, а длины разные, и городить
    подгонку под общий размер ради долей секунды незачем.

    Нужно для отсева эха колонок по голосу: реплика микрофона, похожая не на
    владельца, а на собеседника, — это эхо, и никакой текст сверять не надо
    (решение 17.09).
    """
    eng = _loaded(progress)
    data = np.asarray(pcm, dtype=np.float32).reshape(-1)
    out: list[Any] = []
    least = int(MIN_EMB_S * sr)
    for span in spans:
        a = max(0, int(float(span["start"]) * sr))
        b = min(data.shape[0], int(float(span["end"]) * sr))
        if b - a < least:
            out.append(None)
            continue
        try:
            vec = eng.embed(data[a:b].copy())
        except Exception as err:                      # noqa: BLE001
            log.debug("отпечаток куска %.1f–%.1f не посчитался: %s",
                      span["start"], span["end"], err)
            out.append(None)
            continue
        if vec is None:
            out.append(None)
            continue
        arr = np.asarray(vec, dtype=np.float32).reshape(-1)
        out.append(None if not np.isfinite(arr).all() else arr.tolist())
    return out


# ---------------------------------------------------------------- склейка речи


def _build_pieces(
    spans: Sequence[dict[str, Any]], total: int, sr: int, gap_s: float = GAP_S
) -> tuple[list[dict[str, float]], int]:
    """Карта «склейка -> оригинал» в отсчётах.

    Возвращает (список кусков, длина склейки в отсчётах). У куска:
      orig_start/orig_end — отсчёты в оригинале,
      glued_start/glued_end — отсчёты в склейке.
    """
    gap = max(0, int(round(float(gap_s) * sr)))
    pieces: list[dict[str, float]] = []
    cursor = 0
    for sp in spans:
        a = max(0, int(sp["start"]))
        b = min(int(total), int(sp["end"]))
        n = b - a
        if n <= 0:
            continue
        if pieces:
            cursor += gap
        pieces.append({
            "orig_start": a, "orig_end": b,
            "glued_start": cursor, "glued_end": cursor + n,
        })
        cursor += n
    return pieces, cursor


def _glue(pcm: np.ndarray, pieces: Sequence[dict[str, float]], glued_len: int) -> np.ndarray:
    """Собрать склейку речи по карте кусков."""
    out = np.zeros(int(glued_len), dtype=np.float32)
    for p in pieces:
        gs, ge = int(p["glued_start"]), int(p["glued_end"])
        out[gs:ge] = pcm[int(p["orig_start"]):int(p["orig_end"])]
    return out


def _map_back(
    turns: Iterable[dict[str, Any]],
    pieces: Sequence[dict[str, float]],
    sr: int,
    duration_s: float,
) -> list[dict[str, Any]]:
    """Пересчитать интервалы из шкалы склейки в шкалу оригинала."""
    # Границы кусков в секундах, чтобы не считать их в цикле заново.
    rng = [
        (
            p["glued_start"] / float(sr), p["glued_end"] / float(sr),
            p["orig_start"] / float(sr),
        )
        for p in pieces
    ]
    out: list[dict[str, Any]] = []
    for t in turns:
        gs, ge = float(t["start"]), float(t["end"])
        if ge <= gs:
            continue
        for g_start, g_end, o_start in rng:
            if g_end <= gs:
                continue
            if g_start >= ge:
                break
            lo = max(gs, g_start)
            hi = min(ge, g_end)
            if hi - lo <= 0.01:
                continue
            a = o_start + (lo - g_start)
            b = o_start + (hi - g_start)
            a = max(0.0, min(a, duration_s))
            b = max(0.0, min(b, duration_s))
            if b - a <= 0.01:
                continue
            out.append({
                "start": round(a, 3), "end": round(b, 3),
                "speaker": str(t["speaker"]),
            })
    out.sort(key=lambda r: (r["start"], r["end"]))
    return _merge_turns(out)


def _merge_turns(turns: list[dict[str, Any]], gap_s: float = 0.06) -> list[dict[str, Any]]:
    """Склеить соседние интервалы одного говорящего, разорванные на границе кусков."""
    merged: list[dict[str, Any]] = []
    for t in turns:
        if merged:
            last = merged[-1]
            if last["speaker"] == t["speaker"] and t["start"] - last["end"] <= gap_s:
                last["end"] = max(last["end"], t["end"])
                continue
        merged.append(dict(t))
    return merged


def _embeddings_map(labels: Sequence[str], emb: Any) -> dict[str, list[float]]:
    """Эмбеддинги в JSON-совместимый словарь «метка -> список float»."""
    if emb is None:
        return {}
    try:
        arr = np.asarray(emb, dtype=np.float32)
    except Exception:
        return {}
    if arr.ndim != 2 or arr.shape[0] == 0:
        return {}
    res: dict[str, list[float]] = {}
    for i, label in enumerate(labels):
        if i >= arr.shape[0]:
            break
        row = arr[i]
        if not np.all(np.isfinite(row)):
            continue
        res[str(label)] = [round(float(x), 6) for x in row.tolist()]
    return res


# ---------------------------------------------------------------- прогресс


class _Progress:
    """Переводит шаги движка в шкалу 0..1 и считает остаток времени.

    Оба движка зовут его одинаково: hook(шаг, артефакт, completed=, total=).
    Он же бросает DiarizeCancelled, когда задачу отменили.
    """

    def __init__(self, handle: Any, t0: float):
        self.handle = jobs.as_handle(handle)
        self.t0 = t0
        self.value = 0.0
        self.offsets: dict[str, tuple[float, float, str]] = {}
        acc = 0.0
        for name, weight, title in _STEPS:
            self.offsets[name] = (acc, weight, title)
            acc += weight
        self.total_weight = acc or 1.0
        self.done_steps: set[str] = set()

    def __call__(
        self,
        step_name: str = "",
        step_artefact: Any = None,
        file: Any = None,
        completed: Any = None,
        total: Any = None,
        **_kw: Any,
    ) -> None:
        if self.handle.cancelled:
            raise DiarizeCancelled("Диаризация отменена")

        base, weight, title = self.offsets.get(
            str(step_name),
            (self.value * self.total_weight, 0.0, str(step_name) or "разметка"),
        )
        frac = 1.0
        try:
            if completed is not None and total:
                frac = max(0.0, min(1.0, float(completed) / float(total)))
        except (TypeError, ValueError, ZeroDivisionError):
            frac = 1.0
        if str(step_name) in self.done_steps and frac >= 1.0:
            return
        if frac >= 1.0:
            self.done_steps.add(str(step_name))

        value = (base + weight * frac) / self.total_weight
        self.value = max(self.value, min(0.999, value))
        self.report(title)

    def report(self, note: str) -> None:
        self.handle.progress(self.value, note)
        # Пока шла запись, задача стояла на паузе — это время в остаток не в счёт.
        elapsed = time.time() - self.t0 - float(self.handle.paused_s or 0.0)
        if self.value > 0.03 and elapsed > 1.0:
            self.handle.eta(max(0.0, elapsed / self.value - elapsed))


# ---------------------------------------------------------------- главная функция


def diarize_pcm(
    pcm: np.ndarray,
    sr: int = SR,
    min_speakers: int | None = None,
    max_speakers: int | None = None,
    handle: Any = None,
) -> dict[str, Any]:
    """Разметить говорящих. pcm — numpy float32 моно 16 кГц.

    Возвращает словарь с интервалами в шкале ОРИГИНАЛЬНОЙ записи:
      turns, exclusive_turns, labels, embeddings, speech_seconds, elapsed_s,
      rtf, model.
    """
    from . import audio_io

    handle = jobs.as_handle(handle)
    t0 = time.time()
    arr = np.ascontiguousarray(np.asarray(pcm, dtype=np.float32).reshape(-1))
    if int(sr) != SR:
        arr = np.ascontiguousarray(audio_io.resample(arr, int(sr), SR).astype(np.float32))
    total = int(arr.shape[0])
    duration_s = total / float(SR)

    empty: dict[str, Any] = {
        "turns": [], "exclusive_turns": [], "labels": [], "embeddings": {},
        "speech_seconds": 0.0, "elapsed_s": 0.0, "rtf": 0.0,
        "model": pipeline_model(),
        "duration_s": round(duration_s, 3),
    }
    if total <= 0:
        return empty

    # --- шаг 1: где вообще речь. Пауза на время записи может прийти и сюда,
    # посреди поиска речи: общая модель поиска речи при этом занята, но во время
    # записи она никому не нужна — у живой записи свои модели на каждую дорожку,
    # а диктовка во время записи спит.
    hook = _Progress(handle, time.time())
    hook("speech", completed=0, total=100)
    try:
        spans = vad.speech_timestamps(arr, progress=_speech_progress(hook))
    except DiarizeCancelled:
        raise
    except Exception as err:
        log.warning("VAD не сработал (%s), размечаю запись целиком", err)
        spans = [{"start": 0, "end": total}]

    pieces, glued_len = _build_pieces(spans, total, SR)
    speech_samples = sum(int(p["orig_end"]) - int(p["orig_start"]) for p in pieces)
    speech_s = speech_samples / float(SR)
    empty["speech_seconds"] = round(speech_s, 3)

    if speech_s < MIN_SPEECH_S or not pieces:
        log.info("речи всего %.1f c — диаризация не нужна", speech_s)
        empty["elapsed_s"] = round(time.time() - t0, 2)
        return empty

    log.info(
        "диаризация: запись %.1f c, речи %.1f c (%.0f %%), участков %d",
        duration_s, speech_s, 100.0 * speech_s / max(1e-6, duration_s), len(pieces),
    )

    # --- шаг 2: модель
    eng = _loaded(progress=(lambda v, n: handle.log(n)))
    if handle.cancelled:
        raise DiarizeCancelled("Диаризация отменена")

    glued = _glue(arr, pieces, glued_len)

    # --- шаг 3: движок по склейке. Ручки читаем здесь, а не при загрузке:
    # настройки могли поменять после неё.
    t_apply = time.time()
    out = eng.run(glued, min_speakers=min_speakers, max_speakers=max_speakers,
                  tuning=tuning(), hook=hook)
    apply_s = time.time() - t_apply

    labels = [str(x) for x in out.get("labels") or []]
    turns = _map_back(out.get("turns") or [], pieces, SR, duration_s)
    excl = _map_back(out.get("exclusive_turns") or [], pieces, SR, duration_s)

    elapsed = time.time() - t0
    result = {
        "turns": turns,
        "exclusive_turns": excl,
        "labels": labels,
        "embeddings": _embeddings_map(labels, out.get("centroids")),
        "speech_seconds": round(speech_s, 3),
        "duration_s": round(duration_s, 3),
        "elapsed_s": round(elapsed, 2),
        "rtf": round(apply_s / max(1e-6, speech_s), 3),
        "model": eng.model_name(),
    }
    log.info(
        "диаризация готова: голосов %d, интервалов %d, %.1f c (RTF %.2f)",
        len(labels), len(turns), elapsed, result["rtf"],
    )
    handle.progress(1.0, "разметка готова")
    handle.eta(0.0)
    return result


def _speech_progress(hook: _Progress) -> Callable[[float], None]:
    """Ход поиска речи — в шкалу разметки.

    Поиск речи сообщает ход на каждое окно в 32 мс, на полуторачасовой записи
    это под двести тысяч раз. Дальше передаём раз на процент: полоске этого
    хватает, и отмена задачи проверяется так же часто.
    """
    last = [-1]

    def report(percent: float) -> None:
        whole = int(percent)
        if whole != last[0]:
            last[0] = whole
            hook("speech", completed=whole, total=100)

    return report


# ---------------------------------------------------------------- где считать


def runs_in_helper() -> bool:
    """Размечать отдельной программой с низким приоритетом (решение 22.09).

    Так выбрано в настройке «пока идёт новая запись»; по умолчанию — да.
    """
    return config.get("processing_during_recording") == "background"


def diarize_track(path: str | Any, min_speakers: int | None = None,
                  max_speakers: int | None = None, handle: Any = None,
                  isolated: bool = False) -> dict[str, Any]:
    """Разметить дорожку-файл: в самой программе или помощником.

    `isolated` — помощником (`diarize_worker`). Не запустился — размечаем
    здесь, и тогда на время записи задача встаёт на паузу, как остальная
    тяжёлая работа. Когда помощник закончил, задача снова считается «здесь»:
    сохранение результата и голоса идут в самой программе.
    """
    from . import audio_io

    handle = jobs.as_handle(handle)
    est = estimate_seconds(audio_io.wav_duration(path))
    handle.eta(est)
    handle.log("примерная оценка: %.0f мин" % (est / 60.0))
    if isolated:
        from . import diarize_worker

        try:
            return diarize_worker.run(path, min_speakers, max_speakers, handle)
        except diarize_worker.NotStarted as err:
            log.warning("помощник разметки не запустился (%s) — размечаю в самой программе", err)
            handle.log("помощник не запустился — размечаю в самой программе")
        finally:
            handle.where = "here"
    pcm, sr = audio_io.read_wav(path)
    return diarize_pcm(pcm, sr=sr, min_speakers=min_speakers, max_speakers=max_speakers,
                       handle=handle)


def estimate_seconds(pcm_or_duration: Any, speech_ratio: float | None = None) -> float:
    """Оценка времени разметки: RTF движка от ДЛИТЕЛЬНОСТИ РЕЧИ плюс накладные.

    pcm_or_duration — либо длительность в секундах, либо массив 16 кГц.
    speech_ratio — доля речи 0..1 (если не передать, берём типовые 0.75).
    """
    eng = engine()
    try:
        if isinstance(pcm_or_duration, (int, float)) and not isinstance(pcm_or_duration, bool):
            duration = float(pcm_or_duration)
        else:
            arr = np.asarray(pcm_or_duration)
            duration = float(arr.reshape(-1).shape[0]) / float(SR)
    except Exception:
        return 0.0
    if duration <= 0.0:
        return 0.0
    try:
        ratio = DEFAULT_SPEECH_RATIO if speech_ratio is None else float(speech_ratio)
    except (TypeError, ValueError):
        ratio = DEFAULT_SPEECH_RATIO
    ratio = max(0.0, min(1.0, ratio))
    speech_s = duration * ratio
    if speech_s < MIN_SPEECH_S:
        return 0.0
    return round(speech_s * float(eng.RTF) + float(eng.OVERHEAD_S), 1)


# ---------------------------------------------------------------- переразметка


def label_to_key(label: str) -> str:
    """Метка pyannote -> ключ говорящего в стенограмме.

    Ключ = сама метка («SPEAKER_00»), и это важно: voices.apply_to_meta кладёт
    в meta["speakers"] записи ровно под метками pyannote, а store.rename_speaker
    ищет реплики по seg["speaker_key"]. Любое переименование ключа здесь
    разорвало бы связку «база голосов -> meta -> реплики».
    """
    return str(label or "").strip()


def label_to_name(label: str) -> str:
    """Метка pyannote -> подпись по-русски до переименования пользователем."""
    s = str(label or "").strip()
    digits = "".join(ch for ch in s if ch.isdigit())
    if digits:
        return "%s %d" % (store.SPEAKER_FAR, int(digits) + 1)
    return store.SPEAKER_FAR


def _overlap(a0: float, a1: float, b0: float, b1: float) -> float:
    return max(0.0, min(a1, b1) - max(a0, b0))


def _best_speaker(
    start: float, end: float, turns: Sequence[dict[str, Any]]
) -> tuple[str | None, float]:
    """Говорящий с наибольшим перекрытием. Возвращает (метка, секунды перекрытия)."""
    if end <= start or not turns:
        return None, 0.0
    acc: dict[str, float] = {}
    for t in turns:
        ov = _overlap(start, end, float(t["start"]), float(t["end"]))
        if ov > 0.0:
            acc[str(t["speaker"])] = acc.get(str(t["speaker"]), 0.0) + ov
    if not acc:
        return None, 0.0
    label = max(acc.items(), key=lambda kv: kv[1])
    return label[0], label[1]


def _nearest_speaker(start: float, end: float, turns: Sequence[dict[str, Any]]) -> str | None:
    """Ближайший по времени говорящий — для слов, попавших в паузу."""
    best: str | None = None
    best_dist = None
    mid = (start + end) / 2.0
    for t in turns:
        ts, te = float(t["start"]), float(t["end"])
        dist = 0.0 if ts <= mid <= te else min(abs(mid - ts), abs(mid - te))
        if best_dist is None or dist < best_dist:
            best_dist, best = dist, str(t["speaker"])
    return best


def usable_words(
    words: Any, seg_start: float, seg_end: float
) -> list[dict[str, Any]]:
    """Отобрать слова с годными АБСОЛЮТНЫМИ отметками времени.

    Если у слов нет времени (распознавание без word_timestamps) или время явно
    не в шкале записи (например, отсчитано от начала куска, а не от начала
    записи), по словам размечать нельзя: иначе все слова попадут в нулевую
    секунду и реплика получит говорящего из самого начала записи вместо того,
    кто в ней на самом деле говорит. В таком случае возвращаем пустой список —
    реплика будет размечена целиком по перекрытию.
    """
    if not words:
        return []
    good: list[dict[str, Any]] = []
    lo: float | None = None
    hi: float | None = None
    for w in words:
        if not isinstance(w, dict):
            continue
        try:
            ws = float(w.get("start"))
            we = float(w.get("end"))
        except (TypeError, ValueError):
            continue
        if not (np.isfinite(ws) and np.isfinite(we)):
            continue
        # слово нулевой длительности оставляем (иначе при разрезе реплики
        # пропадёт его текст), но протяжённость считаем только по настоящим
        if we > ws:
            lo = ws if lo is None else min(lo, ws)
            hi = we if hi is None else max(hi, we)
        good.append(w)
    if not good or lo is None or hi is None:
        return []
    # отметки должны хотя бы наполовину лежать внутри самой реплики
    extent = hi - lo
    inside = max(0.0, min(hi, seg_end + 0.5) - max(lo, seg_start - 0.5))
    if extent <= 0.0 or inside < 0.5 * extent:
        log.debug(
            "отметки слов (%.2f-%.2f) вне реплики (%.2f-%.2f) — размечаю целиком",
            lo, hi, seg_start, seg_end,
        )
        return []
    return good


def _word_groups(
    words: Sequence[dict[str, Any]], turns: Sequence[dict[str, Any]], fallback: str | None
) -> list[dict[str, Any]]:
    """Разбить слова на подряд идущие группы одного говорящего."""
    groups: list[dict[str, Any]] = []
    for w in words:
        try:
            ws, we = float(w.get("start") or 0.0), float(w.get("end") or 0.0)
        except (TypeError, ValueError):
            continue
        if we < ws:
            ws, we = we, ws
        label, ov = _best_speaker(ws, we, turns)
        if label is None or ov <= 0.0:
            label = _nearest_speaker(ws, we, turns) or fallback
        if label is None:
            continue
        dur = max(0.0, we - ws)
        if groups and groups[-1]["speaker"] == label:
            g = groups[-1]
            g["words"].append(w)
            g["end"] = max(g["end"], we)
            g["duration"] += dur
        else:
            groups.append({
                "speaker": label, "start": ws, "end": we,
                "duration": dur, "words": [w],
            })
    return groups


def _merge_small_groups(groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Слить слишком короткие группы с соседями: не дробим стенограмму на слова."""
    if len(groups) <= 1:
        return groups
    changed = True
    while changed and len(groups) > 1:
        changed = False
        for i, g in enumerate(groups):
            if (g["end"] - g["start"]) >= MIN_PIECE_S and len(g["words"]) > 1:
                continue
            # к какому соседу примкнуть: к более длинному
            prev_g = groups[i - 1] if i > 0 else None
            next_g = groups[i + 1] if i + 1 < len(groups) else None
            target = None
            if prev_g is not None and next_g is not None:
                target = prev_g if prev_g["duration"] >= next_g["duration"] else next_g
            else:
                target = prev_g or next_g
            if target is None:
                continue
            target["words"] = (
                target["words"] + g["words"] if target is prev_g else g["words"] + target["words"]
            )
            target["start"] = min(target["start"], g["start"])
            target["end"] = max(target["end"], g["end"])
            target["duration"] += g["duration"]
            del groups[i]
            changed = True
            break
    # после слияний рядом могли оказаться группы одного голоса
    out: list[dict[str, Any]] = []
    for g in groups:
        if out and out[-1]["speaker"] == g["speaker"]:
            prev = out[-1]
            prev["words"].extend(g["words"])
            prev["end"] = max(prev["end"], g["end"])
            prev["duration"] += g["duration"]
            continue
        out.append(g)
    return out


def _words_text(words: Sequence[dict[str, Any]]) -> str:
    parts = [str(w.get("text") or "").strip() for w in words]
    return " ".join(p for p in parts if p).strip()


def _apply_label(seg: dict[str, Any], label: str, naming: Any = None) -> bool:
    """Проставить реплике говорящего на месте. True, если что-то изменилось.

    naming — (ключ по метке, имя по метке) для разделения голосов одного
    говорящего; тогда имя ставится и закреплённой реплике.
    """
    before = (seg.get("speaker_key"), seg.get("speaker"))
    if naming is not None:
        key_for, name_for = naming
        seg["speaker_key"] = key_for(label)
        seg["speaker"] = name_for(label)
        return before != (seg.get("speaker_key"), seg.get("speaker"))
    seg["speaker_key"] = label_to_key(label)
    if not seg.get("speaker_locked"):
        seg["speaker"] = label_to_name(label)
    return before != (seg.get("speaker_key"), seg.get("speaker"))


def _labeled_copy(seg: dict[str, Any], label: str, naming: Any = None) -> tuple[dict[str, Any], bool]:
    """Копия реплики с новым говорящим. Исходный словарь не меняем: relabel
    обязан возвращать новый список, не портя то, что ему дали."""
    copy = dict(seg)
    changed = _apply_label(copy, label, naming)
    return copy, changed


def _child_segment(seg: dict[str, Any], group: dict[str, Any], naming: Any = None) -> dict[str, Any]:
    """Новая реплика из группы слов: копия исходной с новым временем и текстом."""
    child = dict(seg)
    child["id"] = uuid.uuid4().hex[:8]
    child["words"] = list(group["words"])
    child["start"] = round(float(group["start"]), 3)
    child["end"] = round(float(group["end"]), 3)
    text = _words_text(group["words"])
    if text:
        child["text"] = text
    child["suggestion"] = None
    _apply_label(child, group["speaker"], naming)
    return child


def _split_min_piece() -> float:
    """С какой длины кусок другого голоса режет фразу. Пусто — заводские 2 с."""
    got = _setting("split_min_piece_s")
    if got is None:
        return float(config.DEFAULTS.get("split_min_piece_s") or 0.0)
    return max(0.0, got)


def _relabel_core(
    segments: Sequence[dict[str, Any]], turns: Sequence[dict[str, Any]], track: str | None,
    pick: Any = None, naming: Any = None,
) -> tuple[list[dict[str, Any]], int]:
    """Общая работа relabel/assign_speakers. Возвращает (новый список, изменено).

    pick — какие реплики размечать (для разделения голосов одного говорящего:
    любая дорожка, в том числе микрофон и закреплённые реплики). Без pick —
    обычная разметка дорожки track.
    """
    clean: list[dict[str, Any]] = []
    for t in (turns or []):
        if not isinstance(t, dict) or not t.get("speaker"):
            continue
        try:
            ts = float(t.get("start") or 0.0)
            te = float(t.get("end") or 0.0)
        except (TypeError, ValueError):
            continue
        if te <= ts:
            continue
        clean.append({"start": ts, "end": te, "speaker": str(t["speaker"])})
    clean.sort(key=lambda t: t["start"])
    turns = clean

    out: list[dict[str, Any]] = []
    changed = 0
    for seg in segments or []:
        seg_track = str(seg.get("track") or "")
        if pick is not None:
            if not pick(seg):
                out.append(seg)
                continue
        # дорожка микрофона — «Я»: при обычной разметке её не трогаем никогда
        elif seg_track == store.TRACK_MIC:
            out.append(seg)
            continue
        elif track is not None and seg_track != str(track):
            out.append(seg)
            continue
        elif seg.get("speaker_locked"):
            out.append(seg)
            continue
        if not turns:
            out.append(seg)
            continue

        try:
            s0 = float(seg.get("start") or 0.0)
            s1 = float(seg.get("end") or 0.0)
        except (TypeError, ValueError):
            out.append(seg)
            continue
        if s1 <= s0:
            out.append(seg)
            continue

        main_label, _ov = _best_speaker(s0, s1, turns)
        words = usable_words(seg.get("words"), s0, s1)
        groups = _merge_small_groups(_word_groups(words, turns, main_label)) if words else []

        if not groups:
            # слов нет (или они без времени) — размечаем реплику целиком
            if main_label is None:
                out.append(seg)
                continue
            new_seg, was_changed = _labeled_copy(seg, main_label, naming)
            out.append(new_seg)
            changed += 1 if was_changed else 0
            continue

        if len(groups) == 1:
            new_seg, was_changed = _labeled_copy(seg, groups[0]["speaker"], naming)
            out.append(new_seg)
            changed += 1 if was_changed else 0
            continue

        # Доля речи «не основного» говорящего внутри реплики. Считаем по
        # протяжённости групп слов (а не по сумме длительностей самих слов):
        # иначе паузы между словами занижают долю и реплика никогда не режется.
        by_speaker: dict[str, float] = {}
        for g in groups:
            extent = max(0.0, float(g["end"]) - float(g["start"]))
            by_speaker[g["speaker"]] = by_speaker.get(g["speaker"], 0.0) + extent
        covered = sum(by_speaker.values())
        dominant = max(by_speaker.items(), key=lambda kv: kv[1])[0]
        alien = covered - by_speaker[dominant]
        # Кусок другого голоса, длинный сам по себе, режем при любой доле: иначе
        # длинная фраза проглатывает короткие чужие реплики целиком (15.09).
        piece = _split_min_piece()
        long_alien = piece > 0 and any(
            g["speaker"] != dominant and float(g["end"]) - float(g["start"]) >= piece
            for g in groups)
        if covered <= 0.0 or ((alien / covered) <= SPLIT_RATIO and not long_alien):
            new_seg, was_changed = _labeled_copy(seg, dominant, naming)
            out.append(new_seg)
            changed += 1 if was_changed else 0
            continue

        # режем реплику по границам говорящих
        for g in groups:
            out.append(_child_segment(seg, g, naming))
        changed += len(groups)

    return out, changed


def relabel_subset(
    segments: Sequence[dict[str, Any]], turns: Sequence[dict[str, Any]],
    pick: Any, key_for: Any, name_for: Any,
) -> list[dict[str, Any]]:
    """Разнести по голосам только выбранные реплики (разделение одного говорящего).

    В отличие от relabel, трогает любую дорожку, в том числе микрофон и
    закреплённые реплики: человек сам попросил разделить именно их.
    """
    new_list, _changed = _relabel_core(segments, turns, None, pick=pick,
                                       naming=(key_for, name_for))
    return new_list


def relabel(
    segments: Sequence[dict[str, Any]], turns: Sequence[dict[str, Any]], track: str = store.TRACK_FAR
) -> list[dict[str, Any]]:
    """Новый список реплик по результату диаризации (с возможными разбиениями).

    Реплики дорожки mic и реплики с speaker_locked=True не меняются.
    """
    new_list, _changed = _relabel_core(segments, turns, track)
    return new_list


# ---------------------------------------------------------------- хранение


def save_result(rec_id: str, result: dict[str, Any]) -> None:
    """Сохранить diarization.json рядом с записью.

    Если папки записи уже нет — молча выходим: запись удалили, пока разметка
    договаривала свой шаг, и mkdir поднял бы пустую папку-призрак.
    """
    path = store.paths(rec_id)["diarization"]
    if not path.parent.exists():
        log.info("разметку сохранять некуда: запись %s удалена", rec_id)
        return
    config.atomic_json(path, _jsonable(result))
    log.info("разметка говорящих сохранена: %s", path.name)


def load_result(rec_id: str) -> dict[str, Any] | None:
    """Прочитать diarization.json. None, если файла нет или он битый."""
    path = store.paths(rec_id)["diarization"]
    if not path.exists():
        return None
    try:
        with io.open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (json.JSONDecodeError, UnicodeDecodeError, OSError, ValueError) as err:
        log.warning("не читается diarization.json: %s", err)
        return None
    return data if isinstance(data, dict) else None


def _jsonable(value: Any) -> Any:
    """numpy-типы в обычные питоновские, иначе json.dump падает."""
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, np.ndarray):
        return [_jsonable(v) for v in value.tolist()]
    if isinstance(value, np.generic):
        return value.item()
    return value
