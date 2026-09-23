# -*- coding: utf-8 -*-
"""Проверка 112: поиск речи на onnxruntime, без torch, — границы бит в бит с прежними.

Поиск речи (Silero VAD) считался пакетом silero-vad поверх torch: офлайн —
JIT-моделью, эфир — обёрткой onnxruntime из того же пакета. Теперь модель одна,
`models\\vad\\silero_vad.onnx`, сессия onnxruntime своя (hagen/vad_onnx.py), и
torch программе не нужен. Эталон снят 23.09 на прежнем коде, до правок, и
лежит здесь числами: участки речи, нарезка для распознавания, доля речи,
события эфира — с нахлёстом и без.

Что проверяем:
  1. torch не подключается: он закрыт до импорта программы, и всё работает;
  2. tests\\meeting.wav: участки, нарезка, доля речи, события эфира, первые
     вероятности — те же числа, что были;
  3. настоящая дорожка (запись владельца 22.09, 12:48, собеседники): то же
     самое, 54 участка и 44 фразы эфира. Дорожка в git не едет: ищется в data\\
     этой папки и соседних; нет — пункт пропускается с пометкой;
  4. два экземпляра модели в двух потоках не мешают друг другу: события те
     же, что при счёте по очереди;
  5. без «хвоста» прошлого окна вероятности другие — модель слушает 576
     отсчётов, а не 512.

Запуск из корня проекта:  .venv\\Scripts\\python.exe tests\\t113_vad_onnx.py
"""
import hashlib
import sys
import threading
from pathlib import Path

# torch закрыт ДО импорта программы: любое обращение к нему — ошибка импорта.
sys.modules["torch"] = None

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
sys.path.insert(0, str(PROJECT / "tests"))
from harness import LINES, FAIL, say, check, finish  # noqa: E402

import numpy as np  # noqa: E402

from hagen import audio_io, vad, vad_onnx  # noqa: E402

SR = vad.SR
MODEL_SHA256 = "1a153a22f4509e292a94e67d6f9b85e8deb25b4988682b7e174c65279d8788e3"

# ------------------------------------------------------------ эталон 23.09
# tests\meeting.wav, 820 734 отсчёта
MEETING = {
    "samples": 820734,
    "ratio": 0.808639583592248,
    "ts_default": [(2176, 79232), (96896, 154496), (171648, 215936), (224896, 248704), (268928, 325504),
                   (343168, 366976), (368768, 432512), (450176, 522112), (542848, 557440),
                   (566912, 621952), (638080, 779648), (787072, 820734)],
    "split": [{'start': 2176, 'end': 79232}, {'start': 96896, 'end': 154496}, {'start': 171648, 'end': 248704},
              {'start': 268928, 'end': 325504}, {'start': 343168, 'end': 432512}, {'start': 450176, 'end': 522112},
              {'start': 542848, 'end': 621952}, {'start': 638080, 'end': 820734}],
    "stream": [('speech_start', 2048, None, None, None, None), ('phrase_end', 2048, 386560, 'too_long', None, None),
               ('speech_start', 384512, None, None, None, None),
               ('phrase_end', 384512, 769024, 'too_long', None, None),
               ('speech_start', 766976, None, None, None, None)],
    "stream_pending": ('phrase_end', 766976, 821760, 'stop', None, None),
    "stream_overlap": [('speech_start', 2048, None, None, None, None),
                       ('phrase_end', 2048, 386560, 'too_long', 386560, None),
                       ('speech_start', 362496, None, None, None, 386560),
                       ('phrase_end', 362496, 747008, 'too_long', 747008, None),
                       ('speech_start', 722944, None, None, None, 747008)],
    "stream_overlap_pending": ('phrase_end', 722944, 821760, 'stop', None, None),
    "probs_first": [0.00167, 0.052115, 0.058405, 0.020086, 0.013713, 0.009747, 0.009811, 0.021758],
}
MEETING["ts_22_5"] = MEETING["ts_default"]

