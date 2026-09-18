# -*- coding: utf-8 -*-
"""Проверка 15: звук, который играет компьютер, доходит до дорожки собеседников.

Весь путь через службу, как у настоящей записи: «Старт» → петля вывода
открывается в процессе-помощнике → звук колонок пишется в дорожку «far» →
«Стоп» → файл дорожки на диске. Отдельно петлю проверяют t6 и t23, отдельно
микрофон через службу — t38; этот путь целиком не проверял никто.

Как отличаем «наш» звук от шума: сначала пара секунд тишины, потом проверка
сама играет tests/meeting.wav в колонки по умолчанию и через десять секунд
останавливает. В дорожке звук должен начаться после тишины, закончиться перед
«Стопом», занимать заметную часть времени (это речь, а не щелчок) и — главное —
повторять огибающую исходника.

Звук НАСТОЯЩИЙ и слышен в комнате — около десяти секунд. Если колонки выключены
или громкость на нуле, проверка честно покраснеет и скажет об этом.
Служба своя, внутри процесса: запущенную рядом программу проверка не трогает.
Настройки и база голосов — временные.

Запуск из корня проекта:  .venv\\Scripts\\python.exe tests\\t15_display_audio.py
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

import numpy as np  # noqa: E402

from hagen import audio_io, platform, store  # noqa: E402

PLAY = PROJECT / "tests" / "meeting.wav"
BASELINE_S = 2.5          # тишина перед проигрыванием — это фон
PLAY_S = 10.0             # сколько играем
THRESHOLD = 0.02          # пик окна 100 мс, выше которого считаем, что звук есть

# Колонки по умолчанию: туда играет winsound, оттуда и снимаем петлю. Режим —
# «звонок» (online): в режиме очной встречи собеседники с колонок не пишутся.
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


def windows(pcm, sr, t_from, t_to, win_s=0.1):
    """Пики окон по 100 мс в промежутке [t_from, t_to) секунд от начала дорожки."""
    a, b = max(0, int(t_from * sr)), max(0, int(t_to * sr))
    part = pcm[a:b]
    n = int(win_s * sr)
    if n <= 0 or part.size < n:
        return np.zeros(0, dtype=np.float32)
    part = part[: (part.size // n) * n].reshape(-1, n)
    return np.abs(part).max(axis=1)


say("=== 0. Колонки ===")
check("колонки по умолчанию найдены", speakers is not None,
      [d.get("name") for d in platform.audio().list_loopback_devices()])
say("   играем и снимаем петлю: %s" % (speakers or {}).get("name"))

rec_id = None
try:
    with TestClient(server.app, base_url="http://127.0.0.1:8787") as cli:
        say("")
        say("=== 1. «Старт» через службу ===")
        r = cli.post("/api/recordings", headers=ORIGIN,
                     json={"title": "Проверка 15 — звук компьютера", "mode": "online"})
        check("запись началась", r.status_code == 200, r.text[:200])
        rec_id = (r.json().get("meta") or {}).get("id") if r.status_code == 200 else None
        t_rec = time.monotonic()

        # Устройства открываются в фоне; помощнику петли нужна секунда-другая.
        time.sleep(3.0 + BASELINE_S)
        say("   играю %s в колонки, %.0f с" % (PLAY.name, PLAY_S))
        winsound.PlaySound(str(PLAY), winsound.SND_FILENAME | winsound.SND_ASYNC)
        time.sleep(PLAY_S)
        winsound.PlaySound(None, 0)
        t_quiet = time.monotonic()
        time.sleep(1.0)

        say("")
        say("=== 2. «Стоп» ===")
        r = cli.post("/api/recordings/%s/stop" % rec_id, headers=ORIGIN, json={})
        check("запись остановлена", r.status_code == 200, r.text[:200])

        say("")
        say("=== 3. Дорожка собеседников ===")
        far = store.track_path(rec_id, "far") if rec_id else None
        check("дорожка собеседников записана", bool(far) and far.exists(), str(far))
        pcm, sr = audio_io.read_wav(far) if far and far.exists() else (np.zeros(0), 16000)
        dur = pcm.size / float(sr or 16000)
        check("длина похожа на время записи", abs(dur - (t_quiet - t_rec + 1.0)) < 4.0,
              "%.1f с в дорожке, %.1f с записи" % (dur, t_quiet - t_rec + 1.0))

        # Где в дорожке начался звук, ищем по самой дорожке, а не по часам:
        # дорожка начинается, когда открылось устройство, а не когда пришёл
        # «Старт», и эта разница плавает на секунды.
        peaks = windows(pcm, sr, 0, dur)
        loud_at = np.nonzero(peaks > THRESHOLD)[0]
        onset = int(loud_at[0]) if loud_at.size else None
        check("во время проигрывания в дорожке звук", onset is not None,
              "пик %.4f — колонки выключены или громкость на нуле?"
              % (float(peaks.max()) if peaks.size else 0.0))
        if onset is not None:
            before = peaks[:onset]
            played = peaks[onset:onset + int(PLAY_S * 10)]
            tail = peaks[-4:]
            share = float((played > THRESHOLD).mean())
            say("   звук начался на %.1f с дорожки; до него %d окон тишины, медиана %.4f"
                % (onset / 10.0, before.size, float(np.median(before)) if before.size else 0.0))
            check("до проигрывания в дорожке тихо — звук начался с нашим «плей»",
                  before.size >= 10 and float(np.median(before)) < THRESHOLD / 4,
                  "%d окон, медиана %.4f" % (before.size,
                                             float(np.median(before)) if before.size else -1))
            check("это речь, а не щелчок: звук в заметной части окон", share > 0.25,
                  "%.0f %%" % (share * 100))
            check("после остановки проигрывания снова тихо",
                  tail.size and float(tail.max()) < THRESHOLD, [round(float(x), 4) for x in tail])

            # Сверка с исходником: огибающая дорожки должна повторять огибающую
            # meeting.wav. Петля берёт ровно то, что ушло в колонки, поэтому
            # совпадение высокое; сдвиг ищем в пределах полусекунды.
            src, ssr = audio_io.read_wav(PLAY)
            ref = windows(src, ssr, 0, PLAY_S)
            best = -1.0
            for lag in range(-5, 6):
                a = peaks[max(0, onset + lag):max(0, onset + lag) + ref.size]
                if a.size < ref.size * 0.8:
                    continue
                n = min(a.size, ref.size)
                if np.std(a[:n]) > 0 and np.std(ref[:n]) > 0:
                    best = max(best, float(np.corrcoef(a[:n], ref[:n])[0, 1]))
            say("   сходство огибающей с %s: %.2f" % (PLAY.name, best))
            check("в дорожке именно наш звук: огибающая повторяет исходник", best > 0.6,
                  "%.2f" % best)

        say("")
        say("=== 4. Уборка ===")
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
io.open(PROJECT / "tests" / "t15_result.txt", "w", encoding="utf-8").write("\n".join(LINES))
sys.exit(1 if FAIL else 0)
