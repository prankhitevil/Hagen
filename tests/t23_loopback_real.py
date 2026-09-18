# -*- coding: utf-8 -*-
"""Проверка 23: отдаёт ли петля вывода звук, когда он действительно играет.

Это определяет, возможна ли дорожка собеседников «как в ТЗ» — цифровым потоком
с устройства вывода, без «Стерео микшера» и без обращения к администраторам.
"""
from __future__ import annotations

import io
import subprocess
import sys
import time
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

import numpy as np  # noqa: E402

from hagen import asr, audio_io, platform  # noqa: E402
from hagen.platform.windows import loopback  # noqa: E402

LINES = []


def say(msg=""):
    LINES.append(str(msg))
    try:
        print(str(msg).encode("ascii", "replace").decode("ascii"), flush=True)
    except Exception:
        pass


def main():
    say("=== устройства петли (один проход перечисления) ===")
    devs = loopback.list_devices()
    target = None
    for d in devs:
        marks = []
        if d["is_default_output"]:
            marks.append("вывод по умолчанию")
        if d["is_communications"]:
            marks.append("устройство связи")
        if d["recommended"]:
            marks.append("РЕКОМЕНДУЮ")
            target = d
        say("   [%3d] %-46s %s" % (d["index"], d["name"][:46], ", ".join(marks)))
    if target is None and devs:
        target = devs[0]
    if target is None:
        say("устройств нет")
        return 1

    got = []
    rec = loopback.LoopbackRecorder(device_index=target["index"],
                                    on_audio=lambda p: got.append(p.copy()))
    say("")
    say("открываю петлю на [%d] %s" % (target["index"], target["name"]))
    try:
        rec.start()
    except Exception as err:
        say("ОТКАЗ при открытии: %s" % str(err)[-140:])
        return 1
    say("открыта: %d Гц, %d кан." % (rec.native_sr, rec.channels))

    say("проигрываю тестовую запись на устройство вывода…")
    play = subprocess.Popen(
        ["ffplay", "-nodisp", "-autoexit", "-loglevel", "error",
         str(PROJECT / "tests" / "meeting.wav")],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, creationflags=platform.system().hidden_process_flags())

    levels = []
    t0 = time.time()
    while time.time() - t0 < 26:
        time.sleep(1.0)
        levels.append(round(rec.level, 3))
        if play.poll() is not None and time.time() - t0 > 20:
            break
    st = rec.status()
    try:
        play.terminate()
    except Exception:
        pass
    rec.stop()

    pcm = np.concatenate(got) if got else np.zeros(0, dtype=np.float32)
    say("")
    say("получено: %.2f c (кусков %d)" % (len(pcm) / 16000.0, len(got)))
    say("уровни по секундам: %s" % levels)
    say("статус: silent=%s stalled=%s alive=%s error=%s"
        % (st["silent"], st["stalled"], st["alive"], st["error"]))
    if pcm.size:
        say("пик=%.5f" % float(np.max(np.abs(pcm))))

    if pcm.size < 16000:
        say("")
        say("ПЕТЛЯ НЕ ОТДАЁТ ЗВУК: поток открывается, но данных нет")
        return 1

    audio_io.write_wav(PROJECT / "tests" / "loopback_real.wav", pcm)
    say("сохранено в tests/loopback_real.wav")
    res = asr.transcribe_live(pcm[: int(24 * 16000)])
    say("")
    say("РАСПОЗНАНО ИЗ ПЕТЛИ: %s" % res.text[:220])
    ok = ("закупкам" in res.text or "сроки" in res.text or "подрядчик" in res.text.lower())
    say("")
    say("ПЕТЛЯ ВЫВОДА РАБОТАЕТ: %s" % ("ДА" if ok else "звук есть, но текст не совпал"))
    return 0 if ok else 1


if __name__ == "__main__":
    code = 1
    try:
        code = main()
    except Exception:
        import traceback

        say("СБОЙ:")
        say(traceback.format_exc())
    finally:
        io.open(PROJECT / "tests" / "t23_result.txt", "w", encoding="utf-8").write("\n".join(LINES))
    sys.exit(code)
