# -*- coding: utf-8 -*-
"""Плавающий кружок микрофона: видно состояние и можно нажать, не ища окно.

Решение 17.09: во время записи держать на экране полупрозрачный
кружок со значком микрофона — красный, когда микрофон включён, серый, когда
выключен; щелчок переключает.

Зачем отдельное окно Windows, а не плашка в окне программы. Во время
совещания на экране Teams, а «Hagen» свёрнут в трей: плашку внутри него
никто не увидит. Тот же довод, что у капсулы диктовки (`dictate.Capsule`), и
устроено так же — своё окно в своём потоке со своим циклом сообщений.

Флаги окна:
  WS_EX_NOACTIVATE — кружок никогда не забирает фокус: нажали во время
    разговора — чужое окно осталось активным, ничего не «перескочило»;
  WS_EX_TOOLWINDOW — нет в панели задач и в Alt+Tab;
  WS_EX_TOPMOST — поверх чужих окон, ради чего всё и затевалось;
  WS_EX_LAYERED — полупрозрачность.

Мышь кружок ловит (WS_EX_TRANSPARENT не ставим — иначе щелчок проваливался бы
в окно под ним).

Собеседники кружка не видят: окно помечено «не попадать в захват экрана», и
демонстрация экрана в Teams, запись экрана и снимок его не заберут. На своём
мониторе он при этом виден как обычно (решение 17.09).
"""
from __future__ import annotations

import logging
import threading
from typing import Any, Callable

log = logging.getLogger("hagen.miccapsule")

#: Размер кружка и отступ от правого нижнего угла рабочей области.
#: Сторона окна: в полтора раза больше прежней (решение 17.09).
SIZE = 84
GAP_RIGHT = 28
GAP_BOTTOM = 96
ALPHA = 235

#: Цвет-невидимка. Всё, что закрашено им, Windows не показывает, поэтому на
#: экране остаётся ОДИН значок — ни подложки, ни круга, ни рамки. Взят цвет,
#: которого в значке заведомо нет.
_KEY = (255, 0, 255)

# Чертёж значка взят из hagen-rec.svg: поле 64×64, скруглённый тёмный квадрат,
# две светлые стойки буквы «H» и волна между ними. Рисуем сами, а не берём
# готовый .ico, потому что волна должна ДВИГАТЬСЯ: в макете дизайнера столбики
# меняют высоту, средний в противофазе с крайними (решение 17.09).
_BOX = (43, 38, 34)          # тёмный квадрат #2b2622
_EDGE = (107, 98, 90)        # его обводка #6b625a
_STEM = (244, 237, 226)      # стойки буквы «H» #f4ede2
_WAVE_ON = (240, 80, 63)     # волна, микрофон включён #f0503f
_WAVE_OFF = (141, 133, 124)  # перекладина, микрофон выключен #8d857c

#: Столбики волны: (x, высота в покое) в поле 64×64. Ширина всех — 3.
#: Средний укорочен на 15 % по замечанию 17.09 (было 24): при
#: движении он слишком сильно бросался в глаза. В файлах значка ту же правку
#: делает дизайнер — иначе трей и кружок разойдутся.
_BARS = ((26, 12), (30.5, 20.4), (35, 12))
#: Движение волны. В макете дизайнера столбики ходят плавно (ease-in-out, цикл
#: 1,1 с), крайние и средний — в противофазе. Четыре рывка это не передавали
#: (замечено 17.09), поэтому считаем ход синусом на 24 шага: глазу
#: это уже слитно, а рисовать 24 прямоугольника раз в 45 мс процессору нипочём.
#: Шагов в цикле. Тридцать шесть вместо двадцати четырёх: при сниженной скорости
#: крупный шаг стал заметен, и ход выглядел ступеньками (замечание 17.09).
WAVE_STEPS = 36
#: Насколько столбик сжимается в нижней точке — как scaleY(.55) в макете.
WAVE_LOW = 0.55


