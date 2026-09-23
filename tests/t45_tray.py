# -*- coding: utf-8 -*-
"""Проверка 45: значок у часов, уведомления с кнопками, автозапуск.

Решения 13.09: крестик прячет окно в трей, вопросы о звонках
приходят уведомлением Windows с кнопками, программа стартует вместе с Windows.

Что проверяем — на настоящем Windows, без окна программы:
  1. Значки нарисованы и читаются как .ico.
  2. Программа зарегистрирована для уведомлений (ключ в HKCU).
  3. Уведомление с кнопками показывается и убирается; нажатие кнопки
     доходит до ответа автоматики звонков, нажатие на тело — открывает окно.
  4. Значок у часов: появляется, красный во время записи, клик открывает окно,
     пункты меню доходят до действий, при остановке значок убирается.
  5. Крестик прячет окно, «Выход» закрывает; во время записи — спрашивает.
  6. Ярлык автозапуска кладётся и убирается (во временной папке).
  7. Второй запуск ярлыка показывает окно работающей программы.

Запуск из корня проекта:  .venv\\Scripts\\python.exe tests\\t45_tray.py
"""
import io
import sys
import tempfile
import time
import winreg
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
import isolate  # noqa: E402  настоящая база голосов не трогается
from harness import LINES, FAIL, say, check, finish  # noqa: E402

isolate.voices()


import win32con  # noqa: E402
import win32gui  # noqa: E402
from PIL import Image  # noqa: E402

from hagen.platform.base import ShellHooks  # noqa: E402
from hagen.platform.windows import tray  # noqa: E402

say("=== 1. Значки ===")
paths = tray.icon_paths()
# Четыре состояния нарисованы дизайнером и лежат в пакете (решение 17.09): раньше значки рисовались кодом, теперь берутся файлами.
check("все четыре состояния на месте", sorted(paths) == ["idle", "muted", "pause", "rec"],
      sorted(paths))
for key, p in paths.items():
    ok = p.exists()
    try:
        Image.open(p).load()
    except Exception as err:
        ok = False
        say("   %s: %s" % (key, err))
    check("значок %s есть и читается" % key, ok, p)

say("")
say("=== 1а. Какой значок когда ===")
# Порядок важности: запись важнее паузы, пауза важнее
# выключенного микрофона. Пауза при этом видна ВО ВРЕМЯ записи — иначе её
# вообще не увидеть: вне записи паузы не бывает.
cases = [
    ({"recording": True}, "rec"),
    ({"recording": True, "paused": True}, "pause"),
    ({"paused": True}, "pause"),
    ({"mic_muted": True}, "muted"),
    ({"recording": True, "mic_muted": True}, "rec"),
    ({}, "idle"),
]
for state, want in cases:
    got = tray.icon_for(state)
    check("%s -> %s" % (state or "покой", want), got == want, got)

say("")
say("=== 2. Регистрация для уведомлений ===")
tray.register_aumid()
with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "SOFTWARE\\Classes\\AppUserModelId\\" + tray.AUMID) as k:
    name = winreg.QueryValueEx(k, "DisplayName")[0]
check("имя программы в реестре", name == "Hagen", name)

say("")
say("=== 3. Уведомление с кнопками ===")
answers, opened = [], []
notifier = tray.Notifier(on_answer=lambda p, a: answers.append((p, a)),
                         on_open=lambda: opened.append(1))
prompt = {"id": "t45abc", "kind": "call_ended", "text": "Проверка 45: уведомление с кнопками",
          "buttons": [{"id": "stop", "label": "Остановить"}, {"id": "keep", "label": "Писать дальше"}]}
err = None
try:
    notifier.show_prompt(prompt)
except Exception as e:
    err = e
check("уведомление показано без ошибок", err is None and notifier._current is not None, err)
toast = notifier._current[1] if notifier._current else None
check("у уведомления две кнопки", toast is not None and len(toast.actions) == 2,
      toast and [a.content for a in toast.actions])
check("кнопка несёт ответ", toast is not None and toast.actions[0].arguments == "prompt=t45abc&answer=stop",
      toast and toast.actions[0].arguments)


class Ev:
    def __init__(self, arguments):
        self.arguments = arguments


notifier._activated(Ev("prompt=t45abc&answer=stop"))
notifier._activated(Ev(""))
time.sleep(0.5)
check("нажатие кнопки дошло до ответа", answers == [("t45abc", "stop")], answers)
check("нажатие на само уведомление открывает окно", opened == [1], opened)
notifier.show_prompt(None)
check("снятый вопрос убирает уведомление", notifier._current is None)

# Главная ловушка: WinRT, загруженный раньше torch, ломает torch целиком
# (WinError 1114, c10.dll) — а с ним распознавание и разметку.
try:
    import torch  # noqa: F401

    from hagen import diar_pyannote
    # pyannote есть только в релизе с разметкой через него; в релизе ONNX
    # после уведомлений должен грузиться onnxruntime.
    if diar_pyannote.installed():
        from pyannote.audio import Pipeline  # noqa: F401
    import onnxruntime  # noqa: F401
    torch_ok = float((torch.ones(2) * 3).sum()) == 6.0
    torch_err = ""
except Exception as e:
    torch_ok, torch_err = False, str(e)[:160]
check("после уведомлений torch, onnxruntime и pyannote (если он есть) загружаются",
      torch_ok, torch_err)

