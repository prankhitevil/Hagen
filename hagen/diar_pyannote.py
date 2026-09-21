# -*- coding: utf-8 -*-
"""Разметка говорящих через pyannote.audio 4.x — движок релиза с токеном.

Модель — `pyannote/speaker-diarization-community-1` с Hugging Face (запасная —
3.1). Доступ к ней закрыт условиями, поэтому нужен токен. Тяжёлые зависимости
(torch, pyannote) импортируются только при первом обращении, чтобы служба
стартовала быстро. Склейку речи, пересчёт времён и всё остальное, что от
движка не зависит, делает diarize.py.

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
import logging
import threading
import time
from typing import Any, Callable

import numpy as np

from . import config

log = logging.getLogger("hagen.diarize")

NAME = "pyannote"
NEEDS_TOKEN = True

SR = 16000

# Замеренный на этой машине RTF по ДЛИТЕЛЬНОСТИ РЕЧИ (не всей записи).
RTF = 0.63
# Накладные расходы: загрузка пайплайна, VAD, сборка результата.
OVERHEAD_S = 15.0

_pipeline = None
_pipeline_model = ""
_pipeline_lock = threading.Lock()
_threads_used = 4


# ---------------------------------------------------------------- готовность


def installed() -> bool:
    """Стоит ли пакет pyannote.audio.

    find_spec("pyannote.audio") без пакета не возвращает None, а падает: сначала
    ищется родитель pyannote. В релизе ONNX пакета нет вовсе.
    """
    import importlib.util

    try:
        return importlib.util.find_spec("pyannote.audio") is not None
    except ModuleNotFoundError:
        return False


def ready() -> tuple[bool, str]:
    """Готова ли разметка. Возвращает (готово, причина-по-русски)."""
    if _pipeline is not None:
        # уже загруженной модели токен больше не нужен
        return True, "Модель %s загружена." % _pipeline_model
    token = (config.get("hf_token") or "").strip()
    if not token:
        return (
            False,
            "Не задан токен Hugging Face. Откройте страницу модели "
            "pyannote/speaker-diarization-community-1, примите лицензию и создайте "
            "токен с правом чтения, затем вставьте его в настройках.",
        )
    if not installed():
        return False, "Не установлен пакет pyannote.audio."
    model = config.get("diarize_model") or ""
    return True, "Токен задан, модель %s." % (model or "по умолчанию")


def _safe_err(err: Any) -> str:
    """Текст ошибки без токена: он не должен попасть ни в лог, ни в интерфейс."""
    text = str(err)
    token = (config.get("hf_token") or "").strip()
    if token and len(token) >= 8:
        text = text.replace(token, "<токен скрыт>")
    return text


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


def _positive(value: Any) -> float | None:
    """Число из ручки разметки; пусто или мусор — None («как у модели»)."""
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def apply_tuning(pipe: Any, tuning: dict | None) -> dict[str, Any]:
    """Применить ручки разметки. Зовётся перед каждой разметкой.

    tuning — словарь от diarize.tuning(): step_s, threshold, Fb, min_voice_s.
    Пустое значение — заводское значение модели. Очередь тяжёлых задач
    однопоточная, поэтому менять общий пайплайн между разметками безопасно.
    """
    import functools

    tuning = tuning or {}
    base = getattr(pipe, "_hagen_base", None) or {}
    info: dict[str, Any] = {}
    seg = getattr(pipe, "_segmentation", None)

    step = _positive(tuning.get("step_s"))
    want = step if step and step > 0 else float(base.get("step") or 0.0)
    if seg is not None and want > 0 and abs(float(getattr(seg, "step", 0.0) or 0.0) - want) > 1e-6:
        _set_window_step(pipe, want)
    if seg is not None:
        info["step"] = round(float(getattr(seg, "step", 0.0) or 0.0), 3)

    params = base.get("params")
    if isinstance(params, dict) and isinstance(params.get("clustering"), dict):
        wanted = copy.deepcopy(params)
        cl = wanted["clustering"]
        thr = _positive(tuning.get("threshold"))
        if thr is not None and "threshold" in cl:
            cl["threshold"] = min(max(thr, 0.05), 2.0)
        fb = _positive(tuning.get("Fb"))
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
        sec = _positive(tuning.get("min_voice_s"))
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


def _load_one(model_name: str, token: str, threads: int = 4):
    """Одна попытка загрузки. Бросает исключение или возвращает пайплайн."""
    import torch

    from pyannote.audio import Pipeline

    torch.set_num_threads(threads)
    try:
        pipe = Pipeline.from_pretrained(model_name, token=token)
    except Exception as err:
        log.info("модели %s нет на диске (%s), иду за ней в сеть",
                 model_name, _safe_err(err)[:120])
        with _allow_network_once():
            pipe = Pipeline.from_pretrained(model_name, token=token)
    if pipe is None:
        raise RuntimeError(
            "Hugging Face не отдал модель %s. Откройте её страницу, примите "
            "лицензию и создайте токен с правом чтения." % model_name
        )
    pipe.to(torch.device("cpu"))
    # Заводские значения модели — до любых правок: пустое поле в настройках
    # возвращает именно их. Шаг окна и остальное применяет apply_tuning.
    _remember_base(pipe)
    # Подмену делаем ПОСЛЕ .to(): быстрый путь берёт устройство у обёртки.
    if config.get("fast_embeddings", True):
        try:
            _speed_up_embeddings(pipe)
        except Exception:
            log.warning("ускорение отпечатков не включилось, работаю обычным путём",
                        exc_info=True)
    return pipe


def use_pipeline(pipe: Any, name: str) -> None:
    """Поставить готовый пайплайн — для проверок, которые грузят модель сами."""
    global _pipeline, _pipeline_model
    with _pipeline_lock:
        _pipeline, _pipeline_model = pipe, name


def load(threads: int = 4, progress: Callable | None = None):
    """Загрузить и закэшировать пайплайн (singleton под блокировкой)."""
    global _pipeline, _pipeline_model, _threads_used

    _threads_used = max(1, int(threads))
    with _pipeline_lock:
        if _pipeline is not None:
            return _pipeline

        ok, reason = ready()
        if not ok:
            raise RuntimeError(reason)

        token = (config.get("hf_token") or "").strip()
        primary = (config.get("diarize_model") or "").strip()
        fallback = (config.get("diarize_fallback_model") or "").strip()

        candidates = [m for m in (primary, fallback) if m]
        if not candidates:
            raise RuntimeError("В настройках не указана модель диаризации.")

        errors: list[str] = []
        for i, name in enumerate(candidates):
            _notify(progress, 0.0, "загрузка модели %s" % name)
            t0 = time.time()
            try:
                pipe = _load_one(name, token, _threads_used)
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
                name, _threads_used, time.time() - t0,
            )
            _notify(progress, 0.0, "модель %s загружена" % name)
            return _pipeline

        raise RuntimeError(
            "Не удалось загрузить ни одну модель диаризации. Нужно принять "
            "лицензию на странице модели и создать токен Hugging Face с правом "
            "чтения. Подробности: " + "; ".join(errors)
        )


def model_name() -> str:
    """Имя реально загруженной модели; до загрузки — из настроек."""
    return _pipeline_model or (config.get("diarize_model") or "")


def tuning_defaults() -> dict[str, float | None]:
    """Заводские значения ручек — у загруженной модели; до загрузки неизвестны."""
    base = getattr(_pipeline, "_hagen_base", None) or {}
    cl = (base.get("params") or {}).get("clustering") or {}
    return {"step_s": base.get("step"), "threshold": cl.get("threshold"),
            "Fb": cl.get("Fb"), "min_voice_s": None}


# ---------------------------------------------------------------- разметка


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


def run(pcm: np.ndarray, min_speakers: int | None = None, max_speakers: int | None = None,
        tuning: dict | None = None, hook: Callable | None = None) -> dict[str, Any]:
    """Разметить склейку речи. pcm — float32 моно 16 кГц.

    Возвращает turns, exclusive_turns, labels, centroids — в шкале склейки.
    """
    import torch

    pipe = load(_threads_used)
    apply_tuning(pipe, tuning)
    torch.set_num_threads(_threads_used)
    payload = {
        "waveform": torch.from_numpy(np.ascontiguousarray(pcm, dtype=np.float32)).unsqueeze(0),
        "sample_rate": SR,
    }
    kwargs: dict[str, Any] = {}
    if min_speakers is not None:
        kwargs["min_speakers"] = int(min_speakers)
    if max_speakers is not None:
        kwargs["max_speakers"] = int(max_speakers)

    try:
        out = pipe.apply(payload, hook=hook, **kwargs)
    except TypeError as err:
        # Некоторые версии не принимают hook; ограничители числа голосов
        # бросаем только вторым шагом, иначе молча потеряем min/max_speakers.
        log.warning("apply не принял hook (%s), повторяю без прогресса", _safe_err(err))
        try:
            out = pipe.apply(payload, **kwargs)
        except TypeError as err2:
            log.warning(
                "apply не принял ограничители числа голосов (%s), размечаю без них",
                _safe_err(err2),
            )
            out = pipe.apply(payload)

    ann, exclusive, emb = _split_output(out)
    turns = _annotation_turns(ann)
    try:
        labels = [str(x) for x in ann.labels()]
    except Exception:
        labels = sorted({t["speaker"] for t in turns})
    return {
        "turns": turns,
        "exclusive_turns": _annotation_turns(exclusive) if exclusive is not None else [],
        "labels": labels,
        "centroids": emb,
    }


def embed(wave: np.ndarray) -> np.ndarray | None:
    """Отпечаток всего куска звука — та же часть модели, что считает отпечатки мест."""
    import torch

    pipe = load(_threads_used)
    emb = getattr(pipe, "_embedding", None)
    if emb is None:
        raise RuntimeError("У модели разметки нет части, считающей отпечатки голоса.")
    data = np.asarray(wave, dtype=np.float32).reshape(1, 1, -1)
    with torch.inference_mode():
        vec = emb(torch.from_numpy(data.copy()))
    arr = np.asarray(vec, dtype=np.float32).reshape(-1)
    return arr if np.isfinite(arr).all() else None