def _wave_scale(step: int) -> tuple[float, float]:
    """Во сколько раз выше или ниже столбики на этом шаге: крайние и средний."""
    import math

    # Мягкий ход: половина косинусоиды даёт замедление у краёв, как ease-in-out.
    t = (1.0 - math.cos(2.0 * math.pi * (step % WAVE_STEPS) / WAVE_STEPS)) / 2.0
    side = WAVE_LOW + (1.0 - WAVE_LOW) * t
    middle = WAVE_LOW + (1.0 - WAVE_LOW) * (1.0 - t)
    return side, middle


#: «Не попадать в захват экрана»: окно видно глазами, но его не видят ни
#: демонстрация экрана в Teams, ни запись экрана, ни снимок (решение 17.09).
#: Работает с Windows 10 2004; на более старых Windows вернёт отказ, и
#: тогда кружок просто будет виден в трансляции — это не повод его не показывать.
WDA_EXCLUDEFROMCAPTURE = 0x00000011


def _hide_from_capture(hwnd: int) -> bool:
    import ctypes

    try:
        ok = ctypes.windll.user32.SetWindowDisplayAffinity(
            ctypes.c_void_p(hwnd), ctypes.c_uint(WDA_EXCLUDEFROMCAPTURE))
    except Exception as err:                          # noqa: BLE001
        log.info("кружок скрыть от трансляции не вышло: %s", err)
        return False
    if not ok:
        log.info("кружок скрыть от трансляции не вышло: Windows отказала "
                 "(код %s) — на трансляции он будет виден",
                 ctypes.windll.kernel32.GetLastError())
    return bool(ok)


#: SetTimer и KillTimer в pywin32 не завёрнуты, поэтому зовём Windows напрямую —
#: тот же приём, что у капсулы диктовки.
#: Шаг движения волны: 36 шагов по 36 мс — тот же цикл 1,3 с, но вдвое мельче
#: приращение, поэтому ход слитный. Скорость цикла снижена на 20 % по
#: замечанию 17.09 (было 1,08 с).
BREATH_MS = 36


def _set_timer(hwnd: int, ms: int, timer_id: int = 1) -> None:
    import ctypes

    ctypes.windll.user32.SetTimer(ctypes.c_void_p(hwnd), timer_id, int(ms), None)


def _kill_timer(hwnd: int, timer_id: int = 1) -> None:
    import ctypes

    try:
        ctypes.windll.user32.KillTimer(ctypes.c_void_p(hwnd), timer_id)
    except Exception:
        log.debug("таймер кружка не снялся", exc_info=True)


def _bgr(rgb: tuple[int, int, int]) -> int:
    """Windows хранит цвет задом наперёд: 0x00BBGGRR."""
    r, g, b = rgb
    return (int(b) << 16) | (int(g) << 8) | int(r)


