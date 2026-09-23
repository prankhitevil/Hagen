# -*- coding: utf-8 -*-
"""Окно Win32 в своём потоке со своим циклом сообщений — одно основание на всех.

Так устроены значок у часов (`tray.Tray`), кружок микрофона
(`miccapsule.MicPill`), капсула диктовки (`input.Capsule`) и окно горячей
клавиши (`input.HotkeyListener`): главный поток занят окном программы
(pywebview), поэтому каждому нужен свой поток, свой класс окна, свой цикл
сообщений и одинаковая обвязка — «поднялось ли», «остановить», «послать
сообщение». До 22.09 эта обвязка была написана четыре раза, и правка в одной
копии не доходила до остальных.

Наследник задаёт имя класса окна, стили и обработчики сообщений; что сделать
сразу после создания окна (занять клавишу, показать значок, выставить
прозрачность) — в `_created`, что перед уничтожением — в `_destroying`.

Заставка (`splash.py`) сюда не входит нарочно: она на чистом ctypes, потому
что поднимается раньше всего остального и рисует картинку своим способом.

pywin32 подключается внутри методов, а не при импорте: розетка не должна
тянуть системные библиотеки, пока окно никому не понадобилось.
"""
from __future__ import annotations

import logging
import threading
from typing import Any, Callable

log = logging.getLogger("hagen.win32window")


def bgr(rgb: tuple[int, int, int]) -> int:
    """Windows хранит цвет задом наперёд: 0x00BBGGRR."""
    r, g, b = rgb
    return (int(b) << 16) | (int(g) << 8) | int(r)


#: SetTimer и KillTimer в pywin32 не завёрнуты (win32gui их не знает), поэтому
#: зовём Windows напрямую. Без таймера ни точка капсулы не мигала бы, ни волна
#: кружка не двигалась.
def set_timer(hwnd: int, ms: int, timer_id: int = 1) -> None:
    import ctypes

    ctypes.windll.user32.SetTimer(ctypes.c_void_p(hwnd), timer_id, int(ms), None)


def kill_timer(hwnd: int, timer_id: int = 1) -> None:
    import ctypes

    try:
        ctypes.windll.user32.KillTimer(ctypes.c_void_p(hwnd), timer_id)
    except Exception:
        log.debug("таймер окна не снялся", exc_info=True)


def work_area() -> tuple[int, int, int, int]:
    """Рабочая область главного экрана: над панелью задач, а не под ней."""
    import win32api
    import win32con

    try:
        info = win32api.GetMonitorInfo(
            win32api.MonitorFromPoint((0, 0), win32con.MONITOR_DEFAULTTOPRIMARY))
        return tuple(info["Work"])
    except Exception:
        return (0, 0, win32api.GetSystemMetrics(win32con.SM_CXSCREEN),
                win32api.GetSystemMetrics(win32con.SM_CYSCREEN))


