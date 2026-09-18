# -*- coding: utf-8 -*-
"""Значок у часов, уведомления Windows с кнопками и автозапуск.

Решения 13.09.2026:
* крестик окна прячет программу в трей, совсем закрыть — «Выход» в меню
  значка; во время записи значок красный;
* вопросы автоматики звонков («Не писать», «Остановить?», «Дописать в
  прошлую?») приходят уведомлением Windows с кнопками — отвечать можно, не
  открывая окно;
* «Hagen» запускается вместе с Windows сразу свёрнутым в трей.

Значок сделан на чистом Win32 через уже стоящий pywin32: свой поток со своим
циклом сообщений, потому что главный поток занят окном (pywebview). Для кнопок
в уведомлениях нужен WinRT — пакет windows-toasts (без .exe). Ему нужен
зарегистрированный идентификатор приложения (AUMID): без него Windows показывает
уведомление, но нажатие кнопки до программы не доходит. Регистрация — ключ в
HKEY_CURRENT_USER, прав администратора не нужно.

Новых процессов модуль не порождает: Kaspersky на этой машине этого не любит.
"""
from __future__ import annotations

import logging
import queue
import threading
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs

from ... import config

log = logging.getLogger("hagen.tray")

AUMID = "PK.Hagen"
APP_NAME = "Hagen"
# Две формы написания и только две. Короткая — знак рядом с
# названием: трей, заголовок уведомления. Полная — приветствие: заголовок окна,
# «О программе», пустой экран.
NAME_SHORT = "Hagen · Consigliere"
NAME_FULL = "Hagen, Your Consigliere"
SHORTCUT_NAME = "Hagen.lnk"


# ---------------------------------------------------------------- значки


#: Значок программы: четыре состояния, нарисованы дизайнером и лежат в пакете
#: (решение 17.09, вариант «буква H с волной»). Раньше значок рисовался
#: кодом на PIL — от этого остались только запасные пути на случай, если файлов
#: почему-то нет рядом.
#: Значки лежат в hagen/icons — на три папки выше этого файла.
ICON_DIR = Path(__file__).resolve().parents[2] / "icons"

#: Состояние → файл. Ключи те же, что были («idle», «rec»), плюс два новых.
ICON_FILES = {
    "idle": "hagen-idle.ico",      # в покое: янтарная волна
    "rec": "hagen-rec.ico",        # идёт запись: красная волна, выше
    "pause": "hagen-pause.ico",    # пауза: красный знак паузы
    "muted": "hagen-muted.ico",    # микрофон выключен: ровная серая перекладина
}


def icon_paths() -> dict[str, Path]:
    """Пути к значкам всех состояний. Нет файла — состояние просто пропускаем."""
    out: dict[str, Path] = {}
    for key, name in ICON_FILES.items():
        path = ICON_DIR / name
        if path.exists():
            out[key] = path
    if not out:
        log.warning("значков нет в %s — трей останется без картинки", ICON_DIR)
    return out


def _mic_muted() -> bool:
    """Выключен ли микрофон Windows. Не смогли узнать — считаем, что включён."""
    try:
        from . import micmute

        return bool(micmute.state().get("muted"))
    except Exception:
        return False


def icon_for(state: dict[str, Any]) -> str:
    """Какой значок показывать. Порядок важности задан 17.09:
    запись важнее паузы, пауза важнее выключенного микрофона."""
    if state.get("recording") and not state.get("paused"):
        return "rec"
    if state.get("paused"):
        return "pause"
    if state.get("mic_muted"):
        return "muted"
    return "idle"


# ---------------------------------------------------------------- автозапуск


def startup_folder() -> Path:
    import win32com.client

    shell = win32com.client.Dispatch("WScript.Shell")
    return Path(shell.SpecialFolders("Startup"))


def autostart_enabled(folder: Path | None = None) -> bool:
    return ((folder or startup_folder()) / SHORTCUT_NAME).exists()