class MicPill:
    """Кружок микрофона поверх всех окон."""

    WM_SHOW = 0x0400 + 41        # WM_USER + 41
    WM_HIDE = 0x0400 + 42
    WM_REPAINT = 0x0400 + 43

    def __init__(self, on_click: Callable[[], dict[str, Any]] | None = None,
                 on_change: Callable[[dict[str, Any]], None] | None = None) -> None:
        self.hwnd: int | None = None
        self.error: str | None = None
        self.hidden_from_capture = False
        self.on_click = on_click
        self.on_change = on_change
        self._muted = False
        self._shown = False
        self._recording = False     # идёт ли запись: от этого волна «дышит»
        self._phase = 0
        self._lock = threading.Lock()
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run, name="mic-capsule", daemon=True)
        self._thread.start()
        self._ready.wait(5.0)

    # -------- наружу (можно звать из любого потока)
    def show(self, muted: bool, recording: bool = True) -> None:
        if not self.hwnd:
            return
        with self._lock:
            self._muted = bool(muted)
            self._recording = bool(recording)
        self._post(self.WM_SHOW)

    def hide(self) -> None:
        if not self.hwnd:
            return
        self._post(self.WM_HIDE)

    def set_muted(self, muted: bool) -> None:
        """Перекрасить, не показывая и не пряча."""
        with self._lock:
            if self._muted == bool(muted):
                return
            self._muted = bool(muted)
        if self.hwnd and self._shown:
            self._post(self.WM_REPAINT)

    def set_recording(self, recording: bool) -> None:
        """Идёт запись — волна «дышит»; остановились — замирает (17.09)."""
        with self._lock:
            if self._recording == bool(recording):
                return
            self._recording = bool(recording)
        if self.hwnd and self._shown:
            self._post(self.WM_SHOW)        # заново поставит или снимет таймер

    def visible(self) -> bool:
        return bool(self.hwnd) and self._shown

    def stop(self) -> None:
        import win32con

        self._post(win32con.WM_CLOSE)
        self._thread.join(timeout=3)

    def _post(self, msg: int) -> None:
        import win32gui

        try:
            win32gui.PostMessage(self.hwnd, msg, 0, 0)
        except Exception:
            log.debug("кружок не откликнулся на сообщение %s", msg, exc_info=True)

    # -------- своё окно
    def _run(self) -> None:
        import win32api
        import win32con
        import win32gui

        try:
            hinst = win32api.GetModuleHandle(None)
            wc = win32gui.WNDCLASS()
            wc.hInstance = hinst
            wc.lpszClassName = "HagenMicPill"
            wc.hbrBackground = 0
            wc.hCursor = win32gui.LoadCursor(0, win32con.IDC_HAND)
            wc.lpfnWndProc = {
                win32con.WM_PAINT: self._on_paint,
                win32con.WM_ERASEBKGND: self._on_erase,
                win32con.WM_TIMER: self._on_timer,
                win32con.WM_LBUTTONUP: self._on_click,
                win32con.WM_DESTROY: self._on_destroy,
                self.WM_SHOW: self._on_show,
                self.WM_HIDE: self._on_hide,
                self.WM_REPAINT: self._on_repaint,
            }
            try:
                win32gui.RegisterClass(wc)
            except win32gui.error:
                pass                      # класс уже зарегистрирован этим процессом
            ex = (win32con.WS_EX_TOPMOST | win32con.WS_EX_TOOLWINDOW
                  | win32con.WS_EX_NOACTIVATE | win32con.WS_EX_LAYERED)
            self.hwnd = win32gui.CreateWindowEx(
                ex, wc.lpszClassName, "Микрофон", win32con.WS_POPUP,
                0, 0, SIZE, SIZE, 0, 0, hinst, None)
            # Цвет-невидимка плюс общая полупрозрачность: видно только значок.
            win32gui.SetLayeredWindowAttributes(
                self.hwnd, _bgr(_KEY), ALPHA,
                win32con.LWA_COLORKEY | win32con.LWA_ALPHA)
            self.hidden_from_capture = _hide_from_capture(self.hwnd)
        except Exception as err:                      # noqa: BLE001
            self.error = str(err)
            log.warning("кружок микрофона не создался: %s", err)
            self.hwnd = None
            self._ready.set()
            return
        self._ready.set()
        win32gui.PumpMessages()

    # -------- сообщения
    def _on_show(self, hwnd: int, msg: int, wparam: int, lparam: int) -> int:
        import win32con
        import win32gui

        x, y = self._place()
        win32gui.SetWindowPos(hwnd, win32con.HWND_TOPMOST, x, y, SIZE, SIZE,
                              win32con.SWP_NOACTIVATE | win32con.SWP_SHOWWINDOW)
        # Круглая дырка в окне: углы не рисуются и мышь сквозь них проходит.
        # Форму окну не задаём: невидимым его делает цвет-невидимка, а прежняя
        # круглая дырка только обрезала бы значок.
        self._shown = True
        with self._lock:
            breathing = self._recording and not self._muted
        if breathing:
            _set_timer(hwnd, BREATH_MS)
        else:
            _kill_timer(hwnd)
        win32gui.InvalidateRect(hwnd, None, False)
        return 0

    def _on_hide(self, hwnd: int, msg: int, wparam: int, lparam: int) -> int:
        import win32con
        import win32gui

        self._shown = False
        _kill_timer(hwnd)
        win32gui.ShowWindow(hwnd, win32con.SW_HIDE)
        return 0

    def _on_erase(self, hwnd: int, msg: int, wparam: int, lparam: int) -> int:
        """Фон не стираем: всё поле закрашивается в _on_paint.

        Иначе между стиранием и рисованием на мгновение видна пустота, и при
        движении волны значок моргает (замечено 17.09).
        """
        return 1

    def _on_timer(self, hwnd: int, msg: int, wparam: int, lparam: int) -> int:
        import win32gui

        with self._lock:
            self._phase = (self._phase + 1) % WAVE_STEPS
        win32gui.InvalidateRect(hwnd, None, False)
        return 0

    def _on_repaint(self, hwnd: int, msg: int, wparam: int, lparam: int) -> int:
        import win32gui

        win32gui.InvalidateRect(hwnd, None, False)
        return 0

    def _on_click(self, hwnd: int, msg: int, wparam: int, lparam: int) -> int:
        """Щелчок переключает микрофон. Чужое окно при этом остаётся активным."""
        import win32gui

        if self.on_click is None:
            return 0
        try:
            state = self.on_click() or {}
        except Exception as err:                      # noqa: BLE001
            log.warning("щелчок по кружку не сработал: %s", err)
            return 0
        with self._lock:
            self._muted = bool(state.get("muted"))
        win32gui.InvalidateRect(hwnd, None, False)
        if self.on_change is not None:
            try:
                self.on_change(state)
            except Exception:
                log.debug("о щелчке по кружку не удалось рассказать окну", exc_info=True)
        return 0

    def _on_destroy(self, hwnd: int, msg: int, wparam: int, lparam: int) -> int:
        import win32gui

        self.hwnd = None
        self._shown = False
        win32gui.PostQuitMessage(0)
        return 0

    @staticmethod
    def _place() -> tuple[int, int]:
        """Правый нижний угол рабочей области — над панелью задач, а не под ней."""
        import win32api
        import win32con

        try:
            info = win32api.GetMonitorInfo(
                win32api.MonitorFromPoint((0, 0), win32con.MONITOR_DEFAULTTOPRIMARY))
            left, top, right, bottom = info["Work"]
        except Exception:
            left, top = 0, 0
            right = win32api.GetSystemMetrics(win32con.SM_CXSCREEN)
            bottom = win32api.GetSystemMetrics(win32con.SM_CYSCREEN)
        return right - SIZE - GAP_RIGHT, bottom - SIZE - GAP_BOTTOM

    # -------- рисование
    def _on_paint(self, hwnd: int, msg: int, wparam: int, lparam: int) -> int:
        import win32con
        import win32gui

        hdc, ps = win32gui.BeginPaint(hwnd)
        mem = bmp = old = None
        try:
            left, top, right, bottom = win32gui.GetClientRect(hwnd)
            w, h = right - left, bottom - top
            with self._lock:
                muted, recording, phase = self._muted, self._recording, self._phase
            # Рисуем НЕ прямо на экране, а на скрытом холсте и потом переносим
            # готовое одним махом. Без этого между закраской поля и значком
            # видна пустота, и при движении волны значок моргает (17.09).
            mem = win32gui.CreateCompatibleDC(hdc)
            bmp = win32gui.CreateCompatibleBitmap(hdc, w, h)
            old = win32gui.SelectObject(mem, bmp)
            # Всё поле красим цветом-невидимкой: на экране останется только значок.
            fill = win32gui.CreateSolidBrush(_bgr(_KEY))
            win32gui.FillRect(mem, (0, 0, w, h), fill)
            win32gui.DeleteObject(fill)
            # Волна движется только во время записи; остановились — замерла на
            # первом кадре (решение 17.09).
            frame = phase if (recording and not muted) else 0
            self._draw_glyph(mem, w, h, muted, frame)
            win32gui.BitBlt(hdc, 0, 0, w, h, mem, 0, 0, win32con.SRCCOPY)
        except Exception:
            log.debug("кружок не нарисовался", exc_info=True)
        finally:
            try:
                if mem is not None:
                    if old is not None:
                        win32gui.SelectObject(mem, old)
                    win32gui.DeleteDC(mem)
                if bmp is not None:
                    win32gui.DeleteObject(bmp)
            except Exception:
                log.debug("холст не прибрался", exc_info=True)
            win32gui.EndPaint(hwnd, ps)
        return 0

    @staticmethod
    def _draw_glyph(hdc: int, w: int, h: int, muted: bool, frame: int) -> None:
        """Нарисовать значок «H-волна» по чертежу из hagen-rec.svg.

        Рисуем сами, а не показываем готовый .ico, ровно по одной причине: во
        время записи волна должна двигаться, а картинка двигаться не умеет.
        Пропорции взяты из файла дизайнера один в один, поле 64×64.
        """
        import win32con
        import win32gui

        k = min(w, h) / 64.0                      # чертёж 64×64 → наш размер

        def px(v: float) -> int:
            return int(round(v * k))

        def box(x, y, bw, bh, color, radius=0.0):
            brush = win32gui.CreateSolidBrush(_bgr(color))
            pen = win32gui.CreatePen(win32con.PS_SOLID, 1, _bgr(color))
            ob, op = win32gui.SelectObject(hdc, brush), win32gui.SelectObject(hdc, pen)
            if radius:
                win32gui.RoundRect(hdc, px(x), px(y), px(x + bw), px(y + bh),
                                   px(radius * 2), px(radius * 2))
            else:
                win32gui.Rectangle(hdc, px(x), px(y), px(x + bw), px(y + bh))
            win32gui.SelectObject(hdc, ob)
            win32gui.SelectObject(hdc, op)
            win32gui.DeleteObject(brush)
            win32gui.DeleteObject(pen)

        # тёмный квадрат с обводкой
        edge = win32gui.CreateSolidBrush(_bgr(_EDGE))
        pen = win32gui.CreatePen(win32con.PS_SOLID, max(1, px(1.5)), _bgr(_EDGE))
        ob, op = win32gui.SelectObject(hdc, edge), win32gui.SelectObject(hdc, pen)
        win32gui.RoundRect(hdc, px(0.75), px(0.75), px(63.25), px(63.25),
                           px(27), px(27))
        win32gui.SelectObject(hdc, ob)
        win32gui.SelectObject(hdc, op)
        win32gui.DeleteObject(edge)
        win32gui.DeleteObject(pen)
        box(2.5, 2.5, 59, 59, _BOX, radius=12.0)

        # две стойки буквы «H»
        box(14, 14, 9, 36, _STEM, radius=2.0)
        box(41, 14, 9, 36, _STEM, radius=2.0)

        if muted:
            # Сигнала нет — ровная серая перекладина, получается обычная «H».
            # Чертёж обновлён дизайнером 17.09: зазор 2 от каждой стойки,
            # толщина 4,8 вместо 4, углы скруглены радиусом 1.
            box(25, 29.6, 14, 4.8, _WAVE_OFF, radius=1.0)
            return

        # волна: столбики растут и опадают от своей середины
        side, middle = _wave_scale(frame)
        for i, (x, tall) in enumerate(_BARS):
            k_bar = middle if i == 1 else side
            height = tall * k_bar
            y = 32.0 - height / 2.0               # середина поля — 32
            box(x, y, 3, height, _WAVE_ON, radius=1.5)