say("")
say("=== 4. Значок у часов ===")
calls = {"open": 0, "record": 0, "quit": 0}
recording = {"on": False}
t = tray.Tray(on_open=lambda: calls.__setitem__("open", calls["open"] + 1),
              on_record=lambda: calls.__setitem__("record", calls["record"] + 1),
              on_quit=lambda: calls.__setitem__("quit", calls["quit"] + 1),
              is_recording=lambda: recording["on"])
check("значок появился", t.start() and t.running, t.error)
recording["on"] = True
time.sleep(1.6)
check("во время записи значок красный", t._state["recording"] is True, t._state)
recording["on"] = False
time.sleep(1.6)
check("после записи обычный", t._state["recording"] is False, t._state)
win32gui.SendMessage(t.hwnd, t.WM_TRAY, 0, win32con.WM_LBUTTONUP)
win32gui.SendMessage(t.hwnd, win32con.WM_COMMAND, t.ID_RECORD, 0)
win32gui.SendMessage(t.hwnd, win32con.WM_COMMAND, t.ID_QUIT, 0)
time.sleep(0.5)
check("клик по значку открывает окно", calls["open"] == 1, calls)
check("пункт «Начать запись» доходит", calls["record"] == 1, calls)
check("пункт «Выход» доходит", calls["quit"] == 1, calls)
t.stop()
check("при остановке значок убран, поток завершён", not t.running and t.hwnd is None)

say("")
say("=== 5. Крестик и «Выход» ===")


class FakeWindow:
    def __init__(self):
        self.log = []

    def show(self):
        self.log.append("show")

    def restore(self):
        self.log.append("restore")

    def hide(self):
        self.log.append("hide")

    def destroy(self):
        self.log.append("destroy")


class FakeServer:
    """Ядро глазами оболочки: только то, что перечислено в ShellHooks."""

    def __init__(self):
        self.active = None
        self.stopped = []

    def hooks(self):
        return ShellHooks(active_recording=lambda: self.active,
                          start_recording=lambda: "new",
                          stop_recording=self._stop,
                          add_prompt_listener=lambda fn: None,
                          answer_prompt=lambda pid, btn: None)

    def _stop(self, rec_id):
        self.stopped.append(rec_id)
        self.active = None


asked = []
win = FakeWindow()
srv = FakeServer()
shell = tray.AppShell(win, srv.hooks(), notifier=None, ask=lambda t_, x: asked.append(x) or asked_answer[0])
asked_answer = [False]
shell.start()
time.sleep(0.3)
check("крестик НЕ закрывает, когда значок работает", shell.on_closing() is False)
time.sleep(0.3)
check("окно спрятано", "hide" in win.log, win.log)
shell.open_window()
check("из трея окно показывается", win.log[-2:] == ["show", "restore"], win.log)
srv.active = "rec1"
shell.quit()
check("«Выход» во время записи спрашивает", len(asked) == 1, asked)
check("ответ «Нет» — ничего не закрыто и запись идёт",
      "destroy" not in win.log and srv.active == "rec1" and not shell.quitting)
asked_answer[0] = True
shell.quit()
check("ответ «Да» — запись остановлена и окно закрыто",
      srv.stopped == ["rec1"] and win.log[-1] == "destroy" and shell.quitting, (srv.stopped, win.log))
check("после «Выхода» крестик закрывает по-настоящему", shell.on_closing() is True)
shell.stop()
shell2 = tray.AppShell(FakeWindow(), FakeServer().hooks())
check("без значка крестик закрывает, как раньше", shell2.on_closing() is True)

say("")
say("=== 6. Автозапуск ===")
tmp = Path(tempfile.mkdtemp(prefix="t45_"))
res = tray.set_autostart(True, folder=tmp)
link = tmp / tray.SHORTCUT_NAME
check("ярлык положен", res.get("ok") and link.exists(), res)
if link.exists():
    import win32com.client

    sc = win32com.client.Dispatch("WScript.Shell").CreateShortcut(str(link))
    check("ярлык запускает программу в трей",
          sc.TargetPath.lower().endswith("python.exe") and sc.Arguments == "run.py --app --tray",
          (sc.TargetPath, sc.Arguments))
    check("рабочая папка — папка программы", Path(sc.WorkingDirectory) == PROJECT, sc.WorkingDirectory)
check("признак «включён»", tray.autostart_enabled(folder=tmp) is True)
res = tray.set_autostart(False, folder=tmp)
check("ярлык убран", res.get("ok") and not link.exists(), res)
try:
    tmp.rmdir()
except OSError:
    pass

say("")
say("=== 7. Второй запуск показывает окно из трея ===")
from fastapi.testclient import TestClient  # noqa: E402

from hagen import server  # noqa: E402

shown = []
with TestClient(server.app, base_url="http://127.0.0.1:8787") as cli:
    ORIGIN = {"Origin": "http://127.0.0.1:8787"}
    r = cli.post("/api/app/show", headers=ORIGIN, json={})
    check("без окна служба честно отвечает «не показано»", r.json().get("shown") is False, r.text)
    server.set_app_hooks(show=lambda: shown.append(1))
    r = cli.post("/api/app/show", headers=ORIGIN, json={})
    check("с окном — показывает", r.json().get("shown") is True and shown == [1], (r.text, shown))
    server._app_hooks.clear()

sys.exit(finish("t45"))
