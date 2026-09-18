# -*- coding: utf-8 -*-
"""Проверка 75: плавающий кружок микрофона (решение 17.09).

Во время записи на экране висит полупрозрачный кружок поверх всех окон:
красный — микрофон включён, серый — выключен, щелчок переключает. Отдельное
окно Windows, потому что во время совещания «Hagen» свёрнут в трей, и
плашку внутри него никто не увидит (тот же довод, что у капсулы диктовки).

Микрофон машины проверка НЕ трогает: щелчок отдаётся пустышке.

Что проверяем:
  1. окно создаётся, показывается, красится и прячется;
  2. щелчок зовёт переключатель и перекрашивает кружок;
  3. поломка переключателя не роняет окно;
  4. кружок круглый, поверх всех, не забирает фокус, не в Alt+Tab;
  5. появляется на записи и уходит после неё, слушается настройка;
  6. настройка есть на странице и сохраняется.

Настройки — временные (isolate).
Запуск из корня проекта:  .venv\\Scripts\\python.exe tests\\t75_mic_pill.py
"""
import io
import sys
import time
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
sys.path.insert(0, str(PROJECT / "tests"))
import isolate  # noqa: E402

isolate.voices()
isolate.settings()

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


def check(name, ok, detail=""):
    if not ok:
        FAIL.append(name)
    say(("   ok    " if ok else "   ПЛОХО ") + name + (": " + str(detail)[:300] if detail != "" else ""))


import win32con  # noqa: E402
import win32gui  # noqa: E402

from hagen import config  # noqa: E402
from hagen.platform.windows import miccapsule  # noqa: E402

CLICKS = {"n": 0, "muted": False, "told": []}


def fake_toggle():
    CLICKS["n"] += 1
    CLICKS["muted"] = not CLICKS["muted"]
    return {"available": True, "muted": CLICKS["muted"], "device": "подставной"}


def broken_toggle():
    raise RuntimeError("устройство пропало")


