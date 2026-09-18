# -*- coding: utf-8 -*-
"""Заставка при запуске: картинка, пока поднимается служба и греется модель.

Зачем. От двойного щелчка по ярлыку до окна проходит около двух секунд: за это
время стартует служба, занимается порт и загружается быстрая модель
распознавания. Всё это время на экране ничего не происходит, и человек щёлкает
второй раз. Заставка показывает, что программа уже работает.

Как. Своё окно на чистом Win32 (ctypes), без Tk и без лишних зависимостей:
Tk в переносимой сборке может отсутствовать, а тянуть его ради двух секунд
незачем. Окно без рамки, поверх других, по центру того экрана, где сейчас
курсор, и не отбирает фокус (WS_EX_NOACTIVATE). Картинка рисуется один раз.

Закрывается сама: `close()` из главного потока, когда окно приложения готово,
или по таймеру — чтобы заставка не осталась висеть, если что-то пошло не так.
"""
from __future__ import annotations

import ctypes
import logging
import threading
import time
from ctypes import wintypes
from pathlib import Path

log = logging.getLogger("hagen.splash")

#: Картинка лежит в hagen/icons — на три папки выше этого файла.
IMAGE = Path(__file__).resolve().parents[2] / "icons" / "hagen-launch.png"
MAX_SECONDS = 20.0          # страховка: дольше заставка не живёт
# Показываем не меньше этого: если служба поднялась быстро, заставка иначе
# мелькнёт и исчезнет — человек успеет увидеть только вспышку (замечено
# 18.09). Окно программы при этом не ждёт: см. close().
MIN_SECONDS = 3.0

WS_POPUP = 0x80000000
WS_EX_TOPMOST = 0x00000008
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000
SW_SHOWNOACTIVATE = 4
WM_PAINT = 0x000F
WM_DESTROY = 0x0002
WM_ERASEBKGND = 0x0014
SM_CXSCREEN, SM_CYSCREEN = 0, 1


