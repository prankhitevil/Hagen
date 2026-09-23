# -*- coding: utf-8 -*-
"""Проверка 57: точная модель грузится по требованию и выгружается после простоя.

Решение 14.09: точная модель (GigaAM rnnt + torch, около 1,3 ГБ) на
старте программы не греется; грузится при первом обращении; выгружается после
простоя — срок в настройках «precise_idle_min», по умолчанию 30 мин, 0 = держать.
Видео, файлы и «Перечитать точнее» от галочки диктовки не зависят.

Что проверяем — без настоящей модели, на подставных объектах в кеше:
  1. Срок простоя: умолчание 30, ноль и мусор значат «не выгружать».
  2. Выгрузка: давно не трогали — выгружает, недавно — оставляет, срок 0 — ничего.
  3. Модель под распознаванием не выгружается (замок берётся без ожидания).
  4. Обращение к модели продлевает ей жизнь.
  5. Фоновая загрузка для диктовки: грузит, если модели нет; не грузит, если есть.
  6. Сторож простоя один на процесс.
  7. Служба на старте греет только модель эфира.
  8. Диктовка начинает загрузку точной модели, когда человек начал говорить;
     английской диктовке она не нужна.
  9. Интерфейс: поле срока, примечание, ноль не превращается в 30.
 10. Настоящая модель: после выгрузки объект освобождён, память не растёт от
     цикла к циклу (утечка через gigaam.encoder.IMPORT_FLASH_ERR, 14.09).

Настоящие settings.json и база голосов не трогаются (tests\\isolate.py).
Запуск из корня проекта:  .venv\\Scripts\\python.exe tests\\t57_precise_idle.py
"""
import io
import sys
import threading
import time
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
sys.path.insert(0, str(PROJECT / "tests"))
import isolate  # noqa: E402
from harness import LINES, FAIL, say, check, finish  # noqa: E402

isolate.voices()
# Две модели, точная на torch: выгружается после простоя именно она.
isolate.settings(asr_count=2, asr_calls="fast", asr_voice="precise", asr_engine="torch")

import numpy as np  # noqa: E402

from hagen import asr, config  # noqa: E402


class FakeModel:
    """Стоит в кеше вместо настоящей модели: грузить 1,3 ГБ ради проверки незачем."""


def put(name, idle_s):
    """Положить подставную модель, «последний раз трогали idle_s секунд назад»."""
    asr._torch_cache[name] = FakeModel()
    asr._torch_used[name] = time.time() - idle_s


def clear():
    asr._torch_cache.clear()
    asr._torch_used.clear()


say("=== 1. Срок простоя ===")
check("в умолчаниях 30 минут", config.DEFAULTS.get("precise_idle_min") == 30,
      config.DEFAULTS.get("precise_idle_min"))
config.save({"precise_idle_min": 30})
check("30 читается как 30", asr.idle_minutes() == 30.0, asr.idle_minutes())
config.save({"precise_idle_min": 0})
check("0 — не выгружать", asr.idle_minutes() == 0.0, asr.idle_minutes())
config.save({"precise_idle_min": -5})
check("отрицательное — не выгружать", asr.idle_minutes() == 0.0, asr.idle_minutes())
config.save({"precise_idle_min": "мусор"})
check("мусор — не выгружать, без падения", asr.idle_minutes() == 0.0, asr.idle_minutes())
config.save({"precise_idle_min": 30})

say("")
say("=== 2. Выгрузка по простою ===")
clear()
put("old", idle_s=31 * 60)
put("fresh", idle_s=5 * 60)
got = asr.release_idle(minutes=30)
check("давно не трогали — выгружена", got == ["old"] and "old" not in asr._torch_cache, got)
check("недавно трогали — осталась", "fresh" in asr._torch_cache, list(asr._torch_cache))
check("отметка времени выгруженной убрана", "old" not in asr._torch_used, list(asr._torch_used))
check("второй круг ничего не трогает", asr.release_idle(minutes=30) == [])

clear()
put("old", idle_s=10 * 3600)
check("срок 0 — ничего не выгружает", asr.release_idle(minutes=0) == []
      and "old" in asr._torch_cache)
