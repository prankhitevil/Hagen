# -*- coding: utf-8 -*-
"""Проверка 100: движок разметки ONNX + VBx и выбор движка релизом.

У программы два движка разметки говорящих с одной и той же моделью
community-1: через pyannote (нужен токен Hugging Face) и через ONNX + VBx
(токен, сеть и pyannote не нужны). Какой работает, решает релиз — файл
release.json. Совпадение ONNX с pyannote по кадрам меряет
tools\\compare_diar.py на настоящих встречах; здесь — то, что должно
держаться всегда:
  1. Дверь у движков одинаковая: те же функции с теми же аргументами.
  2. release.json выбирает движок; нет файла или мусор — pyannote.
  3. ONNX готов без токена; pyannote без токена — нет; странице уходят
     движок и признак «нужен ли токен», поле токена она прячет по нему.
  4. Разметка через ONNX: голоса, отпечатки 256, без наложений в разметке
     «без наложений», pyannote не импортирован.
  5. Повторяемость: второй прогон даёт то же самое.
  6. Ограничение числа голосов доходит до движка.
  7. Ручки из «Продвинутых» доходят до движка.
  8. Отпечатки кусков для отсева эха: короткий — пусто, длинный — 256.

Настоящие settings.json и база голосов не трогаются (tests\\isolate.py).
Запуск из корня проекта:  .venv\\Scripts\\python.exe tests\\t100_diar_onnx.py
"""
import inspect
import io
import json
import sys
import tempfile
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
sys.path.insert(0, str(PROJECT / "tests"))
import isolate  # noqa: E402

isolate.voices()
isolate.settings()

from hagen import audio_io, config, diar_onnx, diar_pyannote, diarize  # noqa: E402

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


def check(name, ok, detail=""):
    if not ok:
        FAIL.append(name)
    say(("   ok    " if ok else "   ПЛОХО ") + name + (": " + str(detail) if detail != "" else ""))


say("=== 1. Дверь у движков одинаковая ===")
for attr in ("NAME", "NEEDS_TOKEN", "RTF", "OVERHEAD_S"):
    check("у обоих движков есть %s" % attr,
          hasattr(diar_onnx, attr) and hasattr(diar_pyannote, attr))
for fn in ("ready", "load", "model_name", "run", "embed", "tuning_defaults"):
    a, b = getattr(diar_onnx, fn, None), getattr(diar_pyannote, fn, None)
    same = callable(a) and callable(b) and \
        list(inspect.signature(a).parameters) == list(inspect.signature(b).parameters)
    check("%s: одинаковые аргументы" % fn, same,
          "" if same else (inspect.signature(a) if a else None, inspect.signature(b) if b else None))
check("имена движков разные и из списка",
      {diar_onnx.NAME, diar_pyannote.NAME} == set(diarize.ENGINES))
check("токен нужен только pyannote",
      diar_onnx.NEEDS_TOKEN is False and diar_pyannote.NEEDS_TOKEN is True)

say("")
say("=== 2. release.json выбирает движок ===")
real_path = diarize.RELEASE_PATH
tmp = Path(tempfile.mkdtemp(prefix="release_test_"))
try:
    diarize.RELEASE_PATH = tmp / "release.json"
    check("нет файла — pyannote", diarize.release_engine() == "pyannote")
    diarize.RELEASE_PATH.write_text(json.dumps({"diarize": "onnx"}), encoding="utf-8")
    check("onnx из файла", diarize.release_engine() == "onnx")
    diarize.RELEASE_PATH.write_text(json.dumps({"diarize": " ONNX "}), encoding="utf-8")
    check("регистр и пробелы не мешают", diarize.release_engine() == "onnx")
    diarize.RELEASE_PATH.write_text("{мусор", encoding="utf-8")
    check("мусор в файле — pyannote", diarize.release_engine() == "pyannote")
    diarize.RELEASE_PATH.write_text(json.dumps({"diarize": "whisper"}), encoding="utf-8")
    check("незнакомый движок — pyannote", diarize.release_engine() == "pyannote")
finally:
    diarize.RELEASE_PATH = real_path
try:
    diarize.use_engine("whisper")
    check("use_engine не принимает незнакомое", False)
except ValueError:
    check("use_engine не принимает незнакомое", True)
diarize.use_engine("onnx")
check("use_engine(onnx) → модуль diar_onnx", diarize.engine() is diar_onnx)
diarize.use_engine("pyannote")
check("use_engine(pyannote) → модуль diar_pyannote", diarize.engine() is diar_pyannote)
diarize.use_engine(None)
check("use_engine(None) → снова по релизу", diarize.engine_name() == diarize.release_engine())

say("")
say("=== 3. Готовность без токена ===")
diarize.use_engine("onnx")
ok, why = diarize.available()
check("ONNX готов без токена", ok, why)
check("ONNX: токен не нужен", diarize.needs_token() is False)
diarize.use_engine("pyannote")
check("pyannote: токен нужен", diarize.needs_token() is True)
if (config.get("hf_token") or "").strip():
    say("   (пропуск: токен лежит в hf_token.txt рядом с программой)")
else:
    ok, why = diarize.available()
    check("pyannote без токена не готов и говорит про токен", (not ok) and "токен" in why, why)
pub = config.public()
check("странице: движок pyannote и поле токена нужно",
      pub.get("diarize_engine") == "pyannote" and pub.get("diarize_needs_token") is True,
      (pub.get("diarize_engine"), pub.get("diarize_needs_token")))