class Splash:
    """Окно заставки. Живёт в своём потоке, чтобы не держать запуск."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path or IMAGE)
        self.shown_at = 0.0
        self.hwnd = 0
        self.error: str | None = None
        self._ready = threading.Event()
        self._thread: threading.Thread | None = None
        self._bits: bytes = b""
        self._size = (0, 0)

    # ----------------------------------------------------------------- запуск
    def start(self) -> bool:
        if not self.path.exists():
            self.error = "файла заставки нет: %s" % self.path
            return False
        self._thread = threading.Thread(target=self._run, name="hagen-splash",
                                        daemon=True)
        self._thread.start()
        self._ready.wait(3.0)
        return bool(self.hwnd)

    def close(self, wait: bool = True) -> None:
        """Убрать заставку. Запуск при этом не задерживается ни на миг.

        Если окно программы готово раньше, чем заставка пробыла на экране
        MIN_SECONDS, она тут же перестаёт быть поверх всех — окно выходит
        вперёд немедленно, — а сама досиживает остаток позади него и исчезает.
        Так короткий запуск не превращается во вспышку, а долгий не ждёт.
        """
        if not self.hwnd:
            return
        if wait and self.shown_at:
            left = MIN_SECONDS - (time.monotonic() - self.shown_at)
            if left > 0:
                try:
                    # HWND_NOTOPMOST, не двигать и не менять размер, не активировать.
                    # Типы объявляем: без них -2 уезжает как 32-битное число и
                    # окно остаётся поверх всех.
                    user32 = ctypes.windll.user32
                    user32.SetWindowPos.argtypes = [
                        wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                        ctypes.c_int, ctypes.c_int, wintypes.UINT]
                    user32.SetWindowPos.restype = wintypes.BOOL
                    user32.SetWindowPos(wintypes.HWND(self.hwnd), wintypes.HWND(-2),
                                        0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010)
                except Exception:
                    pass
                threading.Timer(left, lambda: self.close(wait=False)).start()
                return
        try:
            ctypes.windll.user32.PostMessageW(self.hwnd, WM_DESTROY, 0, 0)
        except Exception:
            pass
        self.hwnd = 0

    # ------------------------------------------------------------- внутреннее
    def _load(self) -> bool:
        """Прочитать картинку в массив точек BGRA. Ошибку не поднимаем."""
        try:
            from PIL import Image

            img = Image.open(self.path).convert("RGB")
            self._size = img.size
            # GDI ждёт строки снизу вверх и порядок BGR
            self._bits = img.transpose(Image.FLIP_TOP_BOTTOM).tobytes("raw", "BGRX")
            return True
        except Exception as err:
            self.error = "картинка не прочиталась: %s" % err
            log.info("заставка пропущена: %s", err)
            return False

    def _run(self) -> None:
        try:
            if not self._load():
                self._ready.set()
                return
            self._create()
        except Exception as err:      # заставка не должна мешать запуску
            self.error = str(err)
            log.info("заставка не поднялась: %s", err)
        finally:
            self._ready.set()

    def _create(self) -> None:
        user32 = ctypes.windll.user32
        gdi32 = ctypes.windll.gdi32
        kernel32 = ctypes.windll.kernel32
        # Без объявления типов ctypes считает дескрипторы обычными int и на
        # 64-битной Windows режет их до 32 бит: CreateWindowExW отвечает
        # «int too long to convert».
        kernel32.GetModuleHandleW.restype = wintypes.HMODULE
        kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
        user32.CreateWindowExW.restype = wintypes.HWND
        user32.CreateWindowExW.argtypes = [
            wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
            wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
        user32.LoadCursorW.restype = wintypes.HANDLE
        user32.LoadCursorW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR]
        # LRESULT на 64-битной Windows — 64 бита, и lParam тоже: без объявления
        # ctypes пытается уложить их в int и падает на каждом сообщении.
        user32.DefWindowProcW.restype = ctypes.c_ssize_t
        user32.DefWindowProcW.argtypes = [wintypes.HWND, ctypes.c_uint,
                                          wintypes.WPARAM, wintypes.LPARAM]

        WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wintypes.HWND, ctypes.c_uint,
                                     wintypes.WPARAM, wintypes.LPARAM)

        def proc(hwnd, msg, wparam, lparam):
            if msg == WM_ERASEBKGND:
                return 1                     # фон не трём: сразу рисуем картинку
            if msg == WM_PAINT:
                self._paint(hwnd)
                return 0
            if msg == WM_DESTROY:
                user32.DestroyWindow(hwnd)
                user32.PostQuitMessage(0)
                return 0
            return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

        self._proc = WNDPROC(proc)           # ссылку держим: иначе соберётся GC

        class WNDCLASS(ctypes.Structure):
            _fields_ = [("style", ctypes.c_uint), ("lpfnWndProc", WNDPROC),
                        ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                        ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
                        ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
                        ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR)]

        wc = WNDCLASS()
        wc.lpfnWndProc = self._proc
        wc.hInstance = kernel32.GetModuleHandleW(None)
        wc.lpszClassName = "HagenSplash"
        wc.hCursor = user32.LoadCursorW(None, ctypes.c_wchar_p(32512))   # IDC_ARROW
        user32.RegisterClassW(ctypes.byref(wc))

        w, h = self._size
        sw = user32.GetSystemMetrics(SM_CXSCREEN)
        sh = user32.GetSystemMetrics(SM_CYSCREEN)
        x, y = (sw - w) // 2, (sh - h) // 2
        hwnd = user32.CreateWindowExW(
            WS_EX_TOPMOST | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE,
            "HagenSplash", "Hagen", WS_POPUP, x, y, w, h,
            None, None, wc.hInstance, None)
        if not hwnd:
            self.error = "окно заставки не создалось"
            return
        self.hwnd = hwnd
        user32.ShowWindow(hwnd, SW_SHOWNOACTIVATE)
        user32.UpdateWindow(hwnd)
        self.shown_at = time.monotonic()
        self._ready.set()

        # Страховка от зависшей заставки: сама уходит через MAX_SECONDS.
        threading.Timer(MAX_SECONDS, lambda: self.close(wait=False)).start()

        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        self.hwnd = 0
        gdi32  # noqa: B018 — держим ссылку на библиотеку до конца цикла

    def _paint(self, hwnd: int) -> None:
        import struct

        user32 = ctypes.windll.user32
        gdi32 = ctypes.windll.gdi32

        class PAINTSTRUCT(ctypes.Structure):
            _fields_ = [("hdc", wintypes.HDC), ("fErase", wintypes.BOOL),
                        ("rcPaint", wintypes.RECT), ("fRestore", wintypes.BOOL),
                        ("fIncUpdate", wintypes.BOOL), ("rgbReserved", ctypes.c_byte * 32)]

        ps = PAINTSTRUCT()
        hdc = user32.BeginPaint(hwnd, ctypes.byref(ps))
        w, h = self._size
        info = ctypes.create_string_buffer(
            struct.pack("<IiiHHIiiII", 40, w, h, 1, 32, 0, 0, 0, 0, 0), 52)
        gdi32.SetDIBitsToDevice(hdc, 0, 0, w, h, 0, 0, 0, h,
                                self._bits, info, 0)
        user32.EndPaint(hwnd, ctypes.byref(ps))


_current: Splash | None = None


def show() -> Splash | None:
    """Показать заставку. Ничего не поднимает наружу: не вышло — и ладно."""
    global _current
    try:
        s = Splash()
        if not s.start():
            log.info("заставка не показана: %s", s.error)
            return None
        _current = s
        return s
    except Exception as err:
        log.info("заставка не показана: %s", err)
        return None


def close() -> None:
    global _current
    if _current is not None:
        _current.close()
        _current = None
