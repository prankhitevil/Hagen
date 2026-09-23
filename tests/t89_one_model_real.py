# -*- coding: utf-8 -*-
"""Проверка 89: одна точная модель на настоящих моделях (стенд 18.09, параметры 21.09).

Соседка t88 проверяет логику на подставных распознавателях. Здесь — то, что
видно только на настоящей модели:
  1. Прогрев эфира при выборе «одна модель: точная (torch)» грузит точную модель, а
     быструю — нет: сессий ONNX в памяти ни одной, torch-модель одна.
  2. Эфир, диктовка и файлы работают на одном и том же экземпляре модели;
     число потоков torch возвращается, даже если его сбросили в один.
  3. Дорожка эфира на tests\\meeting.wav: реплики с текстом и временем слов,
     слова лежат внутри своей реплики, черновиков нет.
  4. Сторож простоя модель эфира не выгружает, даже если её давно не трогали.
  5. Рекомендованное — одна точная модель на onnx-asr с полными весами: torch-модель не
     загружается, у эфира время слов, текст близок к torch.

Настоящие settings.json и база голосов не трогаются (tests\\isolate.py).
Запуск из корня проекта:  .venv\\Scripts\\python.exe tests\\t89_one_model_real.py
"""
import io
import sys
import time
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
sys.path.insert(0, str(PROJECT / "tests"))
import isolate  # noqa: E402
from harness import LINES, FAIL, say, check, finish  # noqa: E402

isolate.voices()
isolate.settings(asr_count=1, asr_single="precise", asr_engine="torch")

import numpy as np  # noqa: E402
import psutil  # noqa: E402

from hagen import asr, audio_io, live, needs, store  # noqa: E402

SR = asr.SR
ONE_TORCH = "live=torch,voice=torch,files=torch"
ONE_OX_FP32 = "live=ox_fp32,voice=ox_fp32,files=ox_fp32"


def memory_mb():
    """Рабочий набор и выделенная процессу память, МБ, после сборки мусора."""
    import gc

    gc.collect()
    info = psutil.Process().memory_info()
    return info.rss / 2**20, info.private / 2**20


if not needs.precise_ready():
    say("   (пропуск: точной модели в папке нет — скачать в «Настройки → Модели»)")
    sys.exit(0)

say("=== 1. Прогрев: точная вместо быстрой ===")
# torch в памяти и так: его поднимает поиск речи. Подключаем заранее, чтобы
# прибавка памяти была про модель, а не про библиотеку.
import torch  # noqa: E402,F401

live.vad.new_model()
before = memory_mb()
t0 = time.time()
info = asr.warmup(live=True, precise=False)
took = time.time() - t0
warm = memory_mb()
st = asr.live_state()
say("   прогрев %.1f с; сразу после него рабочий набор %+.0f МБ, выделено %+.0f МБ"
    % (took, warm[0] - before[0], warm[1] - before[1]))
check("модель эфира готова, работает «одна модель: точная (torch)»",
      st.get("state") == "ready" and st.get("key") == ONE_TORCH and "note" not in st, st)
check("быстрая модель не загружена: сессий ONNX нет", not asr._onnx_cache, list(asr._onnx_cache))
check("в памяти одна torch-модель — точная", list(asr._torch_cache) == ["v3_e2e_rnnt"],
      list(asr._torch_cache))

say("")
say("=== 2. Эфир, диктовка и файлы — одна модель ===")
model = asr._torch_cache["v3_e2e_rnnt"]
pcm, _sr = audio_io.read_wav(PROJECT / "tests" / "meeting.wav")
pcm = np.asarray(pcm, dtype=np.float32).reshape(-1)
piece = pcm[: 6 * SR]
res_live = asr.transcribe_live(piece)
res_dict = asr.transcribe_precise(piece, words=True)
check("эфир и диктовка дали одно и то же", res_live.text == res_dict.text and res_live.text,
      (res_live.text, res_dict.text))
check("у эфира есть время слов", len(res_live.words) > 0, len(res_live.words))
check("модель та же самая, второй не появилось",
      asr._torch_cache.get("v3_e2e_rnnt") is model and len(asr._torch_cache) == 1,
      list(asr._torch_cache))
check("быстрая так и не загрузилась", not asr._onnx_cache, list(asr._onnx_cache))
# silero-vad при подключении сбрасывает torch в один поток на весь процесс —
# так бывает, когда первая запись начинается после прогрева.
torch.set_num_threads(1)
asr.transcribe_live(piece)
check("потоки torch вернулись к заданным, хотя их сбросили в один",
      torch.get_num_threads() == asr._threads(), (torch.get_num_threads(), asr._threads()))
# Сразу после загрузки torch держит лишние ~300 МБ, после первых фраз память
# оседает. Замер 18.09 на этой машине: одна модель — около +1,0…1,3 ГБ рабочего
# набора, быстрая с точной вместе (эфир плюс диктовка или файл) — +2,2 ГБ.
used = memory_mb()
say("   после распознавания: рабочий набор %+.0f МБ, выделено %+.0f МБ"
    % (used[0] - before[0], used[1] - before[1]))
check("в памяти одна модель, не две (рабочий набор меньше +1,7 ГБ)", used[0] - before[0] < 1700,
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
asr._torch_used["v3_e2e_rnnt"] = time.time() - 10 * 3600
got = asr.release_idle(minutes=1)
check("модель эфира не выгружена, хотя её «не трогали 10 часов»",
      got == [] and "v3_e2e_rnnt" in asr._torch_cache, got)

say("")
say("=== 5. Рекомендованное: одна точная модель через onnx-asr, полные веса ===")
if asr.ox_available(None)[0]:
    import difflib  # noqa: E402

    from hagen import config, echo  # noqa: E402

    torch_text = res_live.text
    asr._torch_cache.clear()
    asr._torch_used.clear()
    config.save(dict(asr.RECOMMENDED))
    asr._live_route = None
    t0 = time.time()
    asr.warmup(live=True, precise=False)
    say("   прогрев %.1f с" % (time.time() - t0))
    st = asr.live_state()
    check("работает «одна модель: точная (onnx-asr, полные веса)»",
          st.get("state") == "ready" and st.get("key") == ONE_OX_FP32 and "note" not in st, st)
    ox_live = asr.transcribe_live(piece)
    ox_dict = asr.transcribe_precise(piece, words=True)
    check("эфир и диктовка — одно и то же, со временем слов",
          ox_live.text == ox_dict.text and len(ox_live.words) > 0, (ox_live.text, len(ox_live.words)))
    a, b = echo._words(torch_text), echo._words(ox_live.text)
    same = sum(bl.size for bl in difflib.SequenceMatcher(a=a, b=b).get_matching_blocks())
    check("текст близок к torch (не меньше 80 % слов)", same >= 0.8 * max(len(a), len(b)),
          (torch_text, ox_live.text))
    check("torch-модель не загружалась, быстрая тоже",
          not asr._torch_cache and not asr._onnx_cache and list(asr._ox_cache) == [None],
          (list(asr._torch_cache), list(asr._onnx_cache), list(asr._ox_cache)))
    check("слова лежат внутри куска и складываются в текст",
          all(0.0 <= w.start <= w.end <= 6.05 for w in ox_live.words)
          and " ".join(w.text for w in ox_live.words).split() == ox_live.text.split(),
          [(w.text, w.start, w.end) for w in ox_live.words][:4])
else:
    say("   (пропуск: файлов onnx-asr в папке нет)")

sys.exit(finish("t89"))
