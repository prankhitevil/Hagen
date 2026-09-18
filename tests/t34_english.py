# -*- coding: utf-8 -*-
"""Проверка 34: английское распознавание (Parakeet) и что русский путь не тронут."""
import io
import sys
import time
import wave
from pathlib import Path

import numpy as np

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

from hagen import asr  # noqa: E402

LINES = []
FAIL = []


def say(msg):
    LINES.append(str(msg))
    try:
        print(str(msg), flush=True)
    except Exception:
        # Консоль не знает этих букв (бывает cp1251) — печатаем без них.
        try:
            print(str(msg).encode("ascii", "replace").decode("ascii"), flush=True)
        except Exception:
            pass


def check(name, got, want):
    ok = got == want
    if not ok:
        FAIL.append(name)
    say(("   ok    " if ok else "   ПЛОХО ") + name + ": " + repr(got)
        + ("" if ok else "  (ждали " + repr(want) + ")"))


say("=== 1. Сборка слов из потокенных отметок ===")
# Пакет при чтении словаря заменяет маркер SentencePiece на ПРОБЕЛ, поэтому
# начало слова — это пробел в начале токена. Проверяем именно это.
words = asr._words_from_tokens(
    [" hel", "lo", " wor", "ld", "!"],
    [0.0, 0.08, 0.40, 0.48, 0.56],
    total_s=1.0,
)
check("слов собрано", [w.text for w in words], ["hello", "world!"])
check("начало первого", round(words[0].start, 2), 0.0)
check("конец первого — начало второго", round(words[0].end, 2), 0.40)
check("начало второго", round(words[1].start, 2), 0.40)
check("конец последнего = последняя отметка + шаг",
      round(words[1].end, 2), round(0.56 + asr.EN_FRAME_S, 2))

one = asr._words_from_tokens([" one"], [1.5], total_s=3.0)
check("одно слово", [(w.text, round(w.start, 2)) for w in one], [("one", 1.5)])
check("конец не уезжает за длительность", one[0].end <= 3.0, True)

no_lead = asr._words_from_tokens(["ab", "cd"], [0.0, 0.08], total_s=0.5)
check("токены без пробела — одно слово", [w.text for w in no_lead], ["abcd"])
check("пустое на входе — пусто", asr._words_from_tokens(None, None, 1.0), [])
check("отметок нет — слов нет", asr._words_from_tokens([" a"], None, 1.0), [])

say("")
say("=== 2. Пределы длины куска зависят от модели ===")
check("русский предел", asr.max_chunk_seconds("ru"), 24.0)
check("английский предел", asr.max_chunk_seconds("en"), 120.0)
check("английский больше русского", asr.MAX_CHUNK_EN_S > asr.MAX_CHUNK_S, True)

say("")
say("=== 3. Русский путь не тронут ===")
calls = []
real_torch = asr._transcribe_torch
asr._transcribe_torch = lambda pcm, name, word_timestamps=True: (
    calls.append((name, word_timestamps)) or asr.Result("русский текст"))
try:
    silence = np.zeros(int(0.5 * asr.SR), dtype=np.float32)
    res = asr.transcribe_precise(silence, words=True)
    check("по умолчанию идём в GigaAM через torch", len(calls), 1)
    check("модель та же", calls[0][0], "v3_e2e_rnnt")
    check("слова запрошены", calls[0][1], True)
    check("текст вернулся", res.text, "русский текст")
    calls.clear()
    res = asr.transcribe_precise(silence, words=True, lang="ru")
    check("явный русский — тоже torch", len(calls), 1)
finally:
    asr._transcribe_torch = real_torch

say("")
say("=== 4. Английская модель на месте ===")
ok, why = asr.english_available()
say("   " + why)
check("модель готова", ok, True)

if ok:
    say("")
    say("=== 5. Настоящее распознавание английского ===")
    # Ищем английский образец; если его нет — берём кусок любого wav, чтобы
    # проверить хотя бы то, что модель считается и не падает.
    sample = None
    for name in ("english.wav", "en.wav"):
        cand = PROJECT / "tests" / name
        if cand.exists():
            sample = cand
            break
    if sample is None:
        say("   пропущено: в tests нет ни одного wav")
    else:
        with wave.open(str(sample), "rb") as wf:
            rate = wf.getframerate()
            frames = wf.readframes(min(wf.getnframes(), rate * 20))
            channels = wf.getnchannels()
        pcm = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
        if channels > 1:
            pcm = pcm.reshape(-1, channels).mean(axis=1)
        if rate != asr.SR:                      # грубая, но достаточная передискретизация
            idx = np.linspace(0, pcm.shape[0] - 1, int(pcm.shape[0] * asr.SR / rate))
            pcm = np.interp(idx, np.arange(pcm.shape[0]), pcm).astype(np.float32)
        seconds = pcm.shape[0] / float(asr.SR)
        say("   образец: %s, %.1f c" % (sample.name, seconds))
        # Первый вызов грузит 622 МБ весов, и его время не про скорость
        # распознавания. Поэтому греем, а замеряем второй проход.
        t0 = time.time()
        res = asr._transcribe_english(pcm)
        warm = time.time() - t0
        t0 = time.time()
        res = asr._transcribe_english(pcm)
        spent = time.time() - t0
        say("   первый проход с загрузкой модели: %.1f c" % warm)
        say("   прогретый проход: %.1f c — это %.1fx реального времени"
            % (spent, seconds / max(spent, 0.01)))
        say("   час записи считался бы примерно %.0f минут" % (3600.0 / max(seconds / spent, 0.01) / 60.0))
        say("   текст: " + (res.text[:200] or "(пусто)"))
        check("модель что-то вернула", isinstance(res.text, str), True)
        check("слова с отметками есть", len(res.words) > 0 or not res.text, True)
        if res.words:
            check("отметки внутри отрезка",
                  all(0.0 <= w.start <= seconds + 1 for w in res.words), True)
            check("слова идут по возрастанию",
                  all(res.words[i].start <= res.words[i + 1].start
                      for i in range(len(res.words) - 1)), True)

say("")
say("ИТОГО провалов: %d" % len(FAIL))
for f in FAIL:
    say("   - " + f)
io.open(PROJECT / "tests" / "t34_result.txt", "w", encoding="utf-8").write("\n".join(LINES))
sys.exit(1 if FAIL else 0)