pill = None
try:
    say("=== 1. Окно ===")
    pill = miccapsule.MicPill(on_click=fake_toggle,
                              on_change=lambda st: CLICKS["told"].append(st))
    check("окно создалось", bool(pill.hwnd), pill.error)
    check("сначала не видно", not pill.visible())
    pill.show(muted=False)
    time.sleep(0.4)
    check("после show видно", pill.visible())
    check("окно и правда на экране", bool(win32gui.IsWindowVisible(pill.hwnd)))

    say("")
    say("=== 2. Щелчок ===")
    win32gui.PostMessage(pill.hwnd, win32con.WM_LBUTTONUP, 0, 0)
    time.sleep(0.5)
    check("переключатель позван", CLICKS["n"] == 1, CLICKS)
    check("кружок запомнил новое состояние", pill._muted is True, pill._muted)
    check("окну программы рассказали", len(CLICKS["told"]) == 1 and CLICKS["told"][0]["muted"] is True,
          CLICKS["told"])
    win32gui.PostMessage(pill.hwnd, win32con.WM_LBUTTONUP, 0, 0)
    time.sleep(0.5)
    check("второй щелчок возвращает обратно", pill._muted is False, (CLICKS, pill._muted))

    say("")
    say("=== 3. Перекраска и пряталки ===")
    pill.set_muted(True)
    time.sleep(0.3)
    check("перекрасился без показа/прятанья", pill._muted is True and pill.visible())
    pill.hide()
    time.sleep(0.4)
    check("спрятался", not pill.visible() and not win32gui.IsWindowVisible(pill.hwnd))

    say("")
    say("=== 4. Поломка переключателя ===")
    pill.on_click = broken_toggle
    pill.show(muted=False)
    time.sleep(0.3)
    win32gui.PostMessage(pill.hwnd, win32con.WM_LBUTTONUP, 0, 0)
    time.sleep(0.5)
    check("окно живо после поломки", bool(pill.hwnd) and win32gui.IsWindowVisible(pill.hwnd))
    pill.on_click = fake_toggle

    say("")
    say("=== 5. Флаги окна ===")
    ex = win32gui.GetWindowLong(pill.hwnd, win32con.GWL_EXSTYLE)
    check("поверх всех окон", bool(ex & win32con.WS_EX_TOPMOST))
    check("не забирает фокус", bool(ex & win32con.WS_EX_NOACTIVATE))
    check("нет в Alt+Tab и панели задач", bool(ex & win32con.WS_EX_TOOLWINDOW))
    check("полупрозрачный", bool(ex & win32con.WS_EX_LAYERED))
    check("мышь ловит (не сквозной)", not (ex & win32con.WS_EX_TRANSPARENT))
    import ctypes  # noqa: E402

    aff = ctypes.c_uint(0)
    got = ctypes.windll.user32.GetWindowDisplayAffinity(
        ctypes.c_void_p(pill.hwnd), ctypes.byref(aff))
    check("собеседники в трансляции его не увидят",
          bool(got) and aff.value == miccapsule.WDA_EXCLUDEFROMCAPTURE,
          (got, aff.value, pill.hidden_from_capture))
    left, top, right, bottom = win32gui.GetWindowRect(pill.hwnd)
    check("кружок квадратного размера", (right - left) == miccapsule.SIZE
          and (bottom - top) == miccapsule.SIZE, (right - left, bottom - top))

    say("")
    say("=== 5а. Значок и движение волны ===")
    src = io.open(PROJECT / "hagen" / "platform" / "windows" / "miccapsule.py", encoding="utf-8").read()
    check("значок рисуется по чертежу дизайнера, а не самодельный микрофон",
          "_draw_glyph" in src and "_draw_mic" not in src)
    check("пропорции взяты из файла значка (поле 64×64)", "/ 64.0" in src)
    check("столбики волны заданы чертежом", "_BARS" in src)
    # Плавность считается косинусом на 24 шага (замечено 17.09:
    # четыре кадра давали рывки). Проверяем не текст, а само поведение.
    side0, mid0 = miccapsule._wave_scale(0)
    side_mid, mid_mid = miccapsule._wave_scale(miccapsule.WAVE_STEPS // 2)
    check("крайние и средний в противофазе", side0 < mid0 and side_mid > mid_mid,
          (side0, mid0, side_mid, mid_mid))
    # Плавность мерим приращением за шаг: чем оно мельче, тем слитнее ход.
    # 17.09 решено сделать плавнее — шагов стало 36 вместо 24.
    step = max(abs(miccapsule._wave_scale(i)[0] - miccapsule._wave_scale(i + 1)[0])
               for i in range(miccapsule.WAVE_STEPS))
    check("ход плавный: приращение за шаг мелкое", step < 0.045, round(step, 4))
    check("шагов в цикле хватает", miccapsule.WAVE_STEPS >= 36, miccapsule.WAVE_STEPS)
    check("средний столбик укорочен на 15 %",
          abs(miccapsule._BARS[1][1] - 20.4) < 0.01, miccapsule._BARS[1][1])
    check("цикл около секунды с небольшим",
          1.0 <= miccapsule.WAVE_STEPS * miccapsule.BREATH_MS / 1000.0 <= 1.6,
          miccapsule.WAVE_STEPS * miccapsule.BREATH_MS / 1000.0)
    check("фон не стирается — иначе значок моргает",
          "_on_erase" in src and "InvalidateRect(hwnd, None, True)" not in src)
    check("рисуем на скрытом холсте", "CreateCompatibleBitmap" in src and "BitBlt" in src)
    check("волна движется только при записи",
          "frame = phase if (recording and not muted) else 0" in src)
    check("размер — полтора прежних", miccapsule.SIZE == 84, miccapsule.SIZE)
    check("вокруг значка ничего нет: цвет-невидимка",
          "LWA_COLORKEY" in src and "CreateRoundRectRgn" not in src)

    say("")
    say("=== 6. Связь с записью ===")
    srv = io.open(PROJECT / "hagen" / "server.py", encoding="utf-8").read()
    check("показывается при старте записи", srv.count("sync_mic_pill(True)") == 2,
          srv.count("sync_mic_pill(True)"))
    check("убирается на всех путях остановки", srv.count("sync_mic_pill(False)") == 3,
          srv.count("sync_mic_pill(False)"))
    # Обвязка кружка и маршруты микрофона переехали в роутеры: смотрим туда,
    # где код живёт теперь.
    deps = io.open(PROJECT / "hagen" / "api" / "deps.py", encoding="utf-8").read()
    tools = io.open(PROJECT / "hagen" / "api" / "tools.py", encoding="utf-8").read()
    check("слушается настройка", 'config.get("mic_pill", True)' in deps)
    check("снятая галочка убирает кружок сразу", 'if "mic_pill" in patch:' in srv)
    check("кнопка в окне не расходится с кружком",
          "pill.set_muted(bool(out.get(\"muted\")))" in tools)
    check("настройка заводская — включено", config.DEFAULTS.get("mic_pill") is True)

    say("")
    say("=== 7. Страница ===")
    html = io.open(PROJECT / "hagen" / "static" / "index.html", encoding="utf-8").read()
    js = io.open(PROJECT / "hagen" / "static" / "app.js", encoding="utf-8").read()
    check("галочка есть", 'id="set-mic-pill"' in html)
    check("в подсказке сказано про последствие", "ни собеседники, ни запись" in html)
    check("галочка читается", "$('set-mic-pill').checked = s.mic_pill !== false;" in js)
    check("галочка сохраняется", "mic_pill: $('set-mic-pill').checked," in js)

except Exception as err:                       # noqa: BLE001
    FAIL.append("проверка оборвалась")
    say("ОБОРВАЛОСЬ: %s: %s" % (type(err).__name__, err))
finally:
    try:
        if pill is not None:
            pill.hide()
            pill.stop()
    except Exception:
        pass

say("")
say("Всего замечаний: %d" % len(FAIL))
for name in FAIL:
    say("   — " + name)
io.open(PROJECT / "tests" / "t75_result.txt", "w", encoding="utf-8").write("\n".join(LINES))
sys.exit(1 if FAIL else 0)
