# -*- coding: utf-8 -*-
"""Проверка 89: одна точная модель на настоящих моделях (стенд 18.09, параметры 21.09).

Соседка t88 проверяет логику на подставных распознавателях. Здесь — то, что
видно только на настоящей модели, и всё это без torch (он закрыт до импорта):
  1. Прогрев эфира при рекомендованном выборе грузит точную модель с полными
     весами, а быструю — нет: в памяти одна модель.
  2. Эфир, диктовка и файлы работают на одном и том же экземпляре модели.
  3. Дорожка эфира на tests\\meeting.wav: реплики с текстом и временем слов,
     слова лежат внутри своей реплики, черновиков нет.
  4. Сторож простоя модель эфира не выгружает, даже если её давно не трогали.
  5. Быстрая модель (если скачана): текст без времени слов, близок к точной;
     после выбора «одна быстрая» точная не загружается.

Настоящие settings.json и база голосов не трогаются (tests\\isolate.py).
Запуск из корня проекта:  .venv\\Scripts\\python.exe tests\\t89_one_model_real.py
"""
import sys
import time
from pathlib import Path

# torch закрыт ДО импорта программы: распознавание должно обойтись без него.
sys.modules["torch"] = None

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
sys.path.insert(0, str(PROJECT / "tests"))
import isolate  # noqa: E402
from harness import LINES, FAIL, say, check, finish  # noqa: E402

isolate.voices()
isolate.settings(asr_count=1, asr_single="precise", asr_weights="fp32")

import numpy as np  # noqa: E402
import psutil  # noqa: E402

from hagen import asr, audio_io, live, needs, store  # noqa: E402

SR = asr.SR
ONE_OX_FP32 = "live=ox_fp32,voice=ox_fp32,files=ox_fp32"
ONE_FAST = "live=fast,voice=fast,files=fast"


def memory_mb():
    """Рабочий набор и выделенная процессу память, МБ, после сборки мусора."""
    import gc

    gc.collect()
    info = psutil.Process().memory_info()
    return info.rss / 2**20, info.private / 2**20


if not needs.ready("precise_ox_fp32"):
    say("   (пропуск: точной модели в папке нет — скачать в «Настройки → Модели»)")
    sys.exit(0)

say("=== 1. Прогрев: точная, и только она ===")
live.vad.new_model()
before = memory_mb()
t0 = time.time()
info = asr.warmup(live=True, precise=False)
took = time.time() - t0
warm = memory_mb()
st = asr.live_state()
say("   прогрев %.1f с; сразу после него рабочий набор %+.0f МБ, выделено %+.0f МБ"
    % (took, warm[0] - before[0], warm[1] - before[1]))
check("модель эфира готова, работает «одна модель: точная (полные веса)»",
      st.get("state") == "ready" and st.get("key") == ONE_OX_FP32 and "note" not in st, st)
check("в памяти одна модель — точная с полными весами", list(asr._ox_cache) == ["ox_fp32"],
      list(asr._ox_cache))
check("torch так и не подключился", sys.modules.get("torch") is None)

say("")
say("=== 2. Эфир, диктовка и файлы — одна модель ===")
model = asr._ox_cache["ox_fp32"]
pcm, _sr = audio_io.read_wav(PROJECT / "tests" / "meeting.wav")
pcm = np.asarray(pcm, dtype=np.float32).reshape(-1)
piece = pcm[: 6 * SR]
res_live = asr.transcribe_live(piece)
res_dict = asr.transcribe_precise(piece, words=True, role="voice")
res_file = asr.transcribe_precise(piece, words=True, role="files")
check("эфир, диктовка и файлы дали одно и то же",
      res_live.text == res_dict.text == res_file.text and res_live.text, (res_live.text, res_dict.text))
check("у эфира есть время слов", len(res_live.words) > 0, len(res_live.words))
check("модель та же самая, второй не появилось",
      asr._ox_cache.get("ox_fp32") is model and len(asr._ox_cache) == 1, list(asr._ox_cache))
check("слова лежат внутри куска и складываются в текст",
      all(0.0 <= w.start <= w.end <= 6.05 for w in res_live.words)
      and " ".join(w.text for w in res_live.words).split() == res_live.text.split(),
      [(w.text, w.start, w.end) for w in res_live.words][:4])
