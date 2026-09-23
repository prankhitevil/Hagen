# -*- coding: utf-8 -*-
"""Отдельный процесс, который пишет петлю вывода и отдаёт её в стандартный поток.

Зачем отдельный процесс. На этой машине захват звука внутри долгоживущего
процесса ненадёжен: один и тот же код то открывает устройство, то отвечает
«Invalid device» (-9996), причём сразу для всех устройств и всех звуковых API.
Короткоживущий процесс открывает устройство стабильно — так же ведёт себя
ffmpeg, у которого на каждую запись свой процесс. Поэтому служба запускает
этот помощник на время записи и читает из него готовый поток 16 кГц моно.

Запуск:
    python -m hagen.platform.windows.loopback_worker [--device N] [--no-keep-alive]
Выводит в stdout непрерывный поток float32 little-endian, 16000 Гц, моно.
Служебные сообщения идут в stderr строками вида "INFO ..." и "ERR ...".
"""
from __future__ import annotations

import argparse
import os
import sys
import time


def _err(msg: str) -> None:
    try:
        sys.stderr.write("ERR %s\n" % msg)
        sys.stderr.flush()
    except Exception:
        pass


def _info(msg: str) -> None:
    try:
        sys.stderr.write("INFO %s\n" % msg)
        sys.stderr.flush()
    except Exception:
        pass


def main() -> int:
    ap = argparse.ArgumentParser(add_help=False)
    ap.add_argument("--device", type=int, default=None)
    ap.add_argument("--device-name", default=None,
                    help="устройство вывода, куда программа звонка выводит звук")
    ap.add_argument("--no-keep-alive", action="store_true")
    ap.add_argument("--retries", type=int, default=3)
    ap.add_argument("--parent-pid", type=int, default=0,
                    help="служба, ради которой запущен помощник: её нет — выходим")
    args, _unknown = ap.parse_known_args()

    # Служебные строки идут по-русски, а служба читает их как UTF-8. Без
    # явной кодировки в консоли с кодовой страницей 866 имя устройства
    # приходило кракозябрами, и «открыто устройство» не распознавалось.
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    # Помощник запускается отдельным процессом, и папку проекта ему надо найти
    # самому: отсюда до корня четыре шага — windows, platform, hagen, корень.
    here = os.path.abspath(__file__)
    for _ in range(4):
        here = os.path.dirname(here)
    if here not in sys.path:
        sys.path.insert(0, here)

    import numpy as np

    from hagen.platform.windows import loopback

    out = sys.stdout.buffer

    def on_audio(pcm) -> None:
        try:
            out.write(np.ascontiguousarray(pcm, dtype="<f4").tobytes())
            out.flush()
        except Exception:
            # служба закрыла канал — выходим тихо
            raise SystemExit(0)

    rec = None
    last_err = ""
    for attempt in range(1, max(1, args.retries) + 1):
        try:
            rec = loopback.LoopbackRecorder(
                device_index=args.device,
                on_audio=on_audio,
                keep_alive=not args.no_keep_alive,
                open_retries=1,
                want_name=args.device_name,
            )
            rec.start()
            if rec.choice:
                _info("выбор устройства: %s" % rec.choice)
            _info("открыто устройство [%s] %s, %d Гц, %d кан."
                  % (rec.device_index, rec.device_name, rec.native_sr, rec.channels))
            break
        except Exception as err:
            last_err = str(err)
            rec = None
            _info("попытка %d из %d не удалась" % (attempt, args.retries))
            time.sleep(0.35)

    if rec is None:
        _err(last_err or "устройство не открылось")
        return 2

    def parent_gone() -> bool:
        # Служба убита жёстко, а устройство молчит — обрыва канала помощник не
        # увидит и держал бы устройство вечно.
        if not args.parent_pid:
            return False
        from . import app

        return not app.process_alive(int(args.parent_pid))

    try:
        while True:
            time.sleep(0.5)
            if not rec.alive:
                _err("поток записи закрылся сам")
                return 3
            if parent_gone():
                _info("службы больше нет — выхожу")
                return 0
    except KeyboardInterrupt:
        return 0
    except SystemExit:
        return 0
    finally:
        try:
            rec.stop()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