# Настоящая дорожка: запись 22.09, 12:48, собеседники, 6 579 392 отсчёта (6,9 мин).
REAL_REC = "20260922-124829-5b1a"
REAL = {
    "samples": 6579392,
    "ratio": 0.29835705183700867,
    "ts_default": [(785024, 792448), (904832, 916864), (929408, 941440), (1002624, 1018752), (1031296, 1047936),
                   (1093760, 1107328), (1122944, 1151360), (1156224, 1172352), (1292416, 1303424),
                   (1476224, 1506688), (1513088, 1547648), (1549440, 1567616), (1640064, 1673600),
                   (1689216, 1713024), (2118272, 2152832), (2168448, 2233216), (2243200, 2275712),
                   (2291328, 2329984), (2356864, 2381184), (2387072, 2411392), (2423936, 2466688),
                   (2660992, 2713472), (2971776, 3039616), (3050624, 3175296), (3187840, 3210624),
                   (3268736, 3344256), (3349120, 3389824), (3395200, 3438464), (3449472, 3499904),
                   (3505792, 3518848), (3523200, 3533696), (3542656, 3641216), (3655296, 3671424),
                   (3692672, 3702144), (3764864, 3857792), (3908736, 3926912), (3958400, 4042112),
                   (4132992, 4144512), (4159104, 4173696), (4407936, 4429696), (4775552, 4819840),
                   (5089408, 5119872), (5142656, 5185408), (5197440, 5212544), (5214336, 5235584),
                   (5259392, 5350784), (5386368, 5397888), (5416064, 5425536), (5649024, 5669248),
                   (5786752, 5832576), (5852288, 5928832), (5971072, 6005120), (6027904, 6097280),
                   (6108800, 6155648)],
    "split": [{'start': 785024, 'end': 792448}, {'start': 904832, 'end': 916864}, {'start': 929408, 'end': 941440},
              {'start': 1002624, 'end': 1018752}, {'start': 1031296, 'end': 1047936},
              {'start': 1093760, 'end': 1107328}, {'start': 1122944, 'end': 1172352},
              {'start': 1292416, 'end': 1303424}, {'start': 1476224, 'end': 1567616},
              {'start': 1640064, 'end': 1673600}, {'start': 1689216, 'end': 1713024},
              {'start': 2118272, 'end': 2152832}, {'start': 2168448, 'end': 2233216},
              {'start': 2243200, 'end': 2275712}, {'start': 2291328, 'end': 2329984},
              {'start': 2356864, 'end': 2411392}, {'start': 2423936, 'end': 2466688},
              {'start': 2660992, 'end': 2713472}, {'start': 2971776, 'end': 3039616},
              {'start': 3050624, 'end': 3175296}, {'start': 3187840, 'end': 3210624},
              {'start': 3268736, 'end': 3438464}, {'start': 3449472, 'end': 3641216},
              {'start': 3655296, 'end': 3671424}, {'start': 3692672, 'end': 3702144},
              {'start': 3764864, 'end': 3857792}, {'start': 3908736, 'end': 3926912},
              {'start': 3958400, 'end': 4042112}, {'start': 4132992, 'end': 4144512},
              {'start': 4159104, 'end': 4173696}, {'start': 4407936, 'end': 4429696},
              {'start': 4775552, 'end': 4819840}, {'start': 5089408, 'end': 5119872},
              {'start': 5142656, 'end': 5185408}, {'start': 5197440, 'end': 5235584},
              {'start': 5259392, 'end': 5350784}, {'start': 5386368, 'end': 5397888},
              {'start': 5416064, 'end': 5425536}, {'start': 5649024, 'end': 5669248},
              {'start': 5786752, 'end': 5832576}, {'start': 5852288, 'end': 5928832},
              {'start': 5971072, 'end': 6005120}, {'start': 6027904, 'end': 6097280},
              {'start': 6108800, 'end': 6155648}],
    "stream": [('speech_start', 784896, None, None, None, None), ('phrase_end', 784896, 793088, 'silence', None, None),
               ('speech_start', 904704, None, None, None, None), ('phrase_end', 904704, 941568, 'silence', None, None),
               ('speech_start', 1002496, None, None, None, None),
               ('phrase_end', 1002496, 1048064, 'silence', None, None),
               ('speech_start', 1093632, None, None, None, None),
               ('phrase_end', 1093632, 1172480, 'silence', None, None),
               ('speech_start', 1292288, None, None, None, None),
               ('phrase_end', 1292288, 1304064, 'silence', None, None),
               ('speech_start', 1476096, None, None, None, None),
               ('phrase_end', 1476096, 1567744, 'silence', None, None),
               ('speech_start', 1639936, None, None, None, None),
               ('phrase_end', 1639936, 1713664, 'silence', None, None),
               ('speech_start', 2118144, None, None, None, None),
               ('phrase_end', 2118144, 2466816, 'silence', None, None),
               ('speech_start', 2660864, None, None, None, None),
               ('phrase_end', 2660864, 2713600, 'silence', None, None),
               ('speech_start', 2971648, None, None, None, None),
               ('phrase_end', 2971648, 3210752, 'silence', None, None),
               ('speech_start', 3268608, None, None, None, None),
               ('phrase_end', 3268608, 3653120, 'too_long', None, None),
               ('speech_start', 3655168, None, None, None, None),
               ('phrase_end', 3655168, 3702272, 'silence', None, None),
               ('speech_start', 3764736, None, None, None, None),
               ('phrase_end', 3764736, 3858432, 'silence', None, None),
               ('speech_start', 3908608, None, None, None, None),
               ('phrase_end', 3908608, 4042240, 'silence', None, None),
               ('speech_start', 4132864, None, None, None, None),
               ('phrase_end', 4132864, 4173824, 'silence', None, None),
               ('speech_start', 4407808, None, None, None, None),
               ('phrase_end', 4407808, 4429824, 'silence', None, None),
               ('speech_start', 4775424, None, None, None, None),
               ('phrase_end', 4775424, 4819968, 'silence', None, None),
               ('speech_start', 5089280, None, None, None, None),
               ('phrase_end', 5089280, 5350912, 'silence', None, None),
               ('speech_start', 5386240, None, None, None, None),
               ('phrase_end', 5386240, 5425664, 'silence', None, None),
               ('speech_start', 5648896, None, None, None, None),
               ('phrase_end', 5648896, 5669376, 'silence', None, None),
               ('speech_start', 5786624, None, None, None, None),
               ('phrase_end', 5786624, 5928960, 'silence', None, None),
               ('speech_start', 5970944, None, None, None, None),
               ('phrase_end', 5970944, 6155776, 'silence', None, None)],
    "stream_pending": None,
    "stream_overlap_pending": None,
    "probs_first": [0.00167, 0.006884, 0.008911, 0.007857, 0.005907, 0.005961, 0.005853, 0.00564],
}
REAL["ts_22_5"] = REAL["ts_default"]
# С нахлёстом на настоящей дорожке отличается только разрез на пределе длины
# (фраза 3268608–3653120): следующая фраза начата на 1,5 с раньше разреза.
REAL["stream_overlap"] = [
    ('phrase_end', 3268608, 3653120, 'too_long', 3653120, None) if e[1] == 3268608 and e[0] == 'phrase_end'
    else ('speech_start', 3629056, None, None, None, 3653120) if e == ('speech_start', 3655168, None, None, None, None)
    else ('phrase_end', 3629056, 3702272, 'silence', None, None) if e == ('phrase_end', 3655168, 3702272, 'silence', None, None)
    else e
    for e in REAL["stream"]]