config.save({"precise_idle_min": 0})
check("срок из настроек 0 — тоже ничего", asr.release_idle() == [] and "old" in asr._torch_cache)
config.save({"precise_idle_min": 30})
check("срок из настроек 30 — выгружает", asr.release_idle() == ["old"])

say("")
say("=== 3. Под распознаванием не выгружается ===")
clear()
put("busy", idle_s=60 * 60)
asr._infer_lock.acquire()
try:
    got = asr.release_idle(minutes=30)
    check("идёт распознавание — модель на месте", got == [] and "busy" in asr._torch_cache, got)
finally:
    asr._infer_lock.release()
got = asr.release_idle(minutes=30)
check("распознавание закончилось — выгружена на следующем круге", got == ["busy"], got)

say("")
say("=== 4. Обращение продлевает жизнь ===")
clear()
put("v3_e2e_rnnt", idle_s=29 * 60)
model = asr._torch_cache["v3_e2e_rnnt"]
same = asr._load_torch("v3_e2e_rnnt")      # из кеша: настоящая загрузка не идёт
check("из кеша отдаётся та же модель", same is model)
check("отметка обновлена", time.time() - asr._torch_used["v3_e2e_rnnt"] < 5,
      round(time.time() - asr._torch_used["v3_e2e_rnnt"], 1))
check("после обращения не выгружается", asr.release_idle(minutes=30) == [])

say("")
say("=== 5. Фоновая загрузка для диктовки ===")
clear()
loaded = []
done = threading.Event()
REAL_LOAD = asr._load_torch


def fake_load(name):
    loaded.append((name, threading.current_thread().name))
    asr._torch_cache[name] = FakeModel()
    asr._torch_used[name] = time.time()
    done.set()
    return asr._torch_cache[name]


asr._load_torch = fake_load
try:
    config.save({"offline_model": "v3_e2e_rnnt"})
    asr.preload_precise()
    check("модели нет — загрузка пошла", done.wait(5.0), loaded)
    check("грузит в отдельном потоке, не в зовущем",
          bool(loaded) and loaded[0][1] == "asr-preload", loaded)
    check("грузит именно точную модель", bool(loaded) and loaded[0][0] == "v3_e2e_rnnt", loaded)
    loaded.clear()
    asr._torch_used["v3_e2e_rnnt"] = time.time() - 20 * 60
    asr.preload_precise()
    time.sleep(0.3)
    check("модель уже есть — повторно не грузит", loaded == [], loaded)
    check("…но отметку продлевает", time.time() - asr._torch_used["v3_e2e_rnnt"] < 5)
finally:
    asr._load_torch = REAL_LOAD

say("")
say("=== 6. Сторож простоя один ===")
before = [t for t in threading.enumerate() if t.name == "asr-idle"]
asr._ensure_janitor()
asr._ensure_janitor()
asr._ensure_janitor()
after = [t for t in threading.enumerate() if t.name == "asr-idle"]
check("после трёх вызовов сторож ровно один", len(after) == 1, (len(before), len(after)))
check("сторож — фоновый поток, не держит процесс", after and after[0].daemon)

say("")
say("=== 7. Служба на старте греет только эфир ===")
from hagen import server  # noqa: E402

seen = {}
REAL_WARMUP = asr.warmup


def fake_warmup(live=True, precise=False):
    seen.update(live=live, precise=precise)
    return {"live": 0.0}


asr.warmup = fake_warmup
try:
    for dictate_on in (False, True):
        seen.clear()
        config.save({"dictate_enabled": dictate_on})
        server._warmup_async()
        check("диктовка %s — эфир прогрет" % ("вкл" if dictate_on else "выкл"),
              seen.get("live") is True, seen)
        check("диктовка %s — точная модель НЕ греется" % ("вкл" if dictate_on else "выкл"),
              seen.get("precise") is False, seen)
finally:
    asr.warmup = REAL_WARMUP
    config.save({"dictate_enabled": False})

say("")
say("=== 8. Диктовка начинает загрузку, когда человек заговорил ===")
import tempfile  # noqa: E402

from hagen import dictate  # noqa: E402
from hagen.platform.windows import loopback  # noqa: E402

# Настоящая история диктовок — не наша: подменяем файл временным.
dictate.HISTORY_PATH = Path(tempfile.mkdtemp(prefix="t57_hist_")) / "history.json"


