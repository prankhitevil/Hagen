# -*- coding: utf-8 -*-
"""Проверка 46: настоящее окно программы вместе со значком у часов.

То же, что делает run.py --app, только без службы: окно pywebview, значок,
крестик и «Выход». Окно появляется на экране на несколько секунд и закрывается
само.

  1. Окно, запущенное «в трей» (как при старте с Windows), не видно.
  2. Значок открывает его — окно видно.
  3. Крестик (то же сообщение WM_CLOSE, что шлёт Windows) окно НЕ закрывает,
     а прячет; программа продолжает работать.
  4. «Выход» из меню значка закрывает окно по-настоящему.

Запуск из корня проекта:  .venv\\Scripts\\python.exe tests\\t46_window_tray.py
"""
import io
import sys
import threading
import time
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
import isolate  # noqa: E402  настоящая база голосов не трогается

isolate.voices()

LINES = []
FAIL = []
TITLE = "Hagen — проверка 46"


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
    say(("   ok    " if ok else "   ПЛОХО ") + name + (": " + str(detail) if detail != "" else ""))


import webview  # noqa: E402
import win32con  # noqa: E402
import win32gui  # noqa: E402

from hagen.platform.windows import tray  # noqa: E402


class FakeServer:
    _calls = None

    def _call_active_recording(self):
        return None


def find_hwnd():
    found = []

    def cb(h, _):
        if win32gui.GetWindowText(h) == TITLE:
            found.append(h)
        return True
    win32gui.EnumWindows(cb, None)
    return found[0] if found else None


def wait_for(pred, limit=10.0):
    t0 = time.time()
    while time.time() - t0 < limit:
        if pred():
            return True
        time.sleep(0.1)
    return False


shell = tray.AppShell(None, FakeServer(), notifier=None)
check("значок поднялся", shell.start())
window = webview.create_window(TITLE, html="<h1>Проверка 46</h1>", width=500, height=300,
                               hidden=True)
shell.window = window
closed = threading.Event()


def on_closing():
    return shell.on_closing()


window.events.closing += on_closing
window.events.closed += lambda: closed.set()


def scenario():
    time.sleep(1.5)
    hwnd = find_hwnd()
    check("окно создано", hwnd is not None)
    check("запуск «в трей»: окна не видно", hwnd is not None and not win32gui.IsWindowVisible(hwnd))

    shell.open_window()
    check("значок открыл окно — видно", wait_for(lambda: win32gui.IsWindowVisible(find_hwnd() or 0)))

    win32gui.PostMessage(find_hwnd(), win32con.WM_CLOSE, 0, 0)
    hidden = wait_for(lambda: find_hwnd() and not win32gui.IsWindowVisible(find_hwnd()), 5)
    check("крестик прячет окно, а не закрывает", hidden and not closed.is_set())
    time.sleep(0.5)
    check("программа работает дальше", not closed.is_set() and shell.tray.running)

    shell.open_window()
    check("из трея окно снова открывается", wait_for(lambda: win32gui.IsWindowVisible(find_hwnd() or 0)))

    shell.quit()
    check("«Выход» закрывает окно по-настоящему", closed.wait(10))


webview.start(scenario, gui="edgechromium", private_mode=True)
shell.stop()
check("значок убран после выхода", not shell.tray.running)

say("")
say("ИТОГО провалов: %d" % len(FAIL))
for f in FAIL:
    say("   - " + f)
io.open(PROJECT / "tests" / "t46_result.txt", "w", encoding="utf-8").write("\n".join(LINES))
sys.exit(1 if FAIL else 0)