diarize.use_engine("onnx")
pub = config.public()
check("странице: движок onnx и поле токена не нужно",
      pub.get("diarize_engine") == "onnx" and pub.get("diarize_needs_token") is False,
      (pub.get("diarize_engine"), pub.get("diarize_needs_token")))
page = io.open(PROJECT / "hagen" / "static" / "app.js", encoding="utf-8").read()
check("страница прячет поле токена по признаку от службы",
      "'hf-field'" in page and "diarize_needs_token" in page)

say("")
say("=== 4. Разметка через ONNX ===")
wav = PROJECT / "tests" / "meeting.wav"
check("файл на месте", wav.exists())
if not wav.exists():
    sys.exit(1)
pcm, sr = audio_io.read_wav(wav)
res = diarize.diarize_pcm(pcm, sr=sr)
labels = res.get("labels") or []
check("нашлись голоса", len(labels) >= 1, labels)
check("модель подписана", res.get("model") == diar_onnx.model_name(), res.get("model"))
embs = res.get("embeddings") or {}
check("отпечаток у каждого голоса", sorted(embs) == sorted(labels), sorted(embs))
check("отпечатки размерности 256", all(len(v) == 256 for v in embs.values()),
      [len(v) for v in embs.values()])
excl = res.get("exclusive_turns") or []
overlap = any(b["start"] < a["end"] - 1e-6 for a, b in zip(excl, excl[1:]))
check("в разметке без наложений голоса не перекрываются", excl and not overlap, len(excl))
check("pyannote не импортирован", not any(m.split(".")[0] == "pyannote" for m in sys.modules))
say("   голосов %d, интервалов %d, речи %.1f с, %.1f с (RTF %.3f)"
    % (len(labels), len(res.get("turns") or []), res.get("speech_seconds", 0),
       res.get("elapsed_s", 0), res.get("rtf", 0)))

say("")
say("=== 5. Повторяемость ===")
again = diarize.diarize_pcm(pcm, sr=sr)
check("разметка та же", again.get("turns") == res.get("turns"))
check("отпечатки те же", again.get("embeddings") == res.get("embeddings"))

say("")
say("=== 6. Ограничение числа голосов ===")
one = diarize.diarize_pcm(pcm, sr=sr, max_speakers=1)
check("max_speakers=1 — один голос", len(one.get("labels") or []) == 1, one.get("labels"))

say("")
say("=== 7. Ручки из «Продвинутых» доходят до движка ===")
model = diar_onnx.load()
seen = {}
real_segment, real_cluster = model.segment, model.cluster


def spy_segment(pcm_, step_s, hook):
    seen["step"] = step_s
    return real_segment(pcm_, step_s, hook)


def spy_cluster(emb, seg, threshold, Fa, Fb, ratio, num, lo, hi):
    seen.update(threshold=threshold, Fb=Fb, ratio=ratio)
    return real_cluster(emb, seg, threshold, Fa, Fb, ratio, num, lo, hi)


model.segment, model.cluster = spy_segment, spy_cluster
try:
    config.save({"diarize_window_step_s": 3.0, "diarize_cluster_threshold": 0.5,
                 "diarize_cluster_fb": 0.4, "diarize_min_voice_s": 1.0})
    diarize.diarize_pcm(pcm, sr=sr)
    check("шаг окна 3 с", seen.get("step") == 3.0, seen)
    check("порог 0,5 и штраф 0,4", seen.get("threshold") == 0.5 and seen.get("Fb") == 0.4, seen)
    check("минимум речи 1 с = 10 % окна", abs(seen.get("ratio", 0) - 0.1) < 1e-9, seen)
    config.save({"diarize_window_step_s": None, "diarize_cluster_threshold": None,
                 "diarize_cluster_fb": None, "diarize_min_voice_s": None})
    diarize.diarize_pcm(pcm, sr=sr)
    base = diar_onnx.tuning_defaults()
    check("пустые поля — заводские значения модели",
          seen.get("step") == base["step_s"] and seen.get("threshold") == base["threshold"]
          and seen.get("Fb") == base["Fb"] and abs(seen.get("ratio", 0) - 0.2) < 1e-9, seen)
    config.save({"diarize_window_step_s": 99, "diarize_cluster_threshold": "мусор"})
    diarize.diarize_pcm(pcm, sr=sr)
    check("шаг больше окна обрезается до окна, мусор — заводское",
          seen.get("step") == 10.0 and seen.get("threshold") == base["threshold"], seen)
finally:
    model.segment, model.cluster = real_segment, real_cluster
    config.save({k: config.DEFAULTS[k] for k in (
        "diarize_window_step_s", "diarize_cluster_threshold", "diarize_cluster_fb",
        "diarize_min_voice_s")})

say("")
say("=== 8. Отпечатки кусков для отсева эха ===")
vecs = diarize.embed_spans(pcm, [{"start": 1.0, "end": 1.3}, {"start": 1.0, "end": 4.0}], sr=sr)
check("короткий кусок — пусто", vecs[0] is None)
check("длинный кусок — 256 чисел", vecs[1] is not None and len(vecs[1]) == 256)

diarize.use_engine(None)
say("")
say("ИТОГО провалов: %d" % len(FAIL))
for f in FAIL:
    say("   - " + f)
io.open(PROJECT / "tests" / "t100_result.txt", "w", encoding="utf-8").write("\n".join(LINES))
sys.exit(1 if FAIL else 0)
