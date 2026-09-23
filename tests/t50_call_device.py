# -*- coding: utf-8 -*-
"""Проверка 50: звук собеседников пишется с того устройства, куда играет звонок.

Живой звонок 13.09: Windows держала «по умолчанию» выход монитора Dell, помощник
записи собеседников открыл его, и дорожка собеседников весь звонок (263 с) была
ровными нулями — хотя разговор шёл в колонках.

  1. Устройство звонка по аудиосессиям: где играет программа звонка, там и
     пишем; при нескольких — где громче.
  2. Номер петлевого устройства по имени Windows.
  3. Настоящий помощник записи получает имя устройства и открывает именно его;
     незнакомое имя — честно откатывается на устройство по умолчанию.
  4. Сторож переходит за звонком, если Teams вывел звук на другое устройство.
"""
import io
import sys
import threading
import time
import wave
from pathlib import Path

import numpy as np

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
import isolate  # noqa: E402  настоящая база голосов не трогается
from harness import LINES, FAIL, say, check, finish  # noqa: E402

isolate.voices()

SR = 16000


from hagen import live, store  # noqa: E402
from hagen.platform.windows import capture, loopback  # noqa: E402
from hagen.platform.windows import desktop  # noqa: E402

DELL = "3 - Dell S2417DG (AMD High Definition Audio Device)"
SVEN = "Динамики (2- SVEN SB-G1400)"


def sess(proc, device, state=1, peak=0.0):
    return {"process": proc, "pid": 1, "state": state, "peak": peak, "device": device}


say("=== 1. Устройство звонка ===")
got = desktop.call_output_device([sess("ms-teams.exe", SVEN, peak=0.2), sess("ms-teams.exe", DELL)])
check("Teams играет в колонки и молчит в мониторе — колонки", got and got["device"] == SVEN, got)
got = desktop.call_output_device([sess("ms-teams.exe", DELL, peak=0.1)])
check("Teams играет только в монитор — монитор", got and got["device"] == DELL, got)
got = desktop.call_output_device([sess("python.exe", SVEN, peak=0.5)])
check("играет не программа звонка — устройства звонка нет", got is None, got)
got = desktop.call_output_device([sess("ms-teams.exe", SVEN, state=0)])
check("сессия Teams простаивает — устройства звонка нет", got is None, got)

say("")
say("=== 2. Номер по имени ===")
devs = [{"index": 20, "name": DELL}, {"index": 27, "name": SVEN}, {"index": 5, "name": "Динамики (Steam Streaming Speakers)"}]
check("точное имя", loopback.index_for_name(SVEN, devs) == 27)
check("имя с хвостом [Loopback]", loopback.index_for_name(DELL + " [Loopback]", devs) == 20)
check("незнакомое имя — нет", loopback.index_for_name("Наушники Jabra", devs) is None)

say("")
say("=== 3. Настоящий помощник записи ===")
real = loopback.list_devices()
names = [d["name"] for d in real]
say("   петлевые устройства: %s" % "; ".join(names))
pick = [n for n in names if "SVEN" in n] + [n for n in names if "Dell" in n]
pick = pick or names[:2]
for want in pick[:2]:
    rec = capture.LoopbackProcess(want_name=want)
    try:
        rec.start()
        opened = rec.device_name
    except Exception as err:
        opened = "не открылось: %s" % err
    finally:
        rec.stop()
    check("просили «%s» — открыто оно" % want, loopback.similar(opened, want), "%s | %s" % (opened, rec.choice))
    check("в журнале объяснено, почему это устройство", "звонок выводит звук" in rec.choice, rec.choice)
rec = capture.LoopbackProcess(want_name="Несуществующие колонки 9000")
try:
    rec.start()
    ok = bool(rec.device_name)
finally:
    rec.stop()
check("незнакомое имя — устройство по умолчанию, с объяснением",
      ok and "не нашлось" in rec.choice and "по умолчанию в Windows" in rec.choice, rec.choice)

say("")
say("=== 4. Сторож переходит за звонком ===")
live.DeviceCapture.FOLLOW_EVERY = 2          # чтобы проверка шла секунды, а не минуту
CALL = {"device": DELL}
live.DeviceCapture._call_device = lambda self: CALL["device"]


class FakeRec:
    def __init__(self, on_audio, name):
        self.on_audio, self.device_name, self.error = on_audio, name, None
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._run, daemon=True)
        self._last = time.time()

    def start(self):
        self._t0 = time.time()
        self._fed = 0
        self._t.start()

    def _run(self):
        while not self._stop.wait(0.1):
            n = int((time.time() - self._t0) * SR) - self._fed
            if n > 0:
                self._fed += n
                self._last = time.time()
                self.on_audio(np.full(n, 0.01 if "SVEN" in self.device_name else 0.0, dtype=np.float32))

    def stop(self):
        self._stop.set()
        self._t.join(timeout=2)

    alive = property(lambda self: not self._stop.is_set())
    stalled = property(lambda self: time.time() - self._last > 2.5)

    def status(self):
        return {"device": self.device_name, "alive": self.alive, "silent": False}


opened = []


class Cap(live.DeviceCapture):
    def _open_mic(self, feed):
        r = FakeRec(feed, "микрофон")
        r.start()
        return r

    def _open_far(self, feed):
        name = self._call_device() or "по умолчанию"
        opened.append(name)
        r = FakeRec(feed, name)
        r.start()
        return r


meta = store.create(title="Проверка 50", mode="online", source="live")
rid = meta["id"]
events = []
sess_ = live.LiveSession(rid, mode="online", on_event=events.append)
cap = Cap(sess_)
cap.start(want_far=True)
time.sleep(2.5)
check("пока звонок в мониторе — пишем монитор", cap.far and cap.far.device_name == DELL)
CALL["device"] = SVEN                        # Teams перевёл звук в колонки
moved = False
for _ in range(100):
    time.sleep(0.1)
    if cap.far is not None and cap.far.device_name == SVEN:
        moved = True
        break
check("Teams перевёл звук в колонки — запись перешла за ним", moved, opened)
check("перешли один раз, без дёрганья", opened == [DELL, SVEN], opened)
texts = [e.get("text") or "" for e in events if e.get("type") == "notice"]
check("человеку сказано, куда переключилась запись", any("переключил туда" in t for t in texts), texts)
time.sleep(3.0)
check("больше не переключаемся", opened == [DELL, SVEN], opened)
cap.stop()
sess_.stop()


def secs(tr):
    with wave.open(str(store.track_path(rid, tr)), "rb") as wf:
        return wf.getnframes() / SR


check("дорожки после перехода в ногу (±0,4 с)", abs(secs("mic") - secs("far")) <= 0.4,
      "%.2f / %.2f" % (secs("mic"), secs("far")))
store.delete(rid)

sys.exit(finish("t50"))
