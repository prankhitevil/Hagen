# -*- coding: utf-8 -*-
"""Определение речи и паузы через Silero VAD.

Два режима:
  StreamingPhraseDetector — для живого эфира. Держит фразу, пока пауза короче
      порога (по умолчанию 2,2 с), адаптируется к фоновому шуму.
  speech_timestamps / split_for_asr — для готовых файлов: нарезка по паузам
      на куски не длиннее 24 с (у модели распознавания предел 25 с).

Разрез без паузы. Когда человек говорит дольше предела без паузы, речь приходится
резать прямо посреди звука — часто посреди слова: первый кусок кончается на
«организован…», второй начинается с «…ное», и модель, не слыша начала, пишет
похожее настоящее слово. Поэтому в таком месте кусок после разреза начинается
на OVERLAP_S раньше разреза: слово на стыке целиком попадает во второй кусок, а
распознавание сшивает куски по времени слов (asr.transcribe_spans, живая
запись — live.py). Где разрез пришёлся на паузу, нахлёста нет.
"""
from __future__ import annotations

import threading
from typing import Any

import numpy as np

SR = 16000
FRAME = 512           # модель Silero v5 требует ровно 512 отсчётов на 16 кГц
FRAME_MS = FRAME * 1000.0 / SR   # 32 мс

#: Нахлёст кусков в месте разреза без паузы, секунды. Слово дольше полутора
#: секунд — редкость, а распознавать полторы секунды дважды почти ничего не стоит.
OVERLAP_S = 1.5
#: Стык короче этого — разрез без паузы: поиск речи упёрся в предел длины.
_JOINT_GAP_S = 0.05

_model = None
_model_lock = threading.Lock()


def new_model(onnx: bool = True):
    """Отдельный экземпляр модели.

    КРИТИЧНО: модель Silero держит внутри состояние рекуррентной сети, поэтому
    один экземпляр нельзя использовать из двух потоков одновременно — состояние
    дорожек затирается, а onnxruntime при этом падает без трассировки. У каждой
    дорожки должен быть свой экземпляр: он занимает около 2 МБ, это ничто.
    """
    from silero_vad import load_silero_vad

    try:
        return load_silero_vad(onnx=onnx)
    except Exception:
        return load_silero_vad(onnx=False)


def get_model(onnx: bool = True):
    """Общий экземпляр — только для однопоточных офлайн-задач."""
    global _model
    with _model_lock:
        if _model is None:
            _model = new_model(onnx=onnx)
        return _model


def _probs(frames: np.ndarray, model) -> np.ndarray:
    """Вероятности речи для матрицы кадров [n, 512]."""
    import torch

    with torch.no_grad():
        out = []
        for i in range(frames.shape[0]):
            t = torch.from_numpy(frames[i])
            out.append(float(model(t, SR).item()))
    return np.asarray(out, dtype=np.float32)


