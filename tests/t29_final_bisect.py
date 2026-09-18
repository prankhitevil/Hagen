# -*- coding: utf-8 -*-
"""Окончательная бисекция: что в запуске службы отнимает запись.

Пробуем ffmpeg (самый надёжный путь) из рабочего потока при разных вариантах
того, что делает главный поток.
"""
from __future__ import annotations

import io
import sys
import threading
import time
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

MODE = sys.argv[1] if len(sys.argv) > 1 else "sleep"
RESULT = PROJECT / "tests" / ("t29_%s.txt" % MODE)
LINES = []


def say(msg):
    LINES.append(str(msg))
    io.open(RESULT, "w", encoding="utf-8").write("\n".join(LINES))


done = threading.Event()
HOLDER = {"srv": None}


def probe(tag):
    from hagen.platform.windows import capture

    dev = capture.default_mic_device()
    if dev is None:
        say("%s: устройств нет" % tag)
        return False
    r = capture.probe_device(dev, 0.8)
    ok = bool(r.get("ok"))
    say("%-22s %s %s" % (tag, "OK" if ok else "FAIL", str(r.get("reason") or "")[:70]))
    return ok


def worker():
    time.sleep(5)
    probe("проба ffmpeg")
    done.set()
    srv = HOLDER.get("srv")
    if srv is not None:
        srv.should_exit = True


def main() -> int:
    say("режим: %s" % MODE)
    threading.Thread(target=worker, daemon=True).start()

    if MODE == "sleep":
        while not done.is_set():
            time.sleep(0.2)

    elif MODE == "logging":
        import run as runner

        runner.setup_logging("INFO")
        while not done.is_set():
            time.sleep(0.2)

    elif MODE == "asr":
        from hagen import asr

        asr.warmup(live=True)
        while not done.is_set():
            time.sleep(0.2)

    elif MODE == "uvicorn_main":
        import uvicorn

        from hagen import server

        cfg = uvicorn.Config(server.app, host="127.0.0.1", port=8796,
                            log_level="error", access_log=False)
        srv = uvicorn.Server(cfg)
        HOLDER["srv"] = srv
        srv.run()

    elif MODE == "full":
        import run as runner

        runner.setup_logging("INFO")
        import uvicorn

        from hagen import server

        cfg = uvicorn.Config(server.app, host="127.0.0.1", port=8795,
                            log_level="error", access_log=False)
        srv = uvicorn.Server(cfg)
        HOLDER["srv"] = srv
        srv.run()

    say("готово")
    return 0


if __name__ == "__main__":
    sys.exit(main())