class FakeMic:
    def __init__(self, device_index=None, on_audio=None, block_ms=100):
        self.on_audio = on_audio

    def start(self):
        pass

    def stop(self):
        pass


REAL_MIC = loopback.MicRecorder
REAL_PRELOAD = asr.preload_precise
calls = []
loopback.MicRecorder = FakeMic
asr.preload_precise = lambda: calls.append(time.time())
config.save({"dictate_sound": False})
try:
    for lang, want in (("ru", 1), ("en", 0)):
        calls.clear()
        config.save({"dictate_lang": lang})
        d = dictate.Dictation(busy=lambda: False, paste=lambda t: True)
        d.stage = "idle"
        d.toggle()
        stage = d.stage
        d.cancel()
        check("диктовка %s: слушает" % lang, stage == "listening", stage)
        check("диктовка %s: загрузок точной модели %d" % (lang, want), len(calls) == want, len(calls))

    calls.clear()
    config.save({"dictate_lang": "ru"})
    d = dictate.Dictation(busy=lambda: True, paste=lambda t: True)
    d.stage = "idle"
    d.toggle()
    check("идёт запись совещания — диктовка спит и модель не грузит", calls == [], d.stage)
finally:
    loopback.MicRecorder = REAL_MIC
    asr.preload_precise = REAL_PRELOAD
    config.save({"dictate_lang": "ru"})

say("")
say("=== 9. Интерфейс ===")
html = io.open(PROJECT / "hagen" / "static" / "index.html", encoding="utf-8").read()
js = io.open(PROJECT / "hagen" / "static" / "app.js", encoding="utf-8").read()
pane = html[html.find('id="t-dictate"'):]
pane = pane[:pane.find('class="tabpane', 20)]
check("поле срока — во вкладке «Набор текста»", 'id="set-precise-idle"' in pane)
check("примечание про память рядом", "1,3 ГБ" in pane and "выгружается" in pane)
check("примечание: видео и «Перечитать точнее» от галочки не зависят",
      "от галочки выше они не зависят" in pane)
check("примечание: 0 — держать всё время", "0 — держать в памяти всё время" in pane)
check("страница читает срок", "set-precise-idle').value" in js and "s.precise_idle_min" in js)
check("страница сохраняет срок", "precise_idle_min:" in js)
check("ноль не превращается в 30 при чтении", "s.precise_idle_min || 30" not in js)

say("")
say("=== 10. Настоящая модель: выгрузка действительно освобождает ===")
# Найдено 14.09: gigaam.encoder при ленивом подключении сохранял ошибку flash_attn
# вместе со следом вызовов, и в этом следе навсегда оставалась первая модель.
# Выгрузка ничего не освобождала, повторная загрузка добавляла 0,9 ГБ сверху.
# Проверяем по слабой ссылке и по памяти процесса: если после обновления gigaam
# или правки загрузки модель снова где-то пришпилится — здесь станет красно.
import gc  # noqa: E402
import weakref  # noqa: E402

import psutil  # noqa: E402

clear()
if asr.onnx_ready("v3_e2e_ctc") and (asr.CKPT_DIR / "v3_e2e_rnnt.ckpt").exists():
    proc = psutil.Process()

    def private_mb():
        return proc.memory_info().private / 2**20

    speech = np.random.uniform(-0.1, 0.1, 16000 * 5).astype(np.float32)
    after = []
    for cycle in (1, 2, 3):
        asr.transcribe_precise(speech, words=True)
        ref = weakref.ref(asr._torch_cache["v3_e2e_rnnt"])
        asr._torch_used["v3_e2e_rnnt"] = time.time() - 3600
        got = asr.release_idle(minutes=30)
        gc.collect()
        check("цикл %d: выгружена" % cycle, got == ["v3_e2e_rnnt"], got)
        check("цикл %d: объект модели освобождён, никто не держит" % cycle, ref() is None)
        after.append(private_mb())
    grow = after[-1] - after[0]
    check("память после выгрузки не растёт от цикла к циклу (±150 МБ)", grow < 150,
          "после выгрузок: %s МБ" % [round(x) for x in after])
else:
    say("   (пропуск: настоящей модели в папке нет)")

clear()
sys.exit(finish("t57"))
