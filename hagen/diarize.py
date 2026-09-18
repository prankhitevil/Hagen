# -*- coding: utf-8 -*-
"""Разделение говорящих (диаризация) через pyannote.audio 4.x.

Работает ПОСЛЕ записи, в фоновой задаче. Тяжёлые зависимости (torch, pyannote)
импортируются лениво внутри функций, чтобы служба стартовала быстро.

Главная идея оптимизации: pyannote — самое медленное место всего конвейера
(замерено RTF ~0.63 на этой машине, то есть час записи ~38 минут). Поэтому
перед запуском мы выбрасываем тишину: Silero VAD даёт участки речи, мы склеиваем
ТОЛЬКО их в один массив (с вставкой 0.25 с тишины между участками, чтобы
pyannote не сшивал реплики разных людей), гоняем модель по склейке и
пересчитываем времена обратно в шкалу оригинальной записи. На обычном совещании
речи 60-75 % длительности, то есть экономим треть-половину времени.

Важные особенности API pyannote 4.x, проверенные на этой машине:
  * Pipeline.from_pretrained(..., token=...)  — аргумент называется token;
    при отказе доступа возвращает None, а не бросает исключение;
  * файлы подавать НЕЛЬЗЯ (torchcodec требует DLL FFmpeg 4-7, в системе
    FFmpeg 8 только как exe) — подаём словарь с waveform/sample_rate;
  * apply() возвращает DiarizeOutput, у него нет .itertracks; нужные поля —
    speaker_diarization, exclusive_speaker_diarization, speaker_embeddings.
    Старые модели (3.1) могут вернуть обычный Annotation — это учтено.
"""
from __future__ import annotations

import copy
import io
import json
import logging
import os
import threading
import time
import uuid
from typing import Any, Callable, Iterable, Sequence

import numpy as np

from . import config, store, vad

log = logging.getLogger("hagen.diarize")

SR = 16000

# Замеренный на этой машине RTF по ДЛИТЕЛЬНОСТИ РЕЧИ (не всей записи).
RTF = 0.63
# Накладные расходы: загрузка пайплайна, VAD, сборка результата.
OVERHEAD_S = 15.0
# Если доля речи неизвестна — считаем по типовому совещанию.
DEFAULT_SPEECH_RATIO = 0.75

# Меньше этого количества речи запускать pyannote бессмысленно.
MIN_SPEECH_S = 5.0
# Тишина между склеенными участками речи.
GAP_S = 0.25

# Веса шагов пайплайна для шкалы прогресса и подписи по-русски.
_STEPS: tuple[tuple[str, float, str], ...] = (
    ("segmentation", 0.35, "разметка речи"),
    ("speaker_counting", 0.10, "подсчёт говорящих"),
    ("embeddings", 0.45, "голосовые отпечатки"),
    ("discrete_diarization", 0.10, "сборка разметки"),
)

# Доля «чужой» речи внутри реплики (от размеченной речи реплики), после
# которой реплику надо разрезать по границам говорящих. Второе правило —
# длинный сам по себе кусок другого голоса, настройка split_min_piece_s.
SPLIT_RATIO = 0.35
# Короче этого куска резать не будем — иначе стенограмма рассыпается на слова.
MIN_PIECE_S = 0.7

_pipeline = None
_pipeline_model = ""
_pipeline_lock = threading.Lock()


class DiarizeError(RuntimeError):
    """Понятная пользователю ошибка диаризации."""


class DiarizeCancelled(RuntimeError):
    """Задачу отменили из интерфейса."""


# ---------------------------------------------------------------- готовность


def available() -> tuple[bool, str]:
    """Готова ли диаризация. Возвращает (готово, причина-по-русски)."""
    token = (config.get("hf_token") or "").strip()
    if not token:
        return (
            False,
            "Не задан токен Hugging Face. Откройте страницу модели "
            "pyannote/speaker-diarization-community-1, примите лицензию и создайте "
            "токен с правом чтения, затем вставьте его в настройках.",
        )
    try:
        import importlib.util

        if importlib.util.find_spec("pyannote.audio") is None:
            return False, "Не установлен пакет pyannote.audio."
    except Exception:
        # Проверка необязательная: если не смогли заглянуть — пробуем работать.
        pass
    model = config.get("diarize_model") or ""
    return True, "Токен задан, модель %s." % (model or "по умолчанию")


