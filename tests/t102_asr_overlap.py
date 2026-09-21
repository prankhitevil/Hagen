# -*- coding: utf-8 -*-
"""Проверка 102: нахлёст на разрезе без паузы — слово на стыке остаётся целым.

Модель распознавания берёт не больше 24 секунд. Когда человек говорит дольше
без паузы, речь режется прямо посреди звука, часто посреди слова: первый кусок
кончался на «организован…», второй начинался с «…ное», и модель писала
«полное». Теперь кусок после такого разреза начинается на 1,5 с раньше, а куски
сшиваются по времени слов. Где разрез пришёлся на паузу, всё как было.

Распознавание здесь подставное: звук — это сама шкала времени (каждый отсчёт
равен своей секунде), и подделка по нему знает, какие слова лежат в куске
целиком, а какие задел его край — их она «слышит» обрывками, как настоящая
модель. Что проверяем:
  1. разрез без паузы помечается, после паузы — нет, без нахлёста — как раньше;
  2. кусок с нахлёстом не длиннее предела модели;
  3. файлы: каждое слово ровно один раз, слова на разрезах целые;
  4. быстрая модель без времени слов: без нахлёста, без повторов;
  5. поиск фраз живой записи: после разреза на пределе новая фраза начинается
     раньше разреза и говорит, где он был; без нахлёста — как раньше;
  6. живая запись целиком: те же требования, что к файлам.

Запуск из корня проекта:  .venv\\Scripts\\python.exe tests\\t102_asr_overlap.py
"""
import io
import sys
import time
from pathlib import Path

import numpy as np

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
sys.path.insert(0, str(PROJECT / "tests"))
import isolate  # noqa: E402

isolate.voices()
isolate.settings(max_phrase_seconds=10.0)

from hagen import asr, live, store, vad  # noqa: E402

LINES = []
FAIL = []
SR = vad.SR


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


def check(name, ok, detail=""):
    if not ok:
        FAIL.append(name)
    say(("   ok    " if ok else "   ПЛОХО ") + name + (": " + str(detail)[:300] if detail != "" else ""))


def timeline(total_s, long_words):
    """Слова записи: обычные по 0,4 с через 0,1 с и длинные слова в заданных местах."""
    out, t, n = [], 0.0, 0
    long_words = sorted(long_words, key=lambda w: w[1])
    while t < total_s - 0.5:
        hit = next((w for w in long_words if w[1] - 0.1 < t + 0.4 and w[2] + 0.1 > t), None)
        if hit is not None:
            if hit not in out:
                out.append(hit)
            t = hit[2] + 0.1
            continue
        n += 1
        out.append(("с%d" % n, round(t, 3), round(t + 0.4, 3)))
        t += 0.5
    return out


def time_pcm(total_s):
    """Звук, в котором каждый отсчёт равен своей секунде: по куску видно, где он."""
    return (np.arange(int(total_s * SR), dtype=np.float64) / SR).astype(np.float32)


