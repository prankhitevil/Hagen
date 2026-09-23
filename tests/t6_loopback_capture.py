# -*- coding: utf-8 -*-
"""Проверка 6: петля вывода реально ловит звук и он распознаётся.

Проигрываем тестовый файл на устройстве вывода и одновременно пишем петлю.
Если распознанный из петли текст совпал с исходным — канал собеседников работает.
"""
import io
import json
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
from harness import LINES, FAIL, say  # noqa: E402

import numpy as np  # noqa: E402

from hagen import asr, audio_io, platform  # noqa: E402
from hagen.platform.windows import loopback  # noqa: E402


def dump():
    io.open(PROJECT / "tests" / "t6_result.txt", "w", encoding="utf-8").write("\n".join(LINES))


def main():
    say("=== устройства петли ===")
    devs = loopback.list_devices()
    for d in devs:
        marks = []
        if d["is_default_output"]:
            marks.append("вывод по умолчанию")
        if d["is_communications"]:
            marks.append("УСТРОЙСТВО СВЯЗИ")
        if d["recommended"]:
            marks.append("РЕКОМЕНДУЮ")
        say("   [%3d] %-50s %5d Гц %dкан.  %s"
            % (d["index"], d["name"][:50], d["sample_rate"], d["channels"], ", ".join(marks)))
    say("роли Windows: %s" % loopback.default_render_devices())

    idx = loopback.recommended_device_index()
    if idx is None:
        say("ОШИБКА: нет устройств для петли")
        return 1
    say("пишем с устройства: %s" % idx)

    captured = []
    levels = []

    def on_audio(pcm):
        captured.append(np.asarray(pcm, dtype=np.float32).copy())

    rec = loopback.LoopbackRecorder(device_index=idx, on_audio=on_audio)
    rec.start()
    say("петля открыта: %s" % rec.status())

    wav = str(PROJECT / "tests" / "example.wav")
    say("проигрываю тестовый файл на устройстве вывода...")
    play = subprocess.Popen(
        ["ffplay", "-nodisp", "-autoexit", "-loglevel", "error", wav],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=platform.system().hidden_process_flags(),
    )

    t0 = time.time()
    while time.time() - t0 < 14.0:
        time.sleep(0.5)
        levels.append(round(rec.level, 3))
        if play.poll() is not None and time.time() - t0 > 12.0:
            break
    try:
        play.wait(timeout=3)
    except Exception:
        play.kill()
    time.sleep(0.4)
    status = rec.status()
    rec.stop()

    pcm = np.concatenate(captured) if captured else np.zeros(0, dtype=np.float32)
    say("")
    say("записано: %.2f c (%d кусков)" % (len(pcm) / 16000.0, len(captured)))
    say("уровни по ходу записи: %s" % levels)
    say("пик за запись: %.4f | статус на момент конца: silent=%s stalled=%s alive=%s"
        % (float(np.max(np.abs(pcm))) if pcm.size else 0.0,
           status["silent"], status["stalled"], status["alive"]))

    if pcm.size == 0:
        say("ПРОВАЛ: из петли не пришло ни одного отсчёта")
        return 1

    audio_io.write_wav(PROJECT / "tests" / "loopback_capture.wav", pcm)
    say("сохранено в tests/loopback_capture.wav")

    say("")
    say("=== распознаём то, что поймала петля ===")
    t0 = time.time()
    res = asr.transcribe_live(pcm[: int(24 * 16000)])
    say("распознавание: %.2f c" % (time.time() - t0))
    say("ТЕКСТ ИЗ ПЕТЛИ: %s" % res.text)

    etalon = "Ничьих, не требуя похвал"
    ok = "похвал" in res.text or "лукоморья" in res.text
    say("")
    say("ПЕТЛЯ РАБОТАЕТ: %s" % ("ДА" if ok else "НЕТ"))

    io.open(PROJECT / "tests" / "t6_result.json", "w", encoding="utf-8").write(
        json.dumps({
            "devices": devs,
            "roles": loopback.default_render_devices(),
            "captured_s": round(len(pcm) / 16000.0, 2),
            "levels": levels,
            "text": res.text,
            "ok": bool(ok),
        }, ensure_ascii=False, indent=2)
    )
    say("ГОТОВО")
    return 0 if ok else 1


if __name__ == "__main__":
    code = 1
    try:
        code = main()
    except Exception:
        say("СБОЙ:")
        say(traceback.format_exc())
    finally:
        dump()
    sys.exit(code)