def _safe_err(err: Any) -> str:
    """Текст ошибки без токена: он не должен попасть ни в лог, ни в интерфейс."""
    text = str(err)
    token = (config.get("hf_token") or "").strip()
    if token and len(token) >= 8:
        text = text.replace(token, "<токен скрыт>")
    return text


def _threads() -> int:
    try:
        return max(1, min(16, int(config.get("diarize_threads") or 10)))
    except Exception:
        return 10


def _notify(progress: Callable | None, value: float, note: str) -> None:
    """Аккуратно дёрнуть колбэк прогресса, каким бы он ни был."""
    if progress is None:
        return
    try:
        progress(value, note)
    except TypeError:
        try:
            progress(value)
        except Exception:
            pass
    except Exception:
        pass


# ---------------------------------------------------------------- пайплайн


def _set_window_step(pipe: Any, step_s: float) -> float:
    """Сдвигать окно разметки реже. Возвращает прежний шаг (0 — не вышло).

    Модель смотрит на звук окном в 10 секунд и по умолчанию сдвигает его на
    1 секунду — то есть каждый кусок звука обсчитывается десять раз внахлёст.
    Удвоив шаг, мы вдвое сокращаем число окон, а вместе с ним и работу ОБОИХ
    тяжёлых шагов: поиска речи и подсчёта отпечатков (отпечаток считается на
    каждое окно).

    Чем платим: границы «один замолчал, другой начал» становятся грубее, и
    короткие реплики в одно слово могут потеряться. Для совещания это обычно
    терпимо, для интервью с перебиваниями — нет. Поэтому шаг вынесен в
    настройку `diarize_window_step_s`: 0 — как у модели, откат мгновенный.
    """
    seg = getattr(pipe, "_segmentation", None)
    if seg is None or not hasattr(seg, "step"):
        log.info("шаг окна поменять не вышло: у пайплайна нет _segmentation.step")
        return 0.0
    try:
        was = float(getattr(seg, "step", 0.0) or 0.0)
        duration = float(getattr(seg, "duration", 0.0) or 0.0)
        step = max(0.1, float(step_s))
        if duration and step > duration:
            step = duration            # шаг больше окна — дыры в разметке
        seg.step = step
        if duration:
            # у пайплайна шаг хранится долей окна — держим оба поля согласованными
            pipe.segmentation_step = step / duration
    except (TypeError, ValueError, AttributeError) as err:
        log.info("шаг окна поменять не вышло: %s", err)
        return 0.0
    log.info("шаг окна разметки: %.2f с вместо %.2f с (окно %.1f с)",
             step, was, duration)
    return was