# Замер 23.09 на этой машине: точная с полными весами — около +0,9 ГБ рабочего
# набора сверх numpy и onnxruntime; две модели (быстрая и точная) — около +1,8.
used = memory_mb()
say("   после распознавания: рабочий набор %+.0f МБ, выделено %+.0f МБ"
    % (used[0] - before[0], used[1] - before[1]))
check("в памяти одна модель, не две (рабочий набор меньше +1,5 ГБ)", used[0] - before[0] < 1500,
      "%+.0f МБ" % (used[0] - before[0]))

say("")
say("=== 3. Дорожка эфира на tests\\meeting.wav ===")
rid = store.create(title="Проверка 89", mode="online", source="live")["id"]
segs, drafts = [], []
try:
    tp = live.TrackPipeline(rid, store.TRACK_FAR,
                            on_segment=lambda track, seg: segs.append(seg),
                            on_draft=lambda track, text, at: drafts.append(text))
    t0 = time.time()
    step = int(0.1 * SR)
    for i in range(0, pcm.size, step):
        tp.feed(pcm[i:i + step])
    tp.finish(timeout=180.0)
    took = time.time() - t0
finally:
    store.delete(rid)
say("   звук %.0f с разобран за %.1f с" % (pcm.size / float(SR), took))
for s in segs:
    say("   %6.2f–%6.2f  слов %3d  %s" % (s["start"], s["end"], len(s.get("words") or []),
                                         s["text"][:60]))
check("реплики есть", len(segs) >= 2, len(segs))
check("черновиков нет", not [d for d in drafts if d], [d for d in drafts if d][:2])
check("у каждой реплики — время слов", segs and all(s.get("words") for s in segs))
inside = [s for s in segs
          if not all(s["start"] - 0.3 <= w["start"] <= w["end"] <= s["end"] + 0.3
                     for w in s.get("words") or [])]
check("слова лежат внутри своей реплики (время от начала записи)", not inside,
      [(s["start"], s["end"], s["words"][0], s["words"][-1]) for s in inside][:2])
said = " ".join(s["text"].lower() for s in segs)
found = [w for w in ("срок", "закупк", "смет", "подрядчик") if w in said]
check("в тексте — слова из «совещания»", len(found) >= 2, found)
words_text = [" ".join(w["text"] for w in s["words"]) for s in segs]
check("слова складываются в текст реплики", all(a.split() == s["text"].split()
                                                for a, s in zip(words_text, segs)),
      [(a[:40], s["text"][:40]) for a, s in zip(words_text, segs)][:2])

say("")
say("=== 4. Сторож простоя ===")
asr._ox_used["ox_fp32"] = time.time() - 10 * 3600
got = asr.release_idle(minutes=1)
check("модель эфира не выгружена, хотя её «не трогали 10 часов»",
      got == [] and "ox_fp32" in asr._ox_cache, got)

say("")
say("=== 5. Быстрая модель ===")
if needs.ready("fast"):
    import difflib  # noqa: E402

    from hagen import config, echo  # noqa: E402

    precise_text = res_live.text
    asr._ox_cache.clear()
    asr._ox_used.clear()
    config.save({"asr_count": 1, "asr_single": "fast"})
    asr._live_route = None
    t0 = time.time()
    asr.warmup(live=True, precise=False)
    say("   прогрев %.1f с" % (time.time() - t0))
    st = asr.live_state()
    check("работает «одна модель: быстрая»",
          st.get("state") == "ready" and st.get("key") == ONE_FAST and "note" not in st, st)
    fast_live = asr.transcribe_live(piece)
    check("быстрая: текст есть, времени слов нет", fast_live.text and fast_live.words == [],
          (fast_live.text, len(fast_live.words)))
    a, b = echo._words(precise_text), echo._words(fast_live.text)
    same = sum(bl.size for bl in difflib.SequenceMatcher(a=a, b=b).get_matching_blocks())
    check("текст близок к точной (не меньше 80 % слов)", same >= 0.8 * max(len(a), len(b)),
          (precise_text, fast_live.text))
    check("точная не загружалась", list(asr._ox_cache) == ["fast"], list(asr._ox_cache))
else:
    say("   (пропуск: быстрой модели в папке нет)")

sys.exit(finish("t89"))