def set_autostart(enabled: bool, folder: Path | None = None) -> dict[str, Any]:
    """Ярлык в «Автозагрузке»: тот же запуск, что с рабочего стола, плюс --tray."""
    folder = folder or startup_folder()
    link = folder / SHORTCUT_NAME
    if not enabled:
        existed = link.exists()
        try:
            link.unlink(missing_ok=True)
        except OSError as err:
            return {"ok": False, "enabled": link.exists(), "error": str(err)}
        if existed:
            log.info("автозапуск выключен: ярлык убран из «Автозагрузки»")
        return {"ok": True, "enabled": False, "path": str(link)}

    from . import system

    # запуск переносимым Python из папки программы
    try:
        icon = str(icon_paths()["idle"])
    except Exception:
        icon = None
    system.write_shortcut(link, "run.py --app --tray",
                                     NAME_FULL + " (в трее)", icon)
    log.info("автозапуск включён: %s", link)
    return {"ok": True, "enabled": True, "path": str(link)}


def sync_autostart() -> None:
    """Привести ярлык автозапуска в соответствие с настройкой."""
    try:
        want = bool(config.get("autostart_windows"))
        if want != autostart_enabled():
            set_autostart(want)
    except Exception as err:
        log.warning("автозапуск не настроился: %s", err)


# ---------------------------------------------------------------- уведомления


def register_aumid() -> None:
    """Зарегистрировать программу для уведомлений с кнопками (HKCU)."""
    import winreg

    key_path = "SOFTWARE\\Classes\\AppUserModelId\\%s" % AUMID
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, key_path) as key:
        winreg.SetValueEx(key, "DisplayName", 0, winreg.REG_SZ, APP_NAME)
        winreg.SetValueEx(key, "IconUri", 0, winreg.REG_SZ, str(icon_paths()["idle"]))


def _preload_torch() -> None:
    """Загрузить torch РАНЬШЕ WinRT.

    Проверено на этой машине: стоит в процессе хотя бы импортировать
    windows_toasts (WinRT) до torch — и torch больше не загружается вовсе:
    «WinError 1114 … c10.dll». Вместе с ним отказали бы распознавание и
    разметка говорящих, то есть главная работа программы. В обратном порядке
    работает всё: torch, onnxruntime, pyannote, GigaAM и уведомления.
    """
    try:
        import torch  # noqa: F401
    except Exception as err:
        log.warning("torch не загрузился до уведомлений: %s", err)


