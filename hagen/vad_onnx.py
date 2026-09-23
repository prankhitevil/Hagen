# -*- coding: utf-8 -*-
"""Модель Silero VAD на onnxruntime — без torch и без пакета silero-vad.

Здесь исполнитель поиска речи: сессия onnxruntime с состоянием рекуррентной
сети и правило нарезки записи на участки речи. Дверь для ядра — `vad.py`; сюда
из ядра не ходят.

Файл модели `models\\vad\\silero_vad.onnx` (2,2 МБ, MIT) лежит в репозитории,
как и сети разметки говорящих в `models\\diar\\`: поиск речи нужен при каждой
записи, и без сети программа должна записать встречу.

КРИТИЧНО про вход модели. Окно — 512 отсчётов на 16 кГц, но модель слушает его
ВМЕСТЕ с «хвостом» предыдущего окна в 64 отсчёта: на вход идёт 576 отсчётов
(`context` + `frame`). Без хвоста вероятности не имеют ничего общего с
нынешними: первая проба без него дала расхождение 0,75 и совпадение решений
25 % (23.09). Хвост, как и состояние сети, живёт в экземпляре, поэтому один
экземпляр нельзя звать из двух потоков — у каждой дорожки свой.

Правило нарезки `speech_timestamps` перенесено из silero-vad 6.2.1
(`utils_vad.get_speech_timestamps`) целиком, с теми же порогами и тем же
разрезом длинной речи по самой долгой паузе: границы участков на настоящих
записях совпадают с прежними бит в бит (проверка t113).
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable

import numpy as np

from . import config

log = logging.getLogger("hagen.vad")

SR = 16000
FRAME = 512
#: Хвост прошлого окна, который модель слушает вместе с текущим.
CONTEXT = 64
MODEL_PATH = config.MODELS_DIR / "vad" / "silero_vad.onnx"


class SileroVad:
    """Один экземпляр модели: сессия, состояние сети и хвост прошлого окна."""

    def __init__(self, path: str | Path | None = None):
        import onnxruntime as rt

        opts = rt.SessionOptions()
        # Один поток: модель крошечная, а два экземпляра (две дорожки) и так
        # считают параллельно. Так же было в обёртке пакета silero-vad.
        opts.inter_op_num_threads = 1
        opts.intra_op_num_threads = 1
        opts.log_severity_level = 3
        self.session = rt.InferenceSession(str(path or MODEL_PATH), providers=["CPUExecutionProvider"],
                                           sess_options=opts)
        self._sr = np.array(SR, dtype=np.int64)
        self.reset_states()

    def reset_states(self) -> None:
        self._state = np.zeros((2, 1, 128), dtype=np.float32)
        self._context = np.zeros((1, CONTEXT), dtype=np.float32)

    def __call__(self, frame: np.ndarray) -> float:
        """Вероятность речи в окне из FRAME отсчётов (16 кГц, float32)."""
        x = np.concatenate([self._context, np.asarray(frame, dtype=np.float32).reshape(1, FRAME)], axis=1)
        out, state = self.session.run(None, {"input": x, "state": self._state, "sr": self._sr})
        self._state = state
        self._context = np.ascontiguousarray(x[:, -CONTEXT:])
        return float(out[0, 0])

    def probs(self, frames: np.ndarray) -> np.ndarray:
        """Вероятности речи для матрицы окон [n, FRAME], по порядку."""
        frames = np.asarray(frames, dtype=np.float32)
        return np.asarray([self(frames[i]) for i in range(frames.shape[0])], dtype=np.float32)


def speech_timestamps(audio: np.ndarray,
                      model: SileroVad,
                      threshold: float = 0.5,
                      min_speech_duration_ms: int = 250,
                      max_speech_duration_s: float = float("inf"),
                      min_silence_duration_ms: int = 100,
                      speech_pad_ms: int = 30,
                      progress_tracking_callback: Callable[[float], Any] | None = None,
                      neg_threshold: float | None = None,
                      min_silence_at_max_speech: int = 98,
                      use_max_poss_sil_at_max_speech: bool = True) -> list[dict[str, int]]:
    """Участки речи в отсчётах: [{"start": n, "end": n}, ...].

    Перенос `get_speech_timestamps` из silero-vad 6.2.1 один в один, только
    звук — numpy, а не torch. Смысл параметров — как там:
      threshold — вероятность, с которой окно считается речью; выход из речи —
        по neg_threshold (по умолчанию threshold − 0,15);
      min_speech_duration_ms — короче этого участки выбрасываются;
      max_speech_duration_s — длиннее этого участок режется: по самой долгой
        паузе внутри (не короче min_silence_at_max_speech мс), а нет паузы —
        прямо на пределе;
      min_silence_duration_ms — сколько тишины закрывает участок;
      speech_pad_ms — на сколько раздвинуть границы каждого участка;
      progress_tracking_callback(процент) — ход подсчёта вероятностей.
    Состояние модели сбрасывается в начале: запись слушается с чистого листа.
    """
    audio = np.asarray(audio, dtype=np.float32).reshape(-1)
    sampling_rate = SR
    window_size_samples = FRAME

    model.reset_states()
    min_speech_samples = sampling_rate * min_speech_duration_ms / 1000
    speech_pad_samples = sampling_rate * speech_pad_ms / 1000
    max_speech_samples = sampling_rate * max_speech_duration_s - window_size_samples - 2 * speech_pad_samples
    min_silence_samples = sampling_rate * min_silence_duration_ms / 1000
    min_silence_samples_at_max_speech = sampling_rate * min_silence_at_max_speech / 1000

    audio_length_samples = len(audio)

    speech_probs = []
    for current_start_sample in range(0, audio_length_samples, window_size_samples):
        chunk = audio[current_start_sample: current_start_sample + window_size_samples]
        if len(chunk) < window_size_samples:
            chunk = np.pad(chunk, (0, int(window_size_samples - len(chunk))))
        speech_probs.append(model(chunk))
        progress = current_start_sample + window_size_samples
        if progress > audio_length_samples:
            progress = audio_length_samples
        if progress_tracking_callback:
            progress_tracking_callback((progress / audio_length_samples) * 100)

    triggered = False
    speeches: list[dict[str, Any]] = []
    current_speech: dict[str, Any] = {}

    if neg_threshold is None:
        neg_threshold = max(threshold - 0.15, 0.01)
    temp_end = 0        # возможный конец участка: тишина ещё может оказаться короткой
    prev_end = next_start = 0   # границы на случай, если участок упёрся в предел длины
    possible_ends: list[tuple[int, int]] = []

    for i, speech_prob in enumerate(speech_probs):
        cur_sample = window_size_samples * i

        # Речь вернулась после временного конца: если пауза была достаточно
        # долгой, запомнить её как место возможного разреза.
        if (speech_prob >= threshold) and temp_end:
            sil_dur = cur_sample - temp_end
            if sil_dur > min_silence_samples_at_max_speech:
                possible_ends.append((temp_end, sil_dur))
            temp_end = 0
            if next_start < prev_end:
                next_start = cur_sample

        # начало речи
        if (speech_prob >= threshold) and not triggered:
            triggered = True
            current_speech["start"] = cur_sample
            continue

        # предел длины участка: решить, где резать
        if triggered and (cur_sample - current_speech["start"] > max_speech_samples):
            if use_max_poss_sil_at_max_speech and possible_ends:
                # по самой долгой паузе внутри участка
                prev_end, dur = max(possible_ends, key=lambda x: x[1])
                current_speech["end"] = prev_end
                speeches.append(current_speech)
                current_speech = {}
                next_start = prev_end + dur

                if next_start < prev_end + cur_sample:
                    current_speech["start"] = next_start
                else:
                    triggered = False
                prev_end = next_start = temp_end = 0
                possible_ends = []
            else:
                # прежнее правило: по последней подходящей паузе, если она была
                if prev_end:
                    current_speech["end"] = prev_end
                    speeches.append(current_speech)
                    current_speech = {}
                    if next_start < prev_end:
                        triggered = False
                    else:
                        current_speech["start"] = next_start
                    prev_end = next_start = temp_end = 0
                    possible_ends = []
                else:
                    # паузы не было — резать прямо здесь
                    current_speech["end"] = cur_sample
                    speeches.append(current_speech)
                    current_speech = {}
                    prev_end = next_start = temp_end = 0
                    triggered = False
                    possible_ends = []
                    continue

        # тишина внутри речи
        if (speech_prob < neg_threshold) and triggered:
            if not temp_end:
                temp_end = cur_sample
            sil_dur_now = cur_sample - temp_end

            # слишком короткую паузу местом разреза не считать
            if not use_max_poss_sil_at_max_speech and sil_dur_now > min_silence_samples_at_max_speech:
                prev_end = temp_end

            if sil_dur_now < min_silence_samples:
                continue
            else:
                current_speech["end"] = temp_end
                if (current_speech["end"] - current_speech["start"]) > min_speech_samples:
                    speeches.append(current_speech)
                current_speech = {}
                prev_end = next_start = temp_end = 0
                triggered = False
                possible_ends = []
                continue

    if current_speech and (audio_length_samples - current_speech["start"]) > min_speech_samples:
        current_speech["end"] = audio_length_samples
        speeches.append(current_speech)

    # раздвинуть границы; соседние участки делят короткую паузу пополам
    for i, speech in enumerate(speeches):
        if i == 0:
            speech["start"] = int(max(0, speech["start"] - speech_pad_samples))
        if i != len(speeches) - 1:
            silence_duration = speeches[i + 1]["start"] - speech["end"]
            if silence_duration < 2 * speech_pad_samples:
                speech["end"] += int(silence_duration // 2)
                speeches[i + 1]["start"] = int(max(0, speeches[i + 1]["start"] - silence_duration // 2))
            else:
                speech["end"] = int(min(audio_length_samples, speech["end"] + speech_pad_samples))
                speeches[i + 1]["start"] = int(max(0, speeches[i + 1]["start"] - speech_pad_samples))
        else:
            speech["end"] = int(min(audio_length_samples, speech["end"] + speech_pad_samples))

    return speeches