def fake_hear(words_of_record, with_words=True):
    """Подставная модель: слова куска целиком, обрывки — у его краёв."""
    def hear(piece, *_a, **_kw):
        piece = np.asarray(piece).reshape(-1)
        if piece.size == 0:
            return asr.Result("")
        a = float(piece[0])
        b = a + piece.size / float(SR)
        got = []
        for text, s, e in words_of_record:
            if e <= a or s >= b:
                continue
            if s >= a and e <= b:
                got.append(asr.Word(text, s - a, e - a))
            elif s >= a:                      # край куска режет слово: слышно начало
                got.append(asr.Word(text[:max(1, len(text) // 2)], s - a, b - a))
            elif e <= b:                      # слышен конец — модель «узнаёт» похожее
                got.append(asr.Word("полн" + text[len(text) // 2:], 0.0, e - a))
            else:
                got.append(asr.Word("гм", 0.0, b - a))
        text = " ".join(w.text for w in got)
        return asr.Result(text, got if with_words else [])
    return hear


def words_in(pieces):
    return " ".join(p["text"] for p in pieces).split()


say("=== 1. Пометка разрезов ===")
spans = [{"start": 0, "end": 10 * SR}, {"start": 10 * SR, "end": 20 * SR},
         {"start": 21 * SR, "end": 30 * SR}]
marked = vad.mark_joints(spans, 1.5)
check("разрез без паузы: у первого куска cut", marked[0].get("cut") == 10 * SR, marked[0])
check("у следующего joint, и начат он на 1,5 с раньше",
      marked[1].get("joint") == 10 * SR and marked[1]["start"] == int(8.5 * SR), marked[1])
check("после паузы нахлёста нет", "joint" not in marked[2] and "cut" not in marked[1], marked[2])
check("исходный список не тронут", spans[1]["start"] == 10 * SR)
check("без нахлёста — как раньше", vad.mark_joints(spans, 0.0) == spans)

say("")
say("=== 2. Кусок с нахлёстом не длиннее предела ===")
asked = {}
real_ts = vad.speech_timestamps


def fake_ts(pcm, max_speech_s=24.0, **_kw):
    asked["max"] = max_speech_s
    n = int(max_speech_s * SR)
    return [{"start": 0, "end": n}, {"start": n, "end": 2 * n}]


vad.speech_timestamps = fake_ts
try:
    cut_spans = vad.split_for_asr(np.zeros(60 * SR, dtype=np.float32))
finally:
    vad.speech_timestamps = real_ts
check("поиск речи режет на нахлёст раньше предела", abs(asked.get("max", 0) - 22.5) < 1e-9, asked)
longest = max((s["end"] - s["start"]) / SR for s in cut_spans)
check("каждый кусок вместе с нахлёстом — не длиннее 24 с", longest <= 24.0 + 1e-6, longest)
check("второй кусок помечен как продолжение", cut_spans[1].get("joint") == int(22.5 * SR), cut_spans[1])

say("")
say("=== 3. Файлы: слова на разрезах целые, повторов нет ===")
LONG = [("организованное", 9.7, 10.4), ("распределённое", 19.6, 20.3)]
tl = timeline(30.0, LONG)
pcm = time_pcm(30.0)
file_spans = vad.mark_joints([{"start": 0, "end": 10 * SR}, {"start": 10 * SR, "end": 20 * SR},
                              {"start": 20 * SR, "end": 30 * SR}], 1.5)
real_precise = asr.transcribe_precise
asr.transcribe_precise = fake_hear(tl)
try:
    pieces = asr.transcribe_spans(pcm, file_spans, precise=True, words=True)
finally:
    asr.transcribe_precise = real_precise
got = words_in(pieces)
want = [w[0] for w in tl]
check("слова те же и в том же порядке, каждое ровно раз", got == want,
      [w for w in got if got.count(w) > 1 or w not in want][:6])
check("«организованное» и «распределённое» целые",
      "организованное" in got and "распределённое" in got and "полное" not in " ".join(got))
check("реплики идут по времени и не налезают друг на друга",
      all(a["end"] <= b["start"] + 1e-6 for a, b in zip(pieces, pieces[1:])),
      [(round(p["start"], 2), round(p["end"], 2)) for p in pieces])
check("слова реплики лежат внутри неё",
      all(p["start"] - 1e-6 <= w["start"] and w["end"] <= p["end"] + 0.5
          for p in pieces for w in p["words"]))

say("")
say("=== 4. Модель без времени слов: без нахлёста, без повторов ===")
asr.transcribe_precise = fake_hear(tl, with_words=False)
try:
    plain = asr.transcribe_spans(pcm, file_spans, precise=True, words=True)
finally:
    asr.transcribe_precise = real_precise
got_plain = words_in(plain)
check("целые слова не повторяются", all(got_plain.count(w) == 1 for w in want if w in got_plain),
      [w for w in want if got_plain.count(w) > 1][:6])
check("второй кусок распознан с самого разреза",
      plain[1]["start"] == 10.0 and plain[2]["start"] == 20.0,
      [round(p["start"], 2) for p in plain])

say("")
say("=== 5. Поиск фраз живой записи ===")
real_probs = vad._probs
vad._probs = lambda frames, model: np.ones(frames.shape[0], dtype=np.float32)
try:
    det = vad.StreamingPhraseDetector(max_phrase_s=5.0, overlap_s=1.5)
    events = det.feed(np.zeros(12 * SR, dtype=np.float32))
    old = vad.StreamingPhraseDetector(max_phrase_s=5.0, overlap_s=0.0)
    old_events = old.feed(np.zeros(12 * SR, dtype=np.float32))
finally:
    vad._probs = real_probs
ends = [e for e in events if e["type"] == "phrase_end"]
joints = [e for e in events if e["type"] == "speech_start" and e.get("joint")]
check("фраза режется на пределе и говорит, где разрез",
      ends and ends[0]["reason"] == "too_long" and ends[0].get("cut") == ends[0]["end"], ends[:1])
check("следующая фраза начинается на 1,5 с раньше разреза",
      joints and abs((joints[0]["joint"] - joints[0]["start"]) / SR - 1.5) < 0.05, joints[:1])
check("новая фраза снова не длиннее предела",
      len(ends) >= 2 and (ends[1]["end"] - ends[1]["start"]) / SR <= 5.1,
      [(e["start"] / SR, e["end"] / SR) for e in ends])
check("без нахлёста — как раньше: ни cut, ни продолжения",
      not any(e.get("cut") or e.get("joint") for e in old_events))

say("")
say("=== 6. Живая запись целиком ===")
LONG_LIVE = [("организованное", 9.7, 10.4), ("распределённое", 18.3, 18.9)]
tl_live = timeline(30.0, LONG_LIVE)
rid = store.create(title="Проверка 102", mode="online", source="live", category="Встречи")["id"]
real_live, real_route = asr.transcribe_live, asr.live_route
asr.transcribe_live = fake_hear(tl_live)
asr.live_route = lambda: {"live": "ox_fp32", "drafts": False}
vad._probs = lambda frames, model: np.ones(frames.shape[0], dtype=np.float32)
segs = []
try:
    pipe = live.TrackPipeline(rid, store.TRACK_FAR, on_segment=lambda _t, s: segs.append(s))
    check("нахлёст включён: у модели эфира есть время слов", pipe.detector.overlap_frames > 0)
    audio = time_pcm(30.0)
    for i in range(0, audio.size, SR):
        pipe.feed(audio[i:i + SR])
    deadline = time.time() + 30
    while pipe.pending and time.time() < deadline:
        time.sleep(0.05)
    pipe.finish()
    stored = [s for s in store.load_transcript(rid).get("segments") or []]
finally:
    asr.transcribe_live, asr.live_route = real_live, real_route
    vad._probs = real_probs
    store.delete(rid)
got_live = " ".join(s["text"] for s in sorted(stored, key=lambda s: s["start"])).split()
want_live = [w[0] for w in tl_live if w[2] <= 30.0]
reasons = [s.get("reason") for s in stored]
check("фразы резались на пределе", reasons.count("too_long") >= 2, reasons)
check("каждое слово ровно раз и по порядку", got_live == want_live[:len(got_live)]
      and len(got_live) >= len(want_live) - 2,
      [w for w in got_live if got_live.count(w) > 1 or w not in want_live][:6])
check("слова на разрезах целые",
      "организованное" in got_live and "распределённое" in got_live
      and "полное" not in " ".join(got_live))
ordered = sorted(stored, key=lambda s: s["start"])
check("реплики не налезают друг на друга",
      all(a["end"] <= b["start"] + 1e-6 for a, b in zip(ordered, ordered[1:])),
      [(round(s["start"], 2), round(s["end"], 2)) for s in ordered])

say("")
say("ИТОГО провалов: %d" % len(FAIL))
for f in FAIL:
    say("   - " + f)
io.open(PROJECT / "tests" / "t102_result.txt", "w", encoding="utf-8").write("\n".join(LINES))
sys.exit(1 if FAIL else 0)