class _ToastThread:
    """Свой поток, который владеет связью с центром уведомлений Windows.

    Зачем так. Связь с системой уведомлений привязана к потоку, который её
    открыл, и живёт, пока этот поток в ней состоит. Раньше связь открывалась в
    главном потоке до появления окна программы, а уведомления показывались из
    потока сторожа звонков. На живом звонке 14.09 оба вопроса («записываю» и
    «остановить?») остались без уведомления: Windows ответила «обращение к
    интерфейсу, относящемуся к другому потоку». Накануне те же вопросы
    показывались нормально — то есть это зависело от случая, а не от настроек.

    Теперь и связь, и все обращения к ней живут в одном потоке, который
    работает всё время работы программы. Приём тот же, что у звукового потока
    в loopback.py, и по той же причине.
    """

    def __init__(self) -> None:
        self._q: "queue.Queue[tuple]" = queue.Queue()
        self._thread = threading.Thread(target=self._run, name="toasts", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        try:
            import pythoncom

            # Общая для всех потоков модель: пока наш поток жив, связь не
            # разрушится, даже если главный поток займёт окно программы.
            pythoncom.CoInitializeEx(pythoncom.COINIT_MULTITHREADED)
        except Exception as err:
            log.debug("уведомления: COM в своём потоке не поднялся: %s", err)
        while True:
            fn, box, done = self._q.get()
            try:
                box.append(("ok", fn()))
            except BaseException as err:      # noqa: BLE001 — отдаём как есть
                box.append(("err", err))
            finally:
                done.set()

    def call(self, fn: Callable[[], Any], timeout: float = 20.0) -> Any:
        """Выполнить в потоке уведомлений и дождаться итога."""
        if threading.current_thread() is self._thread:
            return fn()
        box: list = []
        done = threading.Event()
        self._q.put((fn, box, done))
        if not done.wait(timeout):
            raise RuntimeError("поток уведомлений не ответил за %.0f с" % timeout)
        kind, value = box[0]
        if kind == "err":
            raise value
        return value


class Notifier:
    """Уведомления Windows. Вопрос автоматики — с кнопками, ответ — в on_answer."""

    GROUP = "hagen"

    def __init__(self, on_answer: Callable[[str, str], Any],
                 on_open: Callable[[], Any] | None = None) -> None:
        _preload_torch()
        register_aumid()
        self.on_answer = on_answer
        self.on_open = on_open
        self.toaster: Any = None
        self._current: tuple[str, Any] | None = None
        self._lock = threading.Lock()
        self._worker = _ToastThread()
        self._worker.call(self._connect)

    def _connect(self) -> None:
        """Открыть связь с центром уведомлений. Только из потока уведомлений."""
        from windows_toasts import InteractableWindowsToaster

        self.toaster = InteractableWindowsToaster(APP_NAME, AUMID)

    def _deliver(self, toast: Any) -> bool:
        """Показать уведомление. Один раз переоткрывает связь, если не вышло."""
        try:
            self._worker.call(lambda: self.toaster.show_toast(toast))
            return True
        except Exception as err:
            log.warning("уведомление Windows не показалось: %s — открываю связь заново", err)
        try:
            self._worker.call(self._connect)
            self._worker.call(lambda: self.toaster.show_toast(toast))
            log.info("связь с уведомлениями восстановлена, уведомление показано")
            return True
        except Exception as err:
            log.warning("уведомление Windows не показалось: %s", err)
            return False

    def show_prompt(self, prompt: dict[str, Any] | None) -> None:
        """Слушатель calls.CallAutomation: вопрос появился или снят (None)."""
        from windows_toasts import Toast, ToastButton, ToastDuration

        with self._lock:
            old = self._current
            self._current = None
        if old is not None:
            self._remove(old[1])
        if not prompt:
            return
        toast = Toast([NAME_SHORT, str(prompt.get("text") or "")])
        toast.group = self.GROUP
        # Вопрос о конце звонка ждёт 30 с — уведомление держим на экране
        toast.duration = ToastDuration.Long
        for b in prompt.get("buttons") or []:
            toast.AddAction(ToastButton(str(b.get("label") or ""),
                                        arguments="prompt=%s&answer=%s" % (prompt["id"], b["id"])))
        toast.on_activated = self._activated
        if not self._deliver(toast):
            return
        with self._lock:
            self._current = (str(prompt["id"]), toast)

    def info(self, text: str) -> None:
        """Простое уведомление без кнопок."""
        from windows_toasts import Toast

        toast = Toast([NAME_SHORT, text])
        toast.group = self.GROUP
        toast.on_activated = self._activated
        self._deliver(toast)

    def _remove(self, toast: Any) -> None:
        try:
            self._worker.call(lambda: self.toaster.remove_toast(toast))
        except Exception:
            log.debug("уведомление не убралось", exc_info=True)

    def _activated(self, event: Any) -> None:
        """Нажатие на уведомление. Приходит из потока WinRT — работу уводим."""
        args = parse_qs(str(getattr(event, "arguments", "") or ""))
        pid = (args.get("prompt") or [""])[0]
        answer = (args.get("answer") or [""])[0]
        if pid and answer:
            log.info("ответ из уведомления: %s", answer)
            threading.Thread(target=self._safe, args=(self.on_answer, pid, answer),
                             name="toast-answer", daemon=True).start()
        elif self.on_open is not None:
            threading.Thread(target=self._safe, args=(self.on_open,),
                             name="toast-open", daemon=True).start()

    @staticmethod
    def _safe(fn: Callable[..., Any], *args: Any) -> None:
        try:
            fn(*args)
        except Exception:
            log.warning("действие из уведомления не выполнилось", exc_info=True)


# ---------------------------------------------------------------- значок у часов


class Tray:
    """Значок в области уведомлений: Win32, свой поток и цикл сообщений."""

    ID_OPEN, ID_RECORD, ID_QUIT = 1001, 1002, 1003

    def __init__(self, on_open: Callable[[], Any], on_record: Callable[[], Any],
                 on_quit: Callable[[], Any], is_recording: Callable[[], bool]) -> None:
        import win32con

        self.WM_TRAY = win32con.WM_USER + 20
        self.on_open = on_open
        self.on_record = on_record
        self.on_quit = on_quit
        self.is_recording = is_recording
        self.hwnd: int | None = None
        self._icons: dict[str, Any] = {}
        self._state = {"recording": False, "paused": False,
                       "mic_muted": False, "tip": APP_NAME}
        self._ready = threading.Event()
        self._thread: threading.Thread | None = None
        self._poll_stop = threading.Event()
        self.error: str | None = None

    # -------- жизненный цикл
    def start(self, timeout: float = 5.0) -> bool:
        self._thread = threading.Thread(target=self._run, name="tray", daemon=True)
        self._thread.start()
        self._ready.wait(timeout)
        if self.hwnd:
            threading.Thread(target=self._poll, name="tray-poll", daemon=True).start()
        return bool(self.hwnd)

    def stop(self) -> None:
        import win32con
        import win32gui

        self._poll_stop.set()
        if self.hwnd:
            try:
                win32gui.PostMessage(self.hwnd, win32con.WM_CLOSE, 0, 0)
            except Exception:
                pass
        if self._thread is not None:
            self._thread.join(timeout=5)

    @property
    def running(self) -> bool:
        return bool(self.hwnd) and self._thread is not None and self._thread.is_alive()

    def _run(self) -> None:
        import win32api
        import win32con
        import win32gui

        try:
            hinst = win32api.GetModuleHandle(None)
            self._taskbar_msg = win32gui.RegisterWindowMessage("TaskbarCreated")
            wc = win32gui.WNDCLASS()
            wc.hInstance = hinst
            wc.lpszClassName = "HagenTray"
            wc.lpfnWndProc = {
                win32con.WM_DESTROY: self._on_destroy,
                win32con.WM_COMMAND: self._on_command,
                self.WM_TRAY: self._on_tray,
                self._taskbar_msg: self._on_taskbar_created,
            }
            try:
                win32gui.RegisterClass(wc)
            except win32gui.error:
                pass                          # класс уже зарегистрирован этим процессом
            self.hwnd = win32gui.CreateWindow(wc.lpszClassName, APP_NAME, win32con.WS_OVERLAPPED,
                                              0, 0, 0, 0, 0, 0, hinst, None)
            paths = icon_paths()
            flags = win32con.LR_LOADFROMFILE | win32con.LR_DEFAULTSIZE
            for key, path in paths.items():
                self._icons[key] = win32gui.LoadImage(hinst, str(path), win32con.IMAGE_ICON,
                                                      0, 0, flags)
            self._notify(win32gui.NIM_ADD)
            log.info("значок в трее показан")
        except Exception as err:
            self.error = str(err)
            log.warning("значок в трее не появился: %s", err)
            self.hwnd = None
            self._ready.set()
            return
        self._ready.set()
        win32gui.PumpMessages()

    def _notify(self, action: int) -> None:
        import win32gui

        # Порядок важности состояний — в icon_for (решение 17.09).
        # Если нужного значка нет на диске, берём обычный, а не падаем.
        want = icon_for(self._state)
        icon = self._icons.get(want) or self._icons.get("idle")
        nid = (self.hwnd, 0, win32gui.NIF_ICON | win32gui.NIF_MESSAGE | win32gui.NIF_TIP,
               self.WM_TRAY, icon, self._state["tip"][:120])
        win32gui.Shell_NotifyIcon(action, nid)

    def set_state(self, recording: bool, tip: str | None = None,
                  paused: bool = False, mic_muted: bool = False) -> None:
        """Состояние значка. Пауза и выключенный микрофон видны в трее (17.09)."""
        import win32gui

        if tip is None:
            # Состояние — через точку после короткой формы названия.
            if recording and not paused:
                tip = NAME_SHORT + " · слушает"
            elif paused:
                tip = NAME_SHORT + " · на паузе"
            elif mic_muted:
                tip = NAME_SHORT + " · микрофон выключен"
            else:
                tip = NAME_SHORT
        want = {"recording": recording, "paused": paused,
                "mic_muted": mic_muted, "tip": tip}
        if self._state == want or not self.hwnd:
            return
        self._state = want
        try:
            self._notify(win32gui.NIM_MODIFY)
        except Exception:
            log.debug("значок в трее не обновился", exc_info=True)

    def _poll(self) -> None:
        while not self._poll_stop.wait(1.0):
            try:
                self.set_state(bool(self.is_recording()), mic_muted=_mic_muted())
            except Exception:
                log.debug("опрос состояния для значка не удался", exc_info=True)

    # -------- сообщения окна
    def _on_destroy(self, hwnd: int, msg: int, wparam: int, lparam: int) -> int:
        import win32gui

        try:
            self._notify(win32gui.NIM_DELETE)
        except Exception:
            pass
        self.hwnd = None
        win32gui.PostQuitMessage(0)
        return 0

    def _on_taskbar_created(self, hwnd: int, msg: int, wparam: int, lparam: int) -> int:
        import win32gui

        # Проводник перезапустился — значок надо добавить заново
        try:
            self._notify(win32gui.NIM_ADD)
        except Exception:
            pass
        return 0

    def _on_tray(self, hwnd: int, msg: int, wparam: int, lparam: int) -> int:
        import win32con

        if lparam in (win32con.WM_LBUTTONUP, win32con.WM_LBUTTONDBLCLK):
            self._later(self.on_open)
        elif lparam == win32con.WM_RBUTTONUP:
            self._menu()
        return 0

    def _menu(self) -> None:
        import win32con
        import win32gui

        menu = win32gui.CreatePopupMenu()
        win32gui.AppendMenu(menu, win32con.MF_STRING, self.ID_OPEN, "Открыть Hagen")
        win32gui.AppendMenu(menu, win32con.MF_STRING, self.ID_RECORD,
                            "■ Остановить запись" if self._state["recording"] else "● Начать запись")
        win32gui.AppendMenu(menu, win32con.MF_SEPARATOR, 0, "")
        win32gui.AppendMenu(menu, win32con.MF_STRING, self.ID_QUIT, "Выход")
        win32gui.SetMenuDefaultItem(menu, self.ID_OPEN, 0)
        pos = win32gui.GetCursorPos()
        # без этого меню не закрывается кликом мимо — известная особенность Windows
        win32gui.SetForegroundWindow(self.hwnd)
        win32gui.TrackPopupMenu(menu, win32con.TPM_LEFTALIGN | win32con.TPM_BOTTOMALIGN,
                                pos[0], pos[1], 0, self.hwnd, None)
        win32gui.PostMessage(self.hwnd, win32con.WM_NULL, 0, 0)
        win32gui.DestroyMenu(menu)

    def _on_command(self, hwnd: int, msg: int, wparam: int, lparam: int) -> int:
        cmd = int(wparam) & 0xFFFF
        if cmd == self.ID_OPEN:
            self._later(self.on_open)
        elif cmd == self.ID_RECORD:
            self._later(self.on_record)
        elif cmd == self.ID_QUIT:
            self._later(self.on_quit)
        return 0

    @staticmethod
    def _later(fn: Callable[[], Any]) -> None:
        """Действия уводим из потока значка: остановка записи идёт секунды,
        а цикл сообщений всё это время должен откликаться."""
        def run() -> None:
            try:
                fn()
            except Exception:
                log.warning("действие из меню значка не выполнилось", exc_info=True)
        threading.Thread(target=run, name="tray-action", daemon=True).start()


def ask_yes_no(title: str, text: str) -> bool:
    """Вопрос «Да/Нет» системным окном — окно программы может быть спрятано."""
    import ctypes

    MB_YESNO, MB_ICONQUESTION, MB_TOPMOST, IDYES = 0x04, 0x20, 0x40000, 6
    return ctypes.windll.user32.MessageBoxW(None, text, title,
                                            MB_YESNO | MB_ICONQUESTION | MB_TOPMOST) == IDYES


# ---------------------------------------------------------------- окно + трей


class AppShell:
    """Связка окна программы, значка и уведомлений.

    Окно (pywebview) передаётся снаружи; всё, что с ним делается, — это
    show/hide/restore/destroy, поэтому связку можно проверить без настоящего
    окна.
    """

    def __init__(self, window: Any, server_mod: Any, notifier: Notifier | None = None,
                 ask: Callable[[str, str], bool] = ask_yes_no) -> None:
        self.window = window
        self.server = server_mod
        self.notifier = notifier
        self.ask = ask
        self.tray: Tray | None = None
        self.quitting = False
        self._hint_shown = False

    # -------- действия
    def open_window(self) -> None:
        try:
            self.window.show()
            self.window.restore()
        except Exception:
            log.debug("окно не показалось", exc_info=True)

    def toggle_recording(self) -> None:
        rec = self.server._call_active_recording()
        if rec:
            self.server._call_stop_recording(rec)
            return
        body = {"category": config.get("default_category")}
        self.server._loop_call(self.server._create_recording(body), timeout=60)

    def quit(self) -> None:
        rec = self.server._call_active_recording()
        if rec:
            if not self.ask("Hagen", "Сейчас идёт запись. Остановить её и выйти?"):
                return
            try:
                self.server._call_stop_recording(rec)
            except Exception:
                log.error("запись при выходе не остановилась", exc_info=True)
        self.quitting = True
        try:
            self.window.destroy()
        except Exception:
            log.debug("окно не закрылось", exc_info=True)

    # -------- события окна
    def on_closing(self) -> bool:
        """Крестик окна. True — закрыть, False — оставить (спрятать в трей)."""
        if self.quitting or self.tray is None or not self.tray.running:
            return True
        threading.Thread(target=self._hide, name="hide-window", daemon=True).start()
        return False

    def _hide(self) -> None:
        try:
            self.window.hide()
        except Exception:
            log.debug("окно не спряталось", exc_info=True)
        if not self._hint_shown and self.notifier is not None and not config.get("tray_hint_shown"):
            self._hint_shown = True
            self.notifier.info("Hagen свёрнут в трей и следит за звонками. "
                               "Совсем закрыть — правой кнопкой по значку → «Выход».")
            try:
                config.save({"tray_hint_shown": True})
            except Exception:
                pass

    def start(self) -> bool:
        """Поднять значок и подписать уведомления на вопросы автоматики звонков."""
        self.tray = Tray(on_open=self.open_window, on_record=self.toggle_recording,
                         on_quit=self.quit,
                         is_recording=lambda: bool(self.server._call_active_recording()))
        ok = self.tray.start()
        if self.notifier is not None:
            threading.Thread(target=self._subscribe, name="tray-subscribe", daemon=True).start()
        return ok

    def _subscribe(self) -> None:
        # автоматика звонков создаётся при старте службы — дождёмся её
        for _ in range(100):
            calls = getattr(self.server, "_calls", None)
            if calls is not None:
                calls.add_listener(self.notifier.show_prompt)
                log.info("уведомления Windows подписаны на вопросы о звонках")
                return
            time.sleep(0.2)
        log.info("автоматики звонков нет — уведомления о звонках не подписаны")

    def stop(self) -> None:
        if self.tray is not None:
            self.tray.stop()


def make_notifier(server_mod: Any, on_open: Callable[[], Any]) -> Notifier | None:
    def answer(prompt_id: str, button: str) -> None:
        calls = getattr(server_mod, "_calls", None)
        if calls is not None:
            calls.answer(prompt_id, button, source="toast")
    try:
        return Notifier(on_answer=answer, on_open=on_open)
    except Exception as err:
        log.warning("уведомления Windows с кнопками недоступны: %s", err)
        return None