def event(e):
    """Событие эфира в одну строку сравнения: тип, начало, конец, причина, разрез, стык."""
    if e is None:
        return None
    return (e["type"], e["start"], e.get("end"), e.get("reason"), e.get("cut"), e.get("joint"))


def stream(pcm, **kw):
    """Дорожка через поиск фраз эфира кусками по 0,1 с — как их отдаёт устройство."""
    det = vad.StreamingPhraseDetector(**kw)
    out = []
    for i in range(0, pcm.size, 1600):
        out += det.feed(pcm[i:i + 1600])
    return [event(e) for e in out], event(det.pending_phrase())


def load(path):
    pcm, sr = audio_io.read_wav(path)
    return np.asarray(pcm, dtype=np.float32).reshape(-1), sr


def compare(title, pcm, ref):
    say("")
    say("=== %s ===" % title)
    check("длина дорожки та же", pcm.size == ref["samples"], pcm.size)
    ts = [(s["start"], s["end"]) for s in vad.speech_timestamps(pcm)]
    check("участки речи — бит в бит (%d)" % len(ref["ts_default"]), ts == ref["ts_default"],
          [(a, b) for a, b in zip(ts, ref["ts_default"], strict=False) if a != b][:3] or (len(ts), len(ref["ts_default"])))
    ts = [(s["start"], s["end"]) for s in vad.speech_timestamps(pcm, max_speech_s=22.5)]
    check("участки с пределом 22,5 с — бит в бит", ts == ref["ts_22_5"], len(ts))
    split = vad.split_for_asr(pcm)
    check("нарезка для распознавания — бит в бит (%d кусков)" % len(ref["split"]), split == ref["split"],
          [(a, b) for a, b in zip(split, ref["split"], strict=False) if a != b][:3] or (len(split), len(ref["split"])))
    ratio = vad.speech_ratio(pcm)
    check("доля речи — то же число", ratio == ref["ratio"], (ratio, ref["ratio"]))
    got, pending = stream(pcm)
    check("события эфира — бит в бит (%d)" % len(ref["stream"]), got == ref["stream"],
          [(a, b) for a, b in zip(got, ref["stream"], strict=False) if a != b][:3] or (len(got), len(ref["stream"])))
    check("незакрытая фраза на «Стопе» — та же", pending == ref["stream_pending"], (pending, ref["stream_pending"]))
    got, pending = stream(pcm, overlap_s=vad.OVERLAP_S)
    check("события эфира с нахлёстом — бит в бит", got == ref["stream_overlap"],
          [(a, b) for a, b in zip(got, ref["stream_overlap"], strict=False) if a != b][:3])
    check("…и незакрытая фраза", pending == ref["stream_overlap_pending"], pending)
    n = len(ref["probs_first"])
    probs = [round(float(p), 6) for p in vad._probs(pcm[: n * vad.FRAME].reshape(n, vad.FRAME), vad.new_model())]
    check("первые вероятности — те же с точностью до 1e-6", probs == ref["probs_first"], probs)


