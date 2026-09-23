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
from harness import LINES, FAIL, say, check, finish  # noqa: E402

isolate.voices()

TITLE = "Hagen — проверка 46"


import webview  # noqa: E402
import win32con  # noqa: E402
import win32gui  # noqa: E402

from hagen.platform.base import ShellHooks  # noqa: E402
from hagen.platform.windows import tray  # noqa: E402


HOOKS = ShellHooks(active_recording=lambda: None, start_recording=lambda: "",
                   stop_recording=lambda rec_id: None,
                   add_prompt_listener=lambda fn: None, answer_prompt=lambda pid, btn: None)


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


shell = tray.AppShell(None, HOOKS, notifier=None)
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

sys.exit(finish("t46"))