class StreamingPhraseDetector:
    """Решает, когда фраза закончилась.

    Правило из ТЗ: короткие паузы внутри речи фразу не рвут, закрепляем только
    после ~2,0-2,5 с непрерывной тишины. Порог не фиксированный: подстраивается
    под уровень фонового шума, иначе тихий шум в комнате читается как речь.
    """

    def __init__(
        self,
        silence_ms: int = 2200,
        max_phrase_s: float = 24.0,
        base_threshold: float = 0.50,
        onnx: bool = True,
        start_sample: int = 0,
        overlap_s: float = 0.0,
    ):
        # свой экземпляр модели на дорожку: состояние не должно пересекаться
        self.model = new_model(onnx=onnx)
        self.silence_frames_needed = max(1, int(round(silence_ms / FRAME_MS)))
        self.max_phrase_frames = max(1, int(round(max_phrase_s * 1000.0 / FRAME_MS)))
        # Нахлёст при разрезе на пределе длины (см. шапку модуля). Ноль — как
        # раньше: у быстрой модели нет времени слов, сшить фразы ей нечем.
        self.overlap_frames = max(0, int(round(float(overlap_s) * 1000.0 / FRAME_MS)))
        self.overlap_frames = min(self.overlap_frames, self.max_phrase_frames // 2)
        self.base_threshold = float(base_threshold)

        self._tail = np.zeros(0, dtype=np.float32)
        # Номер следующего кадра. Не всегда ноль: если дорожка продолжается
        # (в заметке уже были «Старт» и «Стоп»), счёт начинается с того места,
        # где остановились, — иначе время новых реплик наложилось бы на старые.
        self._frame_idx = max(0, int(start_sample) // FRAME)
        self._in_speech = False
        self._speech_start_frame = self._frame_idx
        self._last_speech_frame = self._frame_idx
        self._silence_run = 0
        self._speech_run = 0
        # адаптация: вероятности, увиденные в подтверждённой тишине
        self._noise_probs: list[float] = []
        self._noise_cap = 240        # ~7,7 с истории
        self.threshold = self.base_threshold
        self.last_prob = 0.0

    # -------------------------------------------------- адаптивный порог
    def _update_threshold(self, prob: float) -> None:
        self._noise_probs.append(float(prob))
        if len(self._noise_probs) > self._noise_cap:
            del self._noise_probs[: len(self._noise_probs) - self._noise_cap]
        if len(self._noise_probs) >= 30:
            floor = float(np.percentile(np.asarray(self._noise_probs), 90.0))
            # порог всегда выше шумового пола, но в разумных пределах
            self.threshold = float(min(0.85, max(0.35, max(self.base_threshold, floor + 0.25))))
        else:
            self.threshold = self.base_threshold

    def reset(self) -> None:
        self._in_speech = False
        self._silence_run = 0
        self._speech_run = 0
        try:
            self.model.reset_states()
        except Exception:
            pass

    def jump_to(self, sample: int) -> None:
        """Перескочить вперёд, не разбирая звук: перерыв в записи залит тишиной.

        Гонять через модель минуты нулей незачем — это секунды процессора прямо
        во время разговора. Незакрытую фразу вызывающий дописывает ДО прыжка:
        здесь она забывается.
        """
        self.reset()
        self._tail = np.zeros(0, dtype=np.float32)
        self._frame_idx = max(self._frame_idx, int(sample) // FRAME)
        self._speech_start_frame = self._frame_idx
        self._last_speech_frame = self._frame_idx

    # -------------------------------------------------- основной вход
    def feed(self, pcm: np.ndarray) -> list[dict[str, Any]]:
        """Скормить очередной кусок 16 кГц float32. Возвращает события.

        Событие: {"type": "speech_start"|"phrase_end", "start": отсчёт, "end": отсчёт}
        Отсчёты абсолютные, от начала дорожки.
        """
        a = np.asarray(pcm, dtype=np.float32).reshape(-1)
        if self._tail.size:
            a = np.concatenate([self._tail, a])
        n_frames = a.shape[0] // FRAME
        self._tail = np.ascontiguousarray(a[n_frames * FRAME:])
        if n_frames == 0:
            return []

        frames = np.ascontiguousarray(a[: n_frames * FRAME]).reshape(n_frames, FRAME)
        probs = _probs(frames, self.model)

        events: list[dict[str, Any]] = []
        hyst_low = None
        for k in range(n_frames):
            p = float(probs[k])
            self.last_prob = p
            idx = self._frame_idx + k
            thr = self.threshold
            hyst_low = thr * 0.65   # гистерезис: выходим из речи по более низкому порогу

            if not self._in_speech:
                self._update_threshold(p)
                if p >= thr:
                    self._speech_run += 1
                    if self._speech_run >= 2:      # 64 мс — не ловим щелчки
                        self._in_speech = True
                        self._speech_start_frame = max(0, idx - self._speech_run - 3)
                        self._last_speech_frame = idx
                        self._silence_run = 0
                        events.append({
                            "type": "speech_start",
                            "start": self._speech_start_frame * FRAME,
                        })
                else:
                    self._speech_run = 0
            else:
                if p >= hyst_low:
                    self._last_speech_frame = idx
                    self._silence_run = 0
                else:
                    self._silence_run += 1
                    if self._silence_run >= self.silence_frames_needed:
                        end_frame = self._last_speech_frame + 4   # небольшой хвост
                        events.append({
                            "type": "phrase_end",
                            "start": self._speech_start_frame * FRAME,
                            "end": (end_frame + 1) * FRAME,
                            "reason": "silence",
                        })
                        self._in_speech = False
                        self._speech_run = 0
                        self._silence_run = 0
                        continue
                # защита от бесконечной фразы: модель не берёт больше 25 с
                if idx - self._speech_start_frame >= self.max_phrase_frames:
                    cut = (idx + 1) * FRAME
                    event = {
                        "type": "phrase_end",
                        "start": self._speech_start_frame * FRAME,
                        "end": cut,
                        "reason": "too_long",
                    }
                    if self.overlap_frames:
                        # Разрез посреди речи: человек говорит дальше, и следующая
                        # фраза начинается на нахлёст раньше разреза — слово на
                        # стыке целиком попадёт в неё. Сшивает фразы live.py.
                        event["cut"] = cut
                        events.append(event)
                        self._speech_start_frame = max(self._speech_start_frame,
                                                       idx + 1 - self.overlap_frames)
                        self._last_speech_frame = idx
                        self._silence_run = 0
                        events.append({
                            "type": "speech_start",
                            "start": self._speech_start_frame * FRAME,
                            "joint": cut,
                        })
                        continue
                    events.append(event)
                    self._in_speech = False
                    self._speech_run = 0
                    self._silence_run = 0
                    self._speech_start_frame = idx + 1

        self._frame_idx += n_frames
        return events

    def pending_phrase(self) -> dict[str, Any] | None:
        """Незакрытая фраза — нужна при нажатии «Стоп»: её надо дописать."""
        if not self._in_speech:
            return None
        return {
            "type": "phrase_end",
            "start": self._speech_start_frame * FRAME,
            "end": (self._last_speech_frame + 4) * FRAME,
            "reason": "stop",
        }

    @property
    def in_speech(self) -> bool:
        return self._in_speech

    @property
    def silence_seconds(self) -> float:
        return self._silence_run * FRAME_MS / 1000.0


# ---------------------------------------------------------------- офлайн


def speech_timestamps(
    pcm: np.ndarray,
    max_speech_s: float = 24.0,
    min_silence_ms: int = 300,
    speech_pad_ms: int = 120,
    threshold: float = 0.5,
    progress=None,
) -> list[dict[str, float]]:
    """Участки речи в отсчётах: [{"start": n, "end": n}, ...]."""
    import torch

    from silero_vad import get_speech_timestamps

    model = get_model(onnx=False)   # офлайн точнее на torch-версии
    audio = torch.from_numpy(np.asarray(pcm, dtype=np.float32).reshape(-1))
    # общий экземпляр: одновременный вызов из двух потоков недопустим
    kwargs: dict[str, Any] = dict(
        threshold=threshold,
        sampling_rate=SR,
        min_speech_duration_ms=200,
        max_speech_duration_s=float(max_speech_s),
        min_silence_duration_ms=int(min_silence_ms),
        speech_pad_ms=int(speech_pad_ms),
        return_seconds=False,
    )
    if progress is not None:
        kwargs["progress_tracking_callback"] = progress
    with _model_lock:
        try:
            res = get_speech_timestamps(audio, model, **kwargs)
        except TypeError:
            kwargs.pop("progress_tracking_callback", None)
            res = get_speech_timestamps(audio, model, **kwargs)
    return [{"start": int(r["start"]), "end": int(r["end"])} for r in res]


def split_for_asr(
    pcm: np.ndarray, max_s: float = 24.0, merge_gap_s: float = 0.6,
    overlap_s: float = OVERLAP_S,
) -> list[dict[str, float]]:
    """Нарезка для распознавания: куски речи не длиннее max_s.

    Silero режет длинную речь по самой длинной внутренней паузе, а если паузы
    нет — прямо на пределе, посреди слова. Такие места помечаются, и кусок
    после разреза начинается на overlap_s раньше (mark_joints); чтобы и с
    нахлёстом кусок не вышел за max_s, Silero режет на overlap_s раньше
    предела. Ещё мы склеиваем соседние короткие участки, чтобы не гонять модель
    на каждое слово отдельно.
    """
    overlap_s = max(0.0, float(overlap_s))
    spans = speech_timestamps(pcm, max_speech_s=max_s - overlap_s)
    if not spans:
        return []
    max_n = int((max_s - overlap_s) * SR)
    gap_n = int(merge_gap_s * SR)
    out: list[dict[str, float]] = [dict(spans[0])]
    for sp in spans[1:]:
        last = out[-1]
        if sp["start"] - last["end"] <= gap_n and (sp["end"] - last["start"]) <= max_n:
            last["end"] = sp["end"]
        else:
            out.append(dict(sp))
    total = int(np.asarray(pcm).reshape(-1).shape[0])
    for sp in out:
        sp["start"] = max(0, int(sp["start"]))
        sp["end"] = min(total, int(sp["end"]))
    return mark_joints([sp for sp in out if sp["end"] > sp["start"]], overlap_s)


def mark_joints(spans: list[dict[str, Any]], overlap_s: float = OVERLAP_S) -> list[dict[str, Any]]:
    """Пометить разрезы без паузы и начать кусок после такого разреза раньше.

    У куска, кончившегося разрезом, — "cut": где был разрез (отсчёт); у
    следующего — "joint" (тот же отсчёт), а его "start" сдвинут на overlap_s
    назад, но не раньше начала предыдущего куска. Сшивает куски
    asr.transcribe_spans.
    """
    out = [dict(sp) for sp in spans]
    over = int(float(overlap_s) * SR)
    if over <= 0:
        return out
    gap = int(_JOINT_GAP_S * SR)
    for prev, cur in zip(out, out[1:]):
        if int(cur["start"]) - int(prev["end"]) <= gap:
            joint = int(cur["start"])
            prev["cut"] = joint
            cur["joint"] = joint
            cur["start"] = max(int(prev["start"]), joint - over)
    return out


def speech_ratio(pcm: np.ndarray) -> float:
    """Доля речи в записи — нужна для оценки времени диаризации."""
    total = int(np.asarray(pcm).reshape(-1).shape[0])
    if total <= 0:
        return 0.0
    spans = speech_timestamps(pcm)
    voiced = sum(sp["end"] - sp["start"] for sp in spans)
    return float(min(1.0, voiced / float(total)))