class MessageWindow:
    """Окно в своём потоке. Наследник описывает класс окна и обработчики."""

    #: Имя класса окна Windows — своё у каждого наследника.
    CLASS_NAME = "HagenWindow"
    #: Заголовок окна и имя потока — для журнала и Диспетчера задач.
    TITLE = "Hagen"
    THREAD_NAME = "hagen-window"
    #: Что написать в журнал, если окно не поднялось.
    FAIL_TEXT = "окно не создалось"
    #: Стили окна: обычные и расширенные; размер при создании.
    STYLE = 0x00000000          # WS_OVERLAPPED
    EX_STYLE = 0
    SIZE = (0, 0)
    #: Кисть фона класса (0 — не стирать) и курсор (IDC_*); None — как у Windows.
    BACKGROUND: int | None = None
    CURSOR: int | None = None

    def __init__(self) -> None:
        self.hwnd: int | None = None
        self.error: str | None = None
        self._ready = threading.Event()
        self._thread: threading.Thread | None = None

    # -------- что описывает наследник
    def _handlers(self) -> dict[int, Callable[..., int]]:
        """Сообщения окна → обработчики (hwnd, msg, wparam, lparam). WM_DESTROY — здесь."""
        return {}

    def _created(self, hwnd: int) -> None:
        """Окно создано, цикл ещё не пошёл: занять клавишу, показать значок…

        Исключение отсюда — «окно не поднялось»: оно уничтожается, `start()`
        отвечает False, причина — в `error`.
        """

    def _destroying(self, hwnd: int) -> None:
        """Окно уничтожается: освободить клавишу, убрать значок."""

    # -------- жизненный цикл
    def start(self, timeout: float = 5.0) -> bool:
        """Поднять окно в своём потоке. True — оно есть.

        Отметку «поднялось» сбрасываем: после `stop` она осталась бы от
        прошлого раза, и повторный запуск вернул бы «не получилось», хотя окно
        на деле поднимается.
        """
        self._ready.clear()
        self._thread = threading.Thread(target=self._run, name=self.THREAD_NAME, daemon=True)
        self._thread.start()
        self._ready.wait(timeout)
        return bool(self.hwnd)

    def stop(self, timeout: float = 5.0) -> None:
        """Закрыть окно и дождаться, пока его поток выйдет."""
        import win32con

        if self.hwnd:
            self.post(win32con.WM_CLOSE)
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    @property
    def running(self) -> bool:
        return bool(self.hwnd) and self._thread is not None and self._thread.is_alive()

    def post(self, msg: int, wparam: int = 0, lparam: int = 0) -> None:
        """Послать сообщение окну — из любого потока."""
        import win32gui

        if not self.hwnd:
            return
        try:
            win32gui.PostMessage(self.hwnd, msg, wparam, lparam)
        except Exception:
            log.debug("%s: не откликнулось на сообщение %s", self.CLASS_NAME, msg,
                      exc_info=True)

    # -------- поток окна
    def _run(self) -> None:
        import win32api
        import win32con
        import win32gui

        hwnd: int | None = None
        hinst = win32api.GetModuleHandle(None)
        # Класс окна — свой у КАЖДОГО экземпляра. pywin32 привязывает словарь
        # обработчиков к классу, а не к окну: второй экземпляр того же класса
        # (например, неудачная попытка занять ту же клавишу) получал бы
        # обработчики первого, и его WM_DESTROY обнулял бы hwnd живого окна.
        cls = "%s-%x" % (self.CLASS_NAME, id(self))
        try:
            wc = win32gui.WNDCLASS()
            wc.hInstance = hinst
            wc.lpszClassName = cls
            if self.BACKGROUND is not None:
                wc.hbrBackground = self.BACKGROUND
            if self.CURSOR is not None:
                wc.hCursor = win32gui.LoadCursor(0, self.CURSOR)
            handlers: dict[int, Any] = dict(self._handlers())
            handlers[win32con.WM_DESTROY] = self._on_destroy
            wc.lpfnWndProc = handlers
            win32gui.RegisterClass(wc)
            w, h = self.SIZE
            hwnd = win32gui.CreateWindowEx(self.EX_STYLE, cls, self.TITLE,
                                           self.STYLE, 0, 0, w, h, 0, 0, hinst, None)
            self._created(hwnd)
            self.hwnd = hwnd
        except Exception as err:                      # noqa: BLE001
            self.error = str(err)
            log.warning("%s: %s", self.FAIL_TEXT, err)
            self.hwnd = None
            if hwnd:
                try:
                    win32gui.DestroyWindow(hwnd)
                except Exception:
                    log.debug("недоподнятое окно не уничтожилось", exc_info=True)
            self._unregister(cls, hinst)
            self._ready.set()
            return
        self._ready.set()
        win32gui.PumpMessages()
        self._unregister(cls, hinst)

    @staticmethod
    def _unregister(cls: str, hinst: Any) -> None:
        """Снять класс окна, когда окна больше нет: иначе они копились бы."""
        import win32gui

        try:
            win32gui.UnregisterClass(cls, hinst)
        except Exception:
            log.debug("класс окна %s не снялся", cls, exc_info=True)

    def _on_destroy(self, hwnd: int, msg: int, wparam: int, lparam: int) -> int:
        import win32gui

        try:
            self._destroying(hwnd)
        except Exception:
            log.debug("%s: уборка перед закрытием не удалась", self.CLASS_NAME, exc_info=True)
        self.hwnd = None
        win32gui.PostQuitMessage(0)
        return 0
