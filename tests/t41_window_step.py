# -*- coding: utf-8 -*-
"""Проверка 41: шаг окна разметки — сколько выигрываем и что теряем.

Модель смотрит на звук окном 10 секунд и по умолчанию сдвигает его на 1 секунду.
Удвоение шага вдвое сокращает число окон, а с ним и работу обоих тяжёлых шагов.
Вопрос, ради которого написана эта проверка: не разъезжается ли при этом сама
разметка — те же ли говорящие и те же ли границы.

Считаем не «похоже на глаз», а согласие по кадрам: идём по записи шагом 0,1 с и
смотрим, совпал ли говорящий. Это та же мера, по которой оценивают диаризацию.

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

isolate.voices()

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


from hagen import audio_io, config, diarize  # noqa: E402

GRID = 0.1          # шаг сетки согласия, секунды

# С 15.09 разметка перед каждым запуском сама ставит шаг окна и остальные ручки
# из настроек (diarize.apply_tuning). Шаг, поставленный прямо у пайплайна, она бы
# тут же вернула к настройкам — оба замера шли бы с одним шагом. Поэтому шаг
# подменяем в чтении настроек, в памяти; остальные ручки — заводские модели,
# чтобы продвинутые настройки машины не влияли на замер.
STEP = {"value": None}
_real_setting = diarize._setting


def _pinned_setting(key):
    if key == "diarize_window_step_s":
        return STEP["value"] if STEP["value"] is not None else _real_setting(key)
    if key in ("diarize_cluster_threshold", "diarize_cluster_fb", "diarize_min_voice_s"):
        return None
    return _real_setting(key)


diarize._setting = _pinned_setting


def run_with_step(pipe, pcm, sr, step_s):
    """Разметить с заданным шагом окна. Возвращает (секунды, результат).

    Настройки на диске не трогаем: шаг подменён в памяти (см. выше). Так
    проверка не оставляет следов, даже если оборвётся на середине.
    """
    STEP["value"] = float(step_s)
    diarize._set_window_step(pipe, float(step_s))
    t0 = time.time()
    res = diarize.diarize_pcm(pcm, sr=sr)
    return round(time.time() - t0, 1), res


def speaker_grid(res, total_s):
    """Кто говорит в каждой десятой доле секунды. None — тишина."""
    cells = int(total_s / GRID) + 1
    grid = [None] * cells
    for turn in res.get("turns") or []:
        start = float(turn.get("start") or 0.0)
        end = float(turn.get("end") or 0.0)
        who = turn.get("speaker")
        for i in range(int(start / GRID), min(int(end / GRID) + 1, cells)):
            grid[i] = who
    return grid


def agreement(a, b):
    """Доля кадров, где обе разметки говорят одно и то же.

    Имена говорящих у двух прогонов свои, поэтому сначала подбираем, какой
    номер из первого прогона какому номеру из второго соответствует — по тому,
    как часто они встречаются вместе.
    """
    pairs: dict = {}
    for x, y in zip(a, b):
        if x is None and y is None:
            continue
        pairs[(x, y)] = pairs.get((x, y), 0) + 1
    mapping: dict = {}
    used = set()
    for (x, y), _ in sorted(pairs.items(), key=lambda kv: -kv[1]):
        if x in mapping or y in used:
            continue
        mapping[x] = y
        used.add(y)
    same = sum(1 for x, y in zip(a, b) if mapping.get(x, x) == y)
    return round(100.0 * same / max(1, len(a)), 1)


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
say("   загружаю модель…")
pipe = diarize.load_pipeline()

results = {}
for step in (1.0, 2.0):
    say("")
    say("=== Шаг окна %.0f с ===" % step)
    spent, res = run_with_step(pipe, pcm, sr, step)
    turns = res.get("turns") or []
    speakers = sorted({t.get("speaker") for t in turns})
    results[step] = {"time": spent, "res": res, "speakers": speakers,
                     "turns": len(turns)}
    say("   время: %.1f с (RTF %.3f)" % (spent, spent / total))
    say("   голосов: %d, интервалов: %d" % (len(speakers), len(turns)))

say("")
say("=== 2. Что дало удвоение шага ===")
base, fast = results[1.0], results[2.0]
speedup = base["time"] / max(fast["time"], 0.01)
say("   было %.1f с → стало %.1f с" % (base["time"], fast["time"]))
say("   ускорение: %.2f раза" % speedup)
check("быстрее, а не медленнее", speedup > 1.0, True)

check("голосов столько же", len(fast["speakers"]), len(base["speakers"]))
grid_a = speaker_grid(base["res"], total)
grid_b = speaker_grid(fast["res"], total)
same = agreement(grid_a, grid_b)
say("   согласие разметок по кадрам: %.1f %%" % same)
check("разметка не разъехалась (не меньше 90 %)", same >= 90.0, True)

say("")
say("=== 3. Откат на месте ===")
diarize._set_window_step(pipe, 1.0)
check("шаг вернулся", float(getattr(pipe._segmentation, "step", 0)), 1.0)
# Значение закреплено намеренно: если кто-то поменяет умолчание, проверка
# об этом скажет — решение принималось по замеру, а не наугад.
check("в умолчаниях стоит удвоенный шаг",
      float(config.DEFAULTS.get("diarize_window_step_s") or 0), 2.0)
check("шаг больше окна не ставится",
      (diarize._set_window_step(pipe, 99.0),
       float(getattr(pipe._segmentation, "step", 0)))[1], 10.0)
diarize._set_window_step(pipe, 1.0)

say("")
say("ИТОГО провалов: %d" % len(FAIL))
for f in FAIL:
    say("   - " + f)
io.open(PROJECT / "tests" / "t41_result.txt", "w", encoding="utf-8").write("\n".join(LINES))
sys.exit(1 if FAIL else 0)
