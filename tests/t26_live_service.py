# -*- coding: utf-8 -*-
"""Проверка 26: живая запись звонка, которую ведёт служба, — распознавание по ходу.

Микрофон и петлю вывода открывает служба, как при настоящем звонке. В колонки
играет тестовое «совещание», и речь собеседников должна распознаваться, пока
запись ещё идёт, а не только после «Стопа».

Чем отличается от соседей: t15 проверяет, что звук колонок доходит до файла
дорожки; t38 — живой путь микрофона. Здесь — что эфир распознаёт собеседников
на лету и не путает дорожки.

Что проверяем:
  1. служба видит микрофоны и петли, пробы открываются;
  2. запись идёт с обеими дорожками;
  3. реплики собеседников появляются ПО ХОДУ записи;
  4. в них слова из проигранного «совещания»;
  5. реплики с петли не подписаны «Я» — дорожки не смешаны.

Звук НАСТОЯЩИЙ и слышен в комнате — около полуминуты. Служба своя, внутри
процесса: запущенную рядом программу проверка не трогает. Настройки и база
голосов — временные.

Запуск из корня проекта:  .venv\\Scripts\\python.exe tests\\t26_live_service.py
"""
import io
import sys
import time
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
sys.path.insert(0, str(PROJECT / "tests"))
import isolate  # noqa: E402

LINES = []
FAIL = []


def say(msg=""):
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
    say(("   ok    " if ok else "   ПЛОХО ") + name
        + (": " + str(detail)[:300] if detail != "" else ""))


import winsound  # noqa: E402

from hagen import platform, store  # noqa: E402

PLAY = PROJECT / "tests" / "meeting.wav"
#: Первые полминуты «совещания»: в них все четыре слова, по которым узнаём речь.
PLAY_S = 30.0
WORDS = ("срок", "закупк", "смет", "подрядчик")

speakers = next((d for d in platform.audio().list_loopback_devices()
                 if d.get("is_default_output")), None)

isolate.voices()
isolate.settings(mode="online", record_far=True,
                 far_device_index=(speakers or {}).get("index"),
                 call_watch_enabled=False, mic_pill=False, screenshots_enabled=False,
                 diarize_auto=False)

from fastapi.testclient import TestClient  # noqa: E402

from hagen import server  # noqa: E402

server._start_dictation = lambda: None
ORIGIN = {"Origin": "http://127.0.0.1:8787"}

rec_id = None
try:
    with TestClient(server.app, base_url="http://127.0.0.1:8787") as cli:
        say("=== 1. Устройства, которые видит служба ===")
        r = cli.post("/api/devices", headers=ORIGIN, params={"probe": "true"})
        dev = r.json() if r.status_code == 200 else {}
        check("список устройств отдан", r.status_code == 200, r.text[:200])
        check("микрофоны есть", bool(dev.get("mics")), len(dev.get("mics") or []))
        check("петли вывода есть", bool(dev.get("loopback")), len(dev.get("loopback") or []))
        check("проба микрофона открылась", (dev.get("mic_probe") or {}).get("ok"),
              dev.get("mic_probe"))
        check("проба петли открылась", (dev.get("far_probe") or {}).get("ok"),
              dev.get("far_probe"))
        say("   играем и снимаем петлю: %s" % (speakers or {}).get("name"))

        say("")
        say("=== 2. «Старт» ===")
        r = cli.post("/api/recordings", headers=ORIGIN,
                     json={"title": "Проверка 26 — живой звонок", "mode": "online"})
        check("запись началась", r.status_code == 200, r.text[:200])
        rec_id = (r.json().get("meta") or {}).get("id") if r.status_code == 200 else None
        time.sleep(3.0)                      # устройства открываются в фоне

        say("")
        say("=== 3. Играю «совещание», смотрю реплики по ходу ===")
        winsound.PlaySound(str(PLAY), winsound.SND_FILENAME | winsound.SND_ASYNC)
        seen = 0
        t0 = time.time()
        while time.time() - t0 < PLAY_S:
            time.sleep(2.0)
            segs = cli.get("/api/recordings/%s" % rec_id).json().get("segments") or []
            for s in segs[seen:]:
                say("   [%4.1f с] %-4s %-10s %s" % (time.time() - t0, s["track"],
                                                    s.get("speaker"), (s.get("text") or "")[:56]))
            seen = len(segs)
        winsound.PlaySound(None, 0)
        # Эфир закрывает фразу после 2,2 с тишины или на 24-й секунде
        # (silence_finalize_ms, max_phrase_seconds). В «совещании» паузы между
        # репликами по полсекунды, поэтому за полминуты по ходу выходит одна
        # длинная реплика — этого достаточно: распознавание идёт до «Стопа».
        live_far = [s for s in segs if s["track"] == "far"]
        check("реплики собеседников появились ПО ХОДУ записи", len(live_far) >= 1,
              "%d реплик с петли до «Стопа»" % len(live_far))

        say("")
        say("=== 4. «Стоп» ===")
        r = cli.post("/api/recordings/%s/stop" % rec_id, headers=ORIGIN, json={})
        check("запись остановлена", r.status_code == 200, r.text[:200])
        data = cli.get("/api/recordings/%s" % rec_id).json()
        meta, segs = data.get("meta") or {}, data.get("segments") or []
        far = [s for s in segs if s["track"] == "far"]
        text = " ".join((s.get("text") or "").lower() for s in far)
        found = [w for w in WORDS if w in text]
        say("   дорожки: %s | реплик с петли: %d" % (meta.get("tracks"), len(far)))
        check("обе дорожки записаны", {"mic", "far"} <= set(meta.get("tracks") or []),
              meta.get("tracks"))
        check("в речи собеседников — слова из «совещания»", len(found) >= 2,
              "нашлись: %s" % found)
        check("реплики с петли не подписаны «Я» — дорожки не смешаны",
              far and all(s.get("speaker") != "Я" for s in far),
              [s.get("speaker") for s in far][:6])

        say("")
        say("=== 5. Уборка ===")
        r = cli.delete("/api/recordings/%s?scope=all" % rec_id, headers=ORIGIN)
        check("проверочная запись удалена", r.status_code == 200, r.text[:200])
        rec_id = None
except Exception:
    import traceback

    check("путь без сбоев", False, traceback.format_exc()[-600:])
finally:
    try:
        winsound.PlaySound(None, 0)
    except Exception:
        pass
    if rec_id:
        try:
            store.delete(rec_id)
        except Exception:
            pass

say("")
say("ИТОГО провалов: %d" % len(FAIL))
for f in FAIL:
    say("   - " + f)
io.open(PROJECT / "tests" / "t26_result.txt", "w", encoding="utf-8").write("\n".join(LINES))
sys.exit(1 if FAIL else 0)