def _speed_up_embeddings(pipe: Any) -> bool:
    """Считать «ствол» сети отпечатков один раз на окно, а не на каждый голос.

    Где теряется время. Разметку почти целиком занимает построение отпечатков
    голоса: на замере это 97 % (26,4 с из 27,2 с). Запись режется на окна по
    10 секунд с шагом в секунду, и pyannote подаёт КАЖДОЕ окно в свёрточную
    сеть отдельно на каждое из трёх мест для голоса — то есть один и тот же
    звук считается трижды. Между собой эти три прогона отличаются только
    маской, а маска влияет лишь на последний шаг (усреднение по кадрам), и он
    стоит меньше процента.

    Что делаем. Подаём окно в сеть один раз, получаем покадровые признаки и
    усредняем их сразу по всем трём маскам. Оба нужных метода
    (forward_frames и forward_embedding) — публичная часть pyannote, и второй
    штатно принимает веса формы (окна, голоса, кадры).

    Замер: 91,3 с → 31,8 с, отпечатки совпали с прежними до 1e-07, число
    найденных голосов то же. Если что-то в устройстве pyannote не совпало с
    ожиданиями — ничего не подменяем и работаем как раньше.
    """
    import math
    import types

    try:
        import numpy as np
        import torch
        from pyannote.core import SlidingWindowFeature
    except Exception as err:
        log.info("ускорение отпечатков недоступно: %s", err)
        return False

    emb = getattr(pipe, "_embedding", None)
    model = getattr(emb, "model_", None)
    if emb is None or model is None:
        log.info("ускорение отпечатков: не нашёл модель, оставляю как было")
        return False
    if not (callable(getattr(model, "forward_frames", None))
            and callable(getattr(model, "forward_embedding", None))):
        log.info("ускорение отпечатков: нет нужных методов, оставляю как было")
        return False
    if not hasattr(pipe, "get_embeddings"):
        return False

    def fast_get_embeddings(self, file, binary_segmentations,
                            exclude_overlap: bool = False, hook=None):
        duration = binary_segmentations.sliding_window.duration
        num_chunks, num_frames, num_speakers = binary_segmentations.data.shape

        # Отбор «чистой» речи повторяет оригинал: если непересекающейся речи
        # слишком мало, берём маску целиком.
        if exclude_overlap:
            min_num_samples = emb.min_num_samples
            num_samples = duration * emb.sample_rate
            min_num_frames = math.ceil(num_frames * min_num_samples / num_samples)
            clean_frames = 1.0 * (
                np.sum(binary_segmentations.data, axis=2, keepdims=True) < 2
            )
            clean_data = binary_segmentations.data * clean_frames
        else:
            min_num_frames = -1
            clean_data = binary_segmentations.data
        clean_segmentations = SlidingWindowFeature(
            clean_data, binary_segmentations.sliding_window
        )

        # Батч теперь считается в ОКНАХ, а не в парах «окно и голос».
        per_batch = max(1, int(getattr(self, "embedding_batch_size", 32)) // max(1, num_speakers))
        batch_count = math.ceil(num_chunks / per_batch)
        if hook is not None:
            hook("embeddings", None, total=batch_count, completed=0)

        out = np.empty((num_chunks, num_speakers, emb.dimension), dtype=np.float32)
        waves: list[torch.Tensor] = []
        masks: list[torch.Tensor] = []
        where: list[int] = []
        batches_done = 0

        def run_batch() -> None:
            nonlocal waves, masks, where, batches_done
            if not waves:
                return
            wf = torch.vstack(waves).to(emb.device)             # (окна, 1, отсчёты)
            mk = torch.stack(masks).to(emb.device)              # (окна, голоса, кадры)
            with torch.inference_mode():
                frames = model.forward_frames(wf)               # ствол — ОДИН раз на окно
                vecs = model.forward_embedding(frames, weights=mk)
            arr = vecs.cpu().numpy()
            if arr.ndim == 2:                                   # один голос на окно
                arr = arr[:, None, :]
            for row, chunk_i in enumerate(where):
                out[chunk_i] = arr[row]
            batches_done += 1
            if hook is not None:
                hook("embeddings", arr, total=batch_count, completed=batches_done)
            waves, masks, where = [], [], []

        pairs = zip(binary_segmentations, clean_segmentations)
        for chunk_i, ((chunk, chunk_masks), (_, chunk_clean)) in enumerate(pairs):
            waveform, _ = self._audio.crop(file, chunk, mode="pad")
            chunk_masks = np.nan_to_num(chunk_masks, nan=0.0).astype(np.float32)
            chunk_clean = np.nan_to_num(chunk_clean, nan=0.0).astype(np.float32)

            used = np.empty_like(chunk_masks.T)                 # (голоса, кадры)
            for k, (mask, clean) in enumerate(zip(chunk_masks.T, chunk_clean.T)):
                used[k] = clean if np.sum(clean) > min_num_frames else mask

            waves.append(waveform[None])
            masks.append(torch.from_numpy(used))
            where.append(chunk_i)
            if len(waves) >= per_batch:
                run_batch()
        run_batch()
        return out

    pipe._hagen_slow_get_embeddings = pipe.get_embeddings
    pipe.get_embeddings = types.MethodType(fast_get_embeddings, pipe)
    log.info("отпечатки голоса: включён быстрый путь (ствол один раз на окно)")
    return True


def _allow_network_once():
    """Временно разрешить обращение к сети за моделью.

    Приложение работает с моделями с диска (config ставит HF_HUB_OFFLINE) — это
    и быстрее, и не плодит окна антивируса. Но если модель в кэше не нашлась,
    например её сменили в настройках, за ней надо всё-таки сходить. Переменную
    окружения менять поздно: huggingface_hub читает её один раз при импорте,
    поэтому переключаем сам флажок в библиотеке и возвращаем как было.
    """
    import contextlib

    @contextlib.contextmanager
    def _ctx():
        try:
            from huggingface_hub import constants as hf_constants
        except Exception:
            yield
            return
        was = getattr(hf_constants, "HF_HUB_OFFLINE", False)
        hf_constants.HF_HUB_OFFLINE = False
        try:
            yield
        finally:
            hf_constants.HF_HUB_OFFLINE = was

    return _ctx()


def _setting(key: str) -> float | None:
    """Число из настроек; пусто или мусор — None («как у модели»)."""
    raw = config.get(key)
    if raw in (None, ""):
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _remember_base(pipe: Any) -> None:
    """Запомнить заводские шаг окна, параметры группировки и фильтр отпечатков."""
    if getattr(pipe, "_hagen_base", None) is not None:
        return
    seg = getattr(pipe, "_segmentation", None)
    base: dict[str, Any] = {
        "step": float(getattr(seg, "step", 0.0) or 0.0) if seg is not None else 0.0,
        "duration": float(getattr(seg, "duration", 0.0) or 0.0) if seg is not None else 0.0,
        "params": None,
        "filter": None,
    }
    try:
        base["params"] = copy.deepcopy(pipe.parameters(instantiated=True))
    except Exception as err:
        log.info("параметры модели разметки не прочитались: %s", err)
    clus = getattr(pipe, "clustering", None)
    if clus is not None and callable(getattr(clus, "filter_embeddings", None)):
        base["filter"] = clus.filter_embeddings
    pipe._hagen_base = base


def apply_tuning(pipe: Any) -> dict[str, Any]:
    """Применить продвинутые настройки разметки. Зовётся перед каждой разметкой.

    Пустое поле — заводское значение модели. Очередь тяжёлых задач
    однопоточная, поэтому менять общий пайплайн между разметками безопасно.
    """
    import functools

    base = getattr(pipe, "_hagen_base", None) or {}
    info: dict[str, Any] = {}
    seg = getattr(pipe, "_segmentation", None)

    step = _setting("diarize_window_step_s")
    want = step if step and step > 0 else float(base.get("step") or 0.0)
    if seg is not None and want > 0 and abs(float(getattr(seg, "step", 0.0) or 0.0) - want) > 1e-6:
        _set_window_step(pipe, want)
    if seg is not None:
        info["step"] = round(float(getattr(seg, "step", 0.0) or 0.0), 3)

    params = base.get("params")
    if isinstance(params, dict) and isinstance(params.get("clustering"), dict):
        wanted = copy.deepcopy(params)
        cl = wanted["clustering"]
        thr = _setting("diarize_cluster_threshold")
        if thr is not None and "threshold" in cl:
            cl["threshold"] = min(max(thr, 0.05), 2.0)
        fb = _setting("diarize_cluster_fb")
        if fb is not None and "Fb" in cl:
            cl["Fb"] = min(max(fb, 0.01), 50.0)
        try:
            now = pipe.parameters(instantiated=True)
        except Exception:
            now = None
        if now != wanted:
            pipe.instantiate(wanted)
        info.update({k: cl[k] for k in ("threshold", "Fb") if k in cl})

    clus = getattr(pipe, "clustering", None)
    filt = base.get("filter")
    if clus is not None and filt is not None:
        sec = _setting("diarize_min_voice_s")
        window = float(base.get("duration") or 10.0)
        if sec is None or sec <= 0:
            clus.filter_embeddings = filt
            info["min_voice_s"] = None
        else:
            ratio = min(max(sec / window, 0.01), 1.0)
            clus.filter_embeddings = functools.partial(filt, min_active_ratio=ratio)
            info["min_voice_s"] = round(ratio * window, 2)

    log.info("настройки разметки: %s", info)
    return info


def _load_one(model_name: str, token: str):
    """Одна попытка загрузки. Бросает исключение или возвращает пайплайн."""
    import torch

    from pyannote.audio import Pipeline

    torch.set_num_threads(_threads())
    try:
        pipe = Pipeline.from_pretrained(model_name, token=token)
    except Exception as err:
        log.info("модели %s нет на диске (%s), иду за ней в сеть",
                 model_name, _safe_err(err)[:120])
        with _allow_network_once():
            pipe = Pipeline.from_pretrained(model_name, token=token)
    if pipe is None:
        raise DiarizeError(
            "Hugging Face не отдал модель %s. Откройте её страницу, примите "
            "лицензию и создайте токен с правом чтения." % model_name
        )
    pipe.to(torch.device("cpu"))
    # Заводские значения модели — до любых правок: пустое поле в настройках
    # возвращает именно их. Шаг окна и остальное применяет apply_tuning.
    _remember_base(pipe)
    apply_tuning(pipe)
    # Подмену делаем ПОСЛЕ .to(): быстрый путь берёт устройство у обёртки.
    if config.get("fast_embeddings", True):
        try:
            _speed_up_embeddings(pipe)
        except Exception:
            log.warning("ускорение отпечатков не включилось, работаю обычным путём",
                        exc_info=True)
    return pipe


def load_pipeline(progress: Callable | None = None):
    """Загрузить и закэшировать пайплайн (singleton под блокировкой)."""
    global _pipeline, _pipeline_model

    with _pipeline_lock:
        if _pipeline is not None:
            return _pipeline

        ok, reason = available()
        if not ok:
            raise DiarizeError(reason)

        token = (config.get("hf_token") or "").strip()
        primary = (config.get("diarize_model") or "").strip()
        fallback = (config.get("diarize_fallback_model") or "").strip()

        candidates = [m for m in (primary, fallback) if m]
        if not candidates:
            raise DiarizeError("В настройках не указана модель диаризации.")

        errors: list[str] = []
        for i, name in enumerate(candidates):
            _notify(progress, 0.0, "загрузка модели %s" % name)
            t0 = time.time()
            try:
                pipe = _load_one(name, token)
            except Exception as err:
                # Текст ошибки от huggingface_hub может содержать токен —
                # прогоняем его через маскировку и в лог, и в сообщение.
                msg = _safe_err(err)
                errors.append("%s: %s" % (name, msg))
                log.warning("модель %s не загрузилась: %s", name, msg)
                if i + 1 < len(candidates):
                    log.info("пробую резервную модель %s", candidates[i + 1])
                continue
            _pipeline = pipe
            _pipeline_model = name
            log.info(
                "диаризация: взята модель %s, потоков %d, загрузка %.1f c",
                name, _threads(), time.time() - t0,
            )
            _notify(progress, 0.0, "модель %s загружена" % name)
            return _pipeline

        raise DiarizeError(
            "Не удалось загрузить ни одну модель диаризации. Нужно принять "
            "лицензию на странице модели и создать токен Hugging Face с правом "
            "чтения. Подробности: " + "; ".join(errors)
        )


def pipeline_model() -> str:
    """Имя реально загруженной модели (пусто, пока не загружали)."""
    return _pipeline_model


#: Короче этого куска отпечаток голоса не считается: модели нужен хоть какой-то
#: кусок речи, на обрывке в четверть секунды получается шум.
MIN_EMB_S = 0.6


def embed_spans(pcm: Any, spans: Sequence[dict[str, float]], sr: int = 16000,
                progress: Callable | None = None) -> list[Any]:
    """Отпечаток голоса для каждого куска речи. Пусто там, где кусок короток.

    Берётся та же модель отпечатков, что и у разметки (`pipe._embedding`), —
    отдельную не грузим. Считается по одному куску: их обычно десятки, а
    длины разные, и городить подгонку под общий размер ради долей секунды
    незачем.

    Нужно для отсева эха колонок по голосу: реплика микрофона, похожая не на
    владельца, а на собеседника, — это эхо, и никакой текст сверять не надо
    (решение 17.09).
    """
    import numpy as _np
    import torch as _torch

    pipe = load_pipeline(progress)
    emb = getattr(pipe, "_embedding", None)
    if emb is None:
        raise DiarizeError("У модели разметки нет части, считающей отпечатки голоса.")
    data = _np.asarray(pcm, dtype=_np.float32).reshape(-1)
    out: list[Any] = []
    least = int(MIN_EMB_S * sr)
    for span in spans:
        a = max(0, int(float(span["start"]) * sr))
        b = min(data.shape[0], int(float(span["end"]) * sr))
        if b - a < least:
            out.append(None)
            continue
        wave = _torch.from_numpy(data[a:b].copy()).reshape(1, 1, -1)
        try:
            with _torch.inference_mode():
                vec = emb(wave)
        except Exception as err:                      # noqa: BLE001
            log.debug("отпечаток куска %.1f–%.1f не посчитался: %s",
                      span["start"], span["end"], err)
            out.append(None)
            continue
        arr = _np.asarray(vec, dtype=_np.float32).reshape(-1)
        out.append(None if not _np.isfinite(arr).all() else arr.tolist())
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


def _annotation_turns(ann: Any) -> list[dict[str, Any]]:
    """Annotation -> список интервалов. Терпим к отсутствию объекта."""
    if ann is None:
        return []
    out: list[dict[str, Any]] = []
    try:
        for segment, _track, label in ann.itertracks(yield_label=True):
            out.append({
                "start": float(segment.start),
                "end": float(segment.end),
                "speaker": str(label),
            })
    except Exception as err:
        log.warning("не смог разобрать разметку pyannote: %s", err)
        return []
    out.sort(key=lambda r: (r["start"], r["end"]))
    return out


def _split_output(out: Any) -> tuple[Any, Any, Any]:
    """Достать (diarization, exclusive, embeddings) из ответа pyannote.

    Модель community-1 возвращает DiarizeOutput, старая 3.1 — обычный Annotation.
    """
    ann = getattr(out, "speaker_diarization", None)
    if ann is None:
        # Старое поведение: сам объект и есть Annotation.
        return out, None, None
    return (
        ann,
        getattr(out, "exclusive_speaker_diarization", None),
        getattr(out, "speaker_embeddings", None),
    )


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
    """Переводит шаги pyannote в шкалу 0..1 и считает остаток времени."""

    def __init__(self, handle: Any, t0: float):
        self.handle = handle
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
        if self.handle is None:
            return
        if getattr(self.handle, "cancelled", False):
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
        if self.handle is None:
            return
        try:
            self.handle.progress(self.value, note)
        except Exception:
            pass
        elapsed = time.time() - self.t0
        if self.value > 0.03 and elapsed > 1.0:
            eta = max(0.0, elapsed / self.value - elapsed)
            try:
                self.handle.eta(eta)
            except Exception:
                pass


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

    t0 = time.time()
    arr = np.ascontiguousarray(np.asarray(pcm, dtype=np.float32).reshape(-1))
    if int(sr) != SR:
        arr = np.ascontiguousarray(audio_io.resample(arr, int(sr), SR).astype(np.float32))
    total = int(arr.shape[0])
    duration_s = total / float(SR)

    empty: dict[str, Any] = {
        "turns": [], "exclusive_turns": [], "labels": [], "embeddings": {},
        "speech_seconds": 0.0, "elapsed_s": 0.0, "rtf": 0.0,
        "model": _pipeline_model or (config.get("diarize_model") or ""),
        "duration_s": round(duration_s, 3),
    }
    if total <= 0:
        return empty

    # --- шаг 1: где вообще речь
    if handle is not None:
        try:
            handle.progress(0.0, "поиск речи")
        except Exception:
            pass
    try:
        spans = vad.speech_timestamps(arr)
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
    pipe = load_pipeline(progress=(lambda v, n: _handle_note(handle, n)))
    apply_tuning(pipe)      # настройки могли поменять после загрузки модели
    if handle is not None and getattr(handle, "cancelled", False):
        raise DiarizeCancelled("Диаризация отменена")

    glued = _glue(arr, pieces, glued_len)

    # --- шаг 3: pyannote по склейке
    import torch

    torch.set_num_threads(_threads())
    payload = {
        "waveform": torch.from_numpy(np.ascontiguousarray(glued)).unsqueeze(0),
        "sample_rate": SR,
    }
    kwargs: dict[str, Any] = {}
    if min_speakers is not None:
        kwargs["min_speakers"] = int(min_speakers)
    if max_speakers is not None:
        kwargs["max_speakers"] = int(max_speakers)

    hook = _Progress(handle, time.time()) if handle is not None else None
    t_apply = time.time()
    try:
        out = pipe.apply(payload, hook=hook, **kwargs)
    except DiarizeCancelled:
        raise
    except TypeError as err:
        # Некоторые версии не принимают hook; ограничители числа голосов
        # бросаем только вторым шагом, иначе молча потеряем min/max_speakers.
        log.warning("apply не принял hook (%s), повторяю без прогресса", _safe_err(err))
        try:
            out = pipe.apply(payload, **kwargs)
        except DiarizeCancelled:
            raise
        except TypeError as err2:
            log.warning(
                "apply не принял ограничители числа голосов (%s), размечаю без них",
                _safe_err(err2),
            )
            out = pipe.apply(payload)
    apply_s = time.time() - t_apply

    ann, exclusive, emb = _split_output(out)
    labels: list[str] = []
    try:
        labels = [str(x) for x in ann.labels()]
    except Exception:
        labels = sorted({t["speaker"] for t in _annotation_turns(ann)})

    turns = _map_back(_annotation_turns(ann), pieces, SR, duration_s)
    excl = _map_back(_annotation_turns(exclusive), pieces, SR, duration_s) if exclusive is not None else []

    elapsed = time.time() - t0
    result = {
        "turns": turns,
        "exclusive_turns": excl,
        "labels": labels,
        "embeddings": _embeddings_map(labels, emb),
        "speech_seconds": round(speech_s, 3),
        "duration_s": round(duration_s, 3),
        "elapsed_s": round(elapsed, 2),
        "rtf": round(apply_s / max(1e-6, speech_s), 3),
        "model": _pipeline_model or (config.get("diarize_model") or ""),
    }
    log.info(
        "диаризация готова: голосов %d, интервалов %d, %.1f c (RTF %.2f)",
        len(labels), len(turns), elapsed, result["rtf"],
    )
    if handle is not None:
        try:
            handle.progress(1.0, "разметка готова")
            handle.eta(0.0)
        except Exception:
            pass
    return result


def _handle_note(handle: Any, note: str) -> None:
    if handle is None:
        return
    try:
        handle.log(note)
    except Exception:
        pass


def estimate_seconds(pcm_or_duration: Any, speech_ratio: float | None = None) -> float:
    """Оценка времени разметки: RTF 0.63 от ДЛИТЕЛЬНОСТИ РЕЧИ плюс накладные.

    pcm_or_duration — либо длительность в секундах, либо массив 16 кГц.
    speech_ratio — доля речи 0..1 (если не передать, берём типовые 0.75).
    """
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
    return round(speech_s * RTF + OVERHEAD_S, 1)


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


def _usable_words(
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
        words = _usable_words(seg.get("words"), s0, s1)
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