say("=== 1. Без torch ===")
check("torch закрыт и не подключился", sys.modules.get("torch") is None)
check("в hagen.vad и hagen.vad_onnx torch не импортируется",
      all("import torch" not in Path(m.__file__).read_text(encoding="utf-8") for m in (vad, vad_onnx)))
path = vad_onnx.MODEL_PATH
check("файл модели лежит в models\\vad", path.exists(), path)
check("это та самая модель silero_vad.onnx (silero-vad 6.2.1)",
      path.exists() and hashlib.sha256(path.read_bytes()).hexdigest() == MODEL_SHA256)
check("хвост прошлого окна — 64 отсчёта, окно — 512", vad_onnx.CONTEXT == 64 and vad.FRAME == 512)

pcm, sr = load(PROJECT / "tests" / "meeting.wav")
check("meeting.wav — 16 кГц", sr == SR, sr)
compare("2. tests\\meeting.wav", pcm, MEETING)

say("")
say("=== 3. Настоящая дорожка ===")
real = None
for root in [PROJECT] + sorted(p for p in PROJECT.parent.iterdir() if p.is_dir()):
    cand = root / "data" / REAL_REC / "far.wav"
    if cand.is_file():
        real = cand
        break
if real is None:
    say("   (пропуск: записи %s нет ни в data\\ этой папки, ни в соседних)" % REAL_REC)
else:
    pcm_real, sr = load(real)
    check("дорожка — 16 кГц", sr == SR, sr)
    compare("3. настоящая дорожка far.wav", pcm_real, REAL)

say("")
say("=== 4. Два экземпляра в двух потоках ===")
piece = pcm[: 20 * SR]
alone = [stream(piece)[0], stream(piece, silence_ms=600)[0]]
results = [None, None]


def run(i, **kw):
    results[i] = stream(piece, **kw)[0]


threads = [threading.Thread(target=run, args=(0,)), threading.Thread(target=run, args=(1,), kwargs={"silence_ms": 600})]
for t in threads:
    t.start()
for t in threads:
    t.join()
check("события двух дорожек в потоках те же, что по очереди", results == alone,
      [len(r or []) for r in results])
check("общий экземпляр офлайна — один на процесс", vad.get_model() is vad.get_model())
check("у каждого детектора свой экземпляр", vad.StreamingPhraseDetector().model is not vad.StreamingPhraseDetector().model)

say("")
say("=== 5. Хвост прошлого окна ===")
# первые 120 окон: тишина и начало речи (речь в meeting.wav с отсчёта 2176)
frames = pcm[: 120 * vad.FRAME].reshape(120, vad.FRAME)
with_tail = vad._probs(frames, vad.new_model())
model = vad.new_model()
state = np.zeros((2, 1, 128), dtype=np.float32)
no_tail = []
for k in range(frames.shape[0]):
    # окно подано без хвоста — 512 отсчётов вместо 576, как в первой пробе 23.09
    out, state = model.session.run(None, {"input": frames[k].reshape(1, -1), "state": state,
                                          "sr": np.array(SR, dtype=np.int64)})
    no_tail.append(float(out[0, 0]))
no_tail = np.asarray(no_tail, dtype=np.float32)
diff = float(np.abs(with_tail - no_tail).max())
check("без хвоста вероятности другие (расхождение > 0,1)", diff > 0.1, round(diff, 4))
check("с хвостом первые вероятности эталонные",
      [round(float(p), 6) for p in with_tail[:8]] == MEETING["probs_first"])

sys.exit(finish("t113"))
