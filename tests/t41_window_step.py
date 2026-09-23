# -*- coding: utf-8 -*-
"""Проверка 41: шаг окна разметки — сколько выигрываем и что теряем.

Модель смотрит на звук окном 10 секунд и по умолчанию сдвигает его на 1 секунду.
Удвоение шага вдвое сокращает число окон, а с ним и работу обоих тяжёлых шагов.
Вопрос, ради которого написана эта проверка: не разъезжается ли при этом сама
разметка — те же ли говорящие и те же ли границы.

Считаем не «похоже на глаз», а согласие по кадрам: идём по записи шагом 0,1 с и
смотрим, совпал ли говорящий. Это та же мера, по которой оценивают диаризацию.

Гоняется на каждом движке, который есть на этой машине: ONNX — всегда,
pyannote — если стоит пакет и модель лежит на диске.

Запускать из корня проекта:  .venv\\Scripts\\python.exe tests\\t41_window_step.py
Гоняет настоящие модели, занимает несколько минут.
"""
import io
import sys
import time
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
import isolate  # noqa: E402  настоящая база голосов не трогается
from harness import LINES, FAIL, say, expect as check, finish  # noqa: E402

isolate.voices()


from hagen import audio_io, config, diar_pyannote, diarize  # noqa: E402
from diar_measure import agreement, speaker_grid  # noqa: E402

# С 15.09 разметка перед каждым запуском сама берёт шаг окна и остальные ручки
# из настроек (diarize.tuning). Поэтому шаг подменяем в чтении настроек, в
# памяти; остальные ручки — заводские модели, чтобы продвинутые настройки
# машины не влияли на замер.
STEP = {"value": None}
_real_setting = diarize._setting


def _pinned_setting(key):
    if key == "diarize_window_step_s":
        return STEP["value"] if STEP["value"] is not None else _real_setting(key)
    if key in ("diarize_cluster_threshold", "diarize_cluster_fb", "diarize_min_voice_s"):
        return None
    return _real_setting(key)


diarize._setting = _pinned_setting


def run_with_step(pcm, sr, step_s):
    """Разметить с заданным шагом окна. Возвращает (секунды, результат).

    Настройки на диске не трогаем: шаг подменён в памяти (см. выше). Так
    проверка не оставляет следов, даже если оборвётся на середине.
    """
    STEP["value"] = float(step_s)
    t0 = time.time()
    res = diarize.diarize_pcm(pcm, sr=sr)
    return round(time.time() - t0, 1), res


def engines():
    """Движки этой машины. pyannote грузится с диска: токен проверке не нужен."""
    out = ["onnx"]
    if not diar_pyannote.installed():
        say("   (pyannote пропущен: пакет не установлен)")
        return out
    name = config.DEFAULTS["diarize_model"]
    try:
        pipe = diar_pyannote._load_one(name, "", diarize._threads())
    except Exception as err:
        say("   (pyannote пропущен: модель не загрузилась с диска: %s)" % str(err)[:120])
        return out
    diar_pyannote.use_pipeline(pipe, name)
    out.append("pyannote")
    return out


wav = PROJECT / "tests" / "meeting.wav"
say("=== 1. Запись для замера ===")
check("файл на месте", wav.exists(), True)
if not wav.exists():
    say("нет tests\\meeting.wav — замер невозможен")
    sys.exit(1)

pcm, sr = audio_io.read_wav(wav)
total = len(pcm) / float(sr)
say("   %s: %.1f с, %d Гц" % (wav.name, total, sr))

say("")
say("   загружаю модели…")
for engine in engines():
    diarize.use_engine(engine)
    results = {}
    for step in (1.0, 2.0):
        say("")
        say("=== %s: шаг окна %.0f с ===" % (engine, step))
        spent, res = run_with_step(pcm, sr, step)
        turns = res.get("turns") or []
        speakers = sorted({t.get("speaker") for t in turns})
        results[step] = {"time": spent, "res": res, "speakers": speakers,
                         "turns": len(turns)}
        say("   время: %.1f с (RTF %.3f)" % (spent, spent / total))
        say("   голосов: %d, интервалов: %d" % (len(speakers), len(turns)))

    say("")
    say("=== %s: что дало удвоение шага ===" % engine)
    base, fast = results[1.0], results[2.0]
    speedup = base["time"] / max(fast["time"], 0.01)
    say("   было %.1f с → стало %.1f с" % (base["time"], fast["time"]))
    say("   ускорение: %.2f раза" % speedup)
    check("%s: быстрее, а не медленнее" % engine, speedup > 1.0, True)

    check("%s: голосов столько же" % engine, len(fast["speakers"]), len(base["speakers"]))
    grid_a = speaker_grid(base["res"].get("turns"), total)
    grid_b = speaker_grid(fast["res"].get("turns"), total)
    same = agreement(grid_a, grid_b)
    say("   согласие разметок по кадрам: %.1f %%" % same)
    check("%s: разметка не разъехалась (не меньше 90 %%)" % engine, same >= 90.0, True)
diarize.use_engine(None)

say("")
say("=== Шаг окна по умолчанию ===")
# Значение закреплено намеренно: если кто-то поменяет умолчание, проверка
# об этом скажет — решение принималось по замеру, а не наугад.
check("в умолчаниях стоит удвоенный шаг",
      float(config.DEFAULTS.get("diarize_window_step_s") or 0), 2.0)

pipe = diar_pyannote._pipeline
if pipe is not None:
    say("")
    say("=== pyannote: откат шага на месте ===")
    diar_pyannote._set_window_step(pipe, 1.0)
    check("шаг вернулся", float(getattr(pipe._segmentation, "step", 0)), 1.0)
    check("шаг больше окна не ставится",
          (diar_pyannote._set_window_step(pipe, 99.0),
           float(getattr(pipe._segmentation, "step", 0)))[1], 10.0)
    diar_pyannote._set_window_step(pipe, 1.0)

sys.exit(finish("t41"))
