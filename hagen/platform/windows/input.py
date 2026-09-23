# -*- coding: utf-8 -*-
"""Розетка «Ввод» для Windows: горячая клавиша, вставка текста, капсула.

Что описывает розетка — в `hagen/platform/base.py`, класс `Input`.

Сюда переехал из `hagen/dictate.py` весь разговор с системой, дословно. Сама
диктовка — когда слушать, что делать с текстом, когда спать — осталась логикой
и живёт там же, где жила.

Четыре решения, которые важно помнить.

1. Горячая клавиша ловится через RegisterHotKey, а НЕ общим перехватом
   клавиатуры. Перехват (WH_KEYBOARD_LL) видит каждое нажатие в системе — ровно
   так устроены программы для кражи паролей, и антивирус относится к ним
   соответственно. RegisterHotKey просит у Windows одно сочетание и ничего
   больше не видит. Отпускание клавиши Windows при этом не сообщает, поэтому
   для режима «пока держу» мы опрашиваем состояние своей единственной клавиши
   через GetAsyncKeyState — это тоже не перехват.

2. Общий перехват поднимается ТОЛЬКО под сочетание из одних модификаторов и
   только если человек сам такое выбрал (решение 14.09).

3. Текст вставляется через буфер обмена и Ctrl+V, а прежнее содержимое буфера
   возвращается обратно. Набор по буквам (SendInput) в чужих окнах теряет
   символы и зависит от раскладки.

4. Своё окно и свой цикл сообщений — приём тот же, что у значка в трее и у
   звукового потока, и по той же причине: главный поток занят окном программы.
"""
from __future__ import annotations

import logging
import struct
import threading
import time
import wave
from pathlib import Path
from typing import Any, Callable

import numpy as np

from ... import config
from .win32window import MessageWindow, bgr, kill_timer, set_timer, work_area

log = logging.getLogger("hagen.input")

#: Страховка сторожу отпускания: дольше этого клавишу никто не держит, а если
#: держит — она залипла. То же число стоит пределом на диктовку в `dictate.py`:
#: здесь оно про клавишу, там про запись, и совпадение случайное.
MAX_SECONDS = 300.0


# ---------------------------------------------------------------- горячая клавиша

MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN = 0x0001, 0x0002, 0x0004, 0x0008
#: Без него Windows шлёт сообщение снова и снова, пока клавишу держат.
MOD_NOREPEAT = 0x4000
WM_HOTKEY = 0x0312

_MOD_NAMES: dict[str, int] = {
    "ctrl": MOD_CONTROL, "control": MOD_CONTROL, "ctl": MOD_CONTROL,
    "alt": MOD_ALT,
    "shift": MOD_SHIFT,
    "win": MOD_WIN, "windows": MOD_WIN, "meta": MOD_WIN,
}


def _key_codes() -> dict[str, int]:
    """Клавиши, которые можно назначить, и их коды Windows.

    Список намеренно короткий: только то, что не мешает обычному набору.
    """
    out: dict[str, int] = {
        "space": 0x20, "insert": 0x2D, "pause": 0x13, "scrolllock": 0x91,
        "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22,
        "`": 0xC0, "-": 0xBD, "=": 0xBB, "[": 0xDB, "]": 0xDD,
        ";": 0xBA, "'": 0xDE, ",": 0xBC, ".": 0xBE, "/": 0xBF, "\\": 0xDC,
    }
    for i in range(26):
        out[chr(ord("a") + i)] = 0x41 + i
    for i in range(10):
        out[str(i)] = 0x30 + i
    for i in range(1, 25):
        out["f%d" % i] = 0x70 + (i - 1)
    return out


KEY_CODES = _key_codes()


class Hotkey:
    """Разобранное сочетание клавиш: что просить у Windows и что показать человеку."""

    __slots__ = ("mods", "vk", "text")

    def __init__(self, mods: int, vk: int, text: str):
        self.mods = int(mods)
        self.vk = int(vk)
        self.text = text

    def __eq__(self, other: Any) -> bool:
        return (isinstance(other, Hotkey) and other.mods == self.mods
                and other.vk == self.vk)

    @property
    def by_hook(self) -> bool:
        """Нужен ли для этого сочетания общий перехват клавиатуры.

        Сочетание из одних модификаторов (Ctrl+Win, Alt+Win) обычным способом
        поймать НЕЛЬЗЯ: проверено опытом 14.09 — RegisterHotKey такую заявку
        принимает без ошибки, но сообщение о нажатии не присылает никогда.
        """
        return self.vk == 0

    def __repr__(self) -> str:
        return "Hotkey(%s)" % self.text


def parse_hotkey(raw: str) -> Hotkey:
    """«ctrl+shift+space» → сочетание. Бросает ValueError с понятным текстом.

    Отдельно разрешены сочетания из ОДНИХ модификаторов («ctrl+win»): у них
    vk = 0, и ловятся они другим способом — см. Hotkey.by_hook и HookListener.
    """
    parts = [p.strip().lower() for p in str(raw or "").replace(" ", "").split("+")]
    parts = [p for p in parts if p]
    if not parts:
        raise ValueError("Сочетание клавиш не задано.")
    mods = 0
    key: str | None = None
    for p in parts:
        if p in _MOD_NAMES:
            mods |= _MOD_NAMES[p]
        elif key is None:
            key = p
        else:
            raise ValueError("В сочетании должна быть только одна обычная клавиша.")
    if key is None:
        # Одних модификаторов должно быть хотя бы два: один Ctrl срабатывал бы
        # десятки раз на дню, в любом Ctrl+C.
        if bin(mods).count("1") < 2:
            raise ValueError("Из одних Ctrl, Alt, Shift и Win нужно взять хотя бы две, "
                             "например Ctrl+Win. Или добавьте обычную клавишу.")
        return Hotkey(mods, 0, format_hotkey(mods, 0))
    vk = KEY_CODES.get(key)
    if vk is None:
        raise ValueError("Клавишу «%s» назначить нельзя. Подойдут буквы, цифры, "
                         "F1–F24, пробел, Insert." % key)
    if not mods and not (0x70 <= vk <= 0x87):
        # Голая буква отобрала бы её у всей системы. F-клавиши так занимать можно.
        raise ValueError("К букве или цифре нужно добавить Ctrl, Alt, Shift или Win — "
                         "иначе клавиша перестанет работать во всех программах.")
    return Hotkey(mods, vk, format_hotkey(mods, vk))


def format_hotkey(mods: int, vk: int) -> str:
    """Как показать сочетание человеку: «Ctrl + Shift + Space»."""
    names = []
    for bit, name in ((MOD_CONTROL, "Ctrl"), (MOD_ALT, "Alt"),
                      (MOD_SHIFT, "Shift"), (MOD_WIN, "Win")):
        if mods & bit:
            names.append(name)
    if vk:
        label = next((k for k, v in KEY_CODES.items() if v == vk), "?")
        names.append(label.upper() if len(label) <= 3 else label.capitalize())
    return " + ".join(names)


class HotkeyListener(MessageWindow):
    """Слушает одно сочетание клавиш. Своё окно, свой цикл сообщений.

    on_press зовётся при нажатии, on_release — когда обычную клавишу отпустили
    (об этом Windows не сообщает, поэтому опрашиваем сами, 25 раз в секунду).
    Оба вызываются из служебных потоков: тяжёлую работу в них делать нельзя,
    иначе следующее нажатие не дойдёт.
    """

    POLL_S = 0.04
    CLASS_NAME = "HagenHotkey"
    TITLE = "Hagen — диктовка"
    THREAD_NAME = "dictate-hotkey"

    def __init__(self, hotkey: Hotkey, on_press: Callable[[], Any],
                 on_release: Callable[[float], Any] | None = None) -> None:
        super().__init__()
        self.hotkey = hotkey
        self.on_press = on_press
        self.on_release = on_release
        self.FAIL_TEXT = "не удалось занять сочетание %s" % hotkey.text
        self._stopping = False

    def stop(self, timeout: float = 5.0) -> None:
        self._stopping = True
        super().stop(timeout)

    def _handlers(self) -> dict[int, Callable[..., int]]:
        return {WM_HOTKEY: self._on_hotkey}

    def _created(self, hwnd: int) -> None:
        import win32gui

        # RegisterHotKey в pywin32 ничего не возвращает: успех — это
        # отсутствие исключения. Проверять её ответ бесполезно, любая
        # проверка считала бы удачу неудачей.
        win32gui.RegisterHotKey(hwnd, 1, self.hotkey.mods | MOD_NOREPEAT, self.hotkey.vk)
        log.info("диктовка слушает %s", self.hotkey.text)

    def _destroying(self, hwnd: int) -> None:
        import win32gui

        win32gui.UnregisterHotKey(hwnd, 1)

    def _on_hotkey(self, hwnd: int, msg: int, wparam: int, lparam: int) -> int:
        started = time.time()
        self._call(self.on_press)
        if self.on_release is not None:
            threading.Thread(target=self._wait_release, args=(started,),
                             name="dictate-release", daemon=True).start()
        return 0

    def _wait_release(self, started: float) -> None:
        """Дождаться отпускания своей клавиши. Чужих нажатий не видим."""
        import win32api

        deadline = started + MAX_SECONDS
        while time.time() < deadline and not self._stopping:
            if not (win32api.GetAsyncKeyState(self.hotkey.vk) & 0x8000):
                break
            time.sleep(self.POLL_S)
        held = time.time() - started
        self._call(lambda: self.on_release(held))       # type: ignore[misc]

    @staticmethod
    def _call(fn: Callable[[], Any]) -> None:
        try:
            fn()
        except Exception:
            log.warning("обработка горячей клавиши не удалась", exc_info=True)


# ------------------------------------------------- сочетания из одних модификаторов

#: Коды клавиш-модификаторов. Левая и правая — одно и то же: человек не должен
#: помнить, каким именно Ctrl он назначал диктовку.
_MOD_VKS: dict[int, tuple[int, ...]] = {
    MOD_CONTROL: (0x11, 0xA2, 0xA3),
    MOD_ALT: (0x12, 0xA4, 0xA5),
    MOD_SHIFT: (0x10, 0xA0, 0xA1),
    MOD_WIN: (0x5B, 0x5C),
}
#: Клавиши, у которых нажатие само по себе что-то делает: Win открывает «Пуск»,
#: Alt перекидывает управление в меню окна. Если такая клавиша ЗАМЫКАЕТ наше
#: сочетание, её нажатие до Windows не доводим — иначе поверх диктовки
#: выскакивало бы меню «Пуск».
_LOUD_VKS = frozenset(_MOD_VKS[MOD_WIN] + _MOD_VKS[MOD_ALT])

_WH_KEYBOARD_LL = 13
_KEY_DOWN = (0x0100, 0x0104)      # WM_KEYDOWN, WM_SYSKEYDOWN
_KEY_UP = (0x0101, 0x0105)        # WM_KEYUP, WM_SYSKEYUP


class HookListener:
    """Ловит сочетание из одних модификаторов, например Ctrl+Win.

    ЧЕСТНОЕ ПРЕДУПРЕЖДЕНИЕ, ради которого написан этот абзац. Другого способа
    поймать такое сочетание в Windows нет: это общий перехват клавиатуры
    (WH_KEYBOARD_LL), то есть программа получает КАЖДОЕ нажатие в системе.
    Ровно так устроены программы для кражи паролей, и антивирус относится к
    ним соответственно. Поэтому:

      * перехват поднимается ТОЛЬКО когда человек сам выбрал сочетание из
        одних модификаторов (решение 14.09); при обычном сочетании
        работает RegisterHotKey, который ничего постороннего не видит;
      * обработчик смотрит на код клавиши и, если это не один из четырёх наших
        модификаторов, сразу отдаёт событие дальше, не разбирая его;
      * ничего не запоминается и никуда не пишется — ни в журнал, ни на диск.

    Обработчик вызывается Windows в нашем потоке и держит всю клавиатуру
    системы: работать в нём долго нельзя. Поэтому он только считает нажатые
    модификаторы, а настоящая работа уходит в другой поток.
    """

    def __init__(self, hotkey: Hotkey, on_press: Callable[[], Any],
                 on_release: Callable[[float], Any] | None = None) -> None:
        self.hotkey = hotkey
        self.on_press = on_press
        self.on_release = on_release
        self.hwnd: int | None = None       # у перехвата окна нет, но признак нужен
        self.error: str | None = None
        self._hook = None
        self._proc = None                  # ссылку держим: иначе её соберёт сборщик
        self._thread_id = 0
        self._down: set[int] = set()
        self._muted: set[int] = set()
        self._through: set[int] = set()    # Win/Alt, чьё нажатие уже ушло в Windows
        self._active = False
        self._since = 0.0
        self._wanted = {bit for bit in _MOD_VKS if hotkey.mods & bit}
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()

    # -------- жизненный цикл
    def start(self, timeout: float = 5.0) -> bool:
        self._thread = threading.Thread(target=self._run, name="dictate-hook",
                                        daemon=True)
        self._thread.start()
        self._ready.wait(timeout)
        return bool(self._hook)

    def stop(self) -> None:
        import ctypes

        if self._thread_id:
            # WM_QUIT в поток перехвата: свой цикл сообщений он закончит сам,
            # а вместе с ним снимется и перехват.
            ctypes.windll.user32.PostThreadMessageW(self._thread_id, 0x0012, 0, 0)
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._hook = None

    @property
    def running(self) -> bool:
        return bool(self._hook) and self._thread is not None and self._thread.is_alive()

    def _run(self) -> None:
        import ctypes
        from ctypes import wintypes

        class KBDLLHOOKSTRUCT(ctypes.Structure):
            _fields_ = [("vkCode", wintypes.DWORD), ("scanCode", wintypes.DWORD),
                        ("flags", wintypes.DWORD), ("time", wintypes.DWORD),
                        ("dwExtraInfo", ctypes.POINTER(wintypes.ULONG))]

        proto = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int,
                                   wintypes.WPARAM, wintypes.LPARAM)
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        # Типы задаём явно. Без них ctypes считает, что функции возвращают
        # 32-битное число, и на 64-битной Windows и номер библиотеки, и номер
        # перехвата обрезаются пополам: SetWindowsHookExW отвечает отказом
        # «библиотека не найдена» (код 126), хотя с ней всё в порядке.
        kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
        kernel32.GetModuleHandleW.restype = ctypes.c_void_p
        user32.SetWindowsHookExW.argtypes = [ctypes.c_int, proto,
                                             ctypes.c_void_p, wintypes.DWORD]
        user32.SetWindowsHookExW.restype = ctypes.c_void_p
        user32.CallNextHookEx.argtypes = [ctypes.c_void_p, ctypes.c_int,
                                          wintypes.WPARAM, wintypes.LPARAM]
        user32.CallNextHookEx.restype = ctypes.c_ssize_t
        user32.UnhookWindowsHookEx.argtypes = [ctypes.c_void_p]

        def proc(ncode: int, wparam: int, lparam: int) -> int:
            if ncode == 0:
                try:
                    vk = ctypes.cast(lparam,
                                     ctypes.POINTER(KBDLLHOOKSTRUCT)).contents.vkCode
                    if self._handle(int(vk), int(wparam)):
                        return 1              # клавишу до Windows не доводим
                except Exception:
                    pass                      # перехват обязан быть незаметным
            return user32.CallNextHookEx(None, ncode, wparam, lparam)

        self._proc = proto(proc)
        try:
            self._thread_id = kernel32.GetCurrentThreadId()
            hmod = kernel32.GetModuleHandleW(None)
            self._hook = user32.SetWindowsHookExW(_WH_KEYBOARD_LL, self._proc, hmod, 0)
            if not self._hook:
                raise RuntimeError("Windows не разрешила следить за клавиатурой "
                                   "(код %d)" % ctypes.GetLastError())
            log.info("диктовка слушает %s (общим перехватом клавиатуры)",
                     self.hotkey.text)
        except Exception as err:
            self.error = str(err)
            log.warning("перехват клавиатуры не поднялся: %s", err)
            self._hook = None
            self._ready.set()
            return
        self._ready.set()
        try:
            msg = wintypes.MSG()
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                pass
        finally:
            user32.UnhookWindowsHookEx(self._hook)
            self._hook = None
            log.info("перехват клавиатуры снят")

    # -------- разбор нажатий
    def _bit(self, vk: int) -> int:
        for bit, codes in _MOD_VKS.items():
            if vk in codes:
                return bit
        return 0

    def _handle(self, vk: int, wparam: int) -> bool:
        """True — клавишу проглотить. Чужие клавиши сюда даже не доходят по смыслу."""
        bit = self._bit(vk)
        if not bit:
            return False                      # не наш модификатор — не наше дело
        if wparam in _KEY_DOWN:
            if vk in self._muted:
                return True                   # автоповтор проглоченной клавиши
            self._down.add(bit)
            if not self._active and self._down >= self._wanted:
                self._active = True
                self._since = time.time()
                _spawn(self.on_press)
                if self._through - {vk}:
                    # Win или Alt нажали РАНЬШЕ и они уже ушли в Windows: без
                    # другой клавиши между нажатием и отпусканием Windows сочтёт
                    # это одиночным нажатием — откроется «Пуск» или меню окна.
                    # Найдено 15.09 на ноутбуке: Alt+Win, когда Win нажат первым.
                    _send_mask_key()
                if vk in _LOUD_VKS:
                    # Эта клавиша замкнула сочетание: не пускаем её дальше, иначе
                    # поверх диктовки откроется «Пуск» или меню окна.
                    self._muted.add(vk)
                    return True
            if vk in _LOUD_VKS:
                self._through.add(vk)
            return False
        if wparam in _KEY_UP:
            self._down.discard(bit)
            self._through.discard(vk)
            swallow = vk in self._muted
            self._muted.discard(vk)
            if self._active and not (self._down >= self._wanted):
                self._active = False
                held = time.time() - self._since
                if self.on_release is not None:
                    _spawn(lambda: self.on_release(held))   # type: ignore[misc]
            return swallow
        return False


#: Незанятый код клавиши: Windows его ни к чему не привязывает. Тот же приём
#: использует AutoHotkey (MenuMaskKey = vkE8).
MASK_VK = 0xE8


def _send_mask_key() -> None:
    """Нажать и отпустить незанятую клавишу, пока Win/Alt ещё зажаты.

    Зовётся из обработчика перехвата в момент, когда сочетание замкнулось.
    Сама маска сюда же и вернётся, но её код не модификатор — _handle её
    пропустит, не разбирая.
    """
    try:
        import ctypes

        user32 = ctypes.windll.user32
        user32.keybd_event(MASK_VK, 0, 0, 0)
        user32.keybd_event(MASK_VK, 0, 0x0002, 0)       # KEYEVENTF_KEYUP
    except Exception:
        pass


def _spawn(fn: Callable[[], Any]) -> None:
    """Увести работу из перехвата: пока он не вернулся, вся клавиатура ждёт."""
    def run() -> None:
        try:
            fn()
        except Exception:
            log.warning("обработка сочетания не удалась", exc_info=True)
    threading.Thread(target=run, name="dictate-hook-action", daemon=True).start()


def make_listener(hotkey: Hotkey, on_press: Callable[[], Any],
                  on_release: Callable[[float], Any] | None = None) -> Any:
    """Каким способом ловить это сочетание.

    Обычное (есть буква, цифра или F-клавиша) — RegisterHotKey: программа не
    видит ничего, кроме своего сочетания. Из одних модификаторов — перехват,
    другого пути нет. Выбор делается здесь одной строкой, чтобы его было видно.
    """
    if hotkey.by_hook:
        return HookListener(hotkey, on_press, on_release)
    return HotkeyListener(hotkey, on_press, on_release)


# ---------------------------------------------------------------- вставка

_CF_UNICODETEXT = 13


def _clipboard_text() -> str | None:
    """Что сейчас в буфере обмена. None — там не текст, вернуть не сможем."""
    import win32clipboard

    try:
        win32clipboard.OpenClipboard()
    except Exception:
        return None
    try:
        if not win32clipboard.IsClipboardFormatAvailable(_CF_UNICODETEXT):
            return None
        return str(win32clipboard.GetClipboardData(_CF_UNICODETEXT))
    except Exception:
        return None
    finally:
        try:
            win32clipboard.CloseClipboard()
        except Exception:
            pass


def _set_clipboard(text: str) -> bool:
    """Положить текст в буфер. Буфером владеет вся система — бывает занят."""
    import win32clipboard

    for _ in range(5):
        try:
            win32clipboard.OpenClipboard()
        except Exception:
            time.sleep(0.05)
            continue
        try:
            win32clipboard.EmptyClipboard()
            win32clipboard.SetClipboardData(_CF_UNICODETEXT, str(text))
            return True
        except Exception as err:
            log.debug("буфер обмена не принял текст: %s", err)
            return False
        finally:
            try:
                win32clipboard.CloseClipboard()
            except Exception:
                pass
    log.warning("буфер обмена занят другой программой — вставить не вышло")
    return False


def _press_ctrl_v() -> None:
    import win32api
    import win32con

    VK_CONTROL, VK_V = 0x11, 0x56
    up = win32con.KEYEVENTF_KEYUP
    # Если человек всё ещё держит свою клавишу с Shift или Alt, Ctrl+V
    # превратился бы в другое сочетание. Поэтому сначала «отпускаем» их.
    for vk in (0x10, 0x12, 0x5B, 0x5C):     # Shift, Alt, Win левый и правый
        if win32api.GetAsyncKeyState(vk) & 0x8000:
            win32api.keybd_event(vk, 0, up, 0)
    win32api.keybd_event(VK_CONTROL, 0, 0, 0)
    win32api.keybd_event(VK_V, 0, 0, 0)
    time.sleep(0.02)
    win32api.keybd_event(VK_V, 0, up, 0)
    win32api.keybd_event(VK_CONTROL, 0, up, 0)


def paste_text(text: str, restore: bool = True) -> bool:
    """Вставить текст в окно, которое сейчас в работе.

    Фокус мы не трогаем: своё окно не показываем и наверх не поднимаем, поэтому
    Ctrl+V уходит туда, где человек и стоял курсором.
    """
    if not str(text or "").strip():
        return False
    saved = _clipboard_text() if restore else None
    if not _set_clipboard(text):
        return False
    try:
        _press_ctrl_v()
    except Exception as err:
        log.warning("вставка не удалась: %s", err)
        return False
    if restore and saved is not None:
        # Вернуть прежнее содержимое можно только после того, как окно-получатель
        # успело прочитать буфер. Делаем это не спеша и в стороне.
        def give_back() -> None:
            time.sleep(0.7)
            _set_clipboard(saved)
        threading.Thread(target=give_back, name="clip-restore", daemon=True).start()
    return True


# ---------------------------------------------------------------- звуки


def sound_paths() -> dict[str, Path]:
    """Два коротких щелчка: начало и конец. Рисуются один раз, лежат в data."""
    folder = config.DATA_DIR / "_dictate"
    folder.mkdir(parents=True, exist_ok=True)
    out = {"start": folder / "start.wav", "stop": folder / "stop.wav"}
    tones = {"start": (660.0, 990.0), "stop": (880.0, 590.0)}
    for key, path in out.items():
        if not path.exists():
            _write_click(path, *tones[key])
    return out


def _write_click(path: Path, f0: float, f1: float, ms: int = 70) -> None:
    """Короткий щелчок: тон со скольжением и быстрым затуханием."""
    rate = 44100
    n = int(rate * ms / 1000.0)
    t = np.arange(n, dtype=np.float64) / rate
    freq = f0 + (f1 - f0) * (t / max(t[-1], 1e-9))
    wave_f = np.sin(2 * np.pi * freq * t) * np.exp(-t * 38.0) * 0.35
    data = (np.clip(wave_f, -1.0, 1.0) * 32767).astype(np.int16)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(struct.pack("<%dh" % data.size, *data.tolist()))


def play(which: str) -> None:
    """Щелчок. Не звучит — не беда, работу это не останавливает."""
    if not config.get("dictate_sound", True):
        return

    def run() -> None:
        try:
            import winsound

            path = sound_paths().get(which)
            if path is None:
                return
            winsound.PlaySound(str(path), winsound.SND_FILENAME | winsound.SND_ASYNC
                               | winsound.SND_NODEFAULT)
        except Exception:
            log.debug("щелчок не прозвучал", exc_info=True)
    threading.Thread(target=run, name="dictate-sound", daemon=True).start()


# ---------------------------------------------------------------- капсула

#: Как выглядит капсула. Цвета заданы здесь, а не в теме окна: это отдельное
#: окно Windows, до нашего оформления ему не дотянуться.
_CAP_BG = (31, 36, 48)
_CAP_TEXT = (240, 244, 250)
_CAP_DOT = {"listening": (220, 38, 38), "thinking": (240, 180, 41),
            "sleeping": (240, 180, 41)}
CAP_HEIGHT = 42
CAP_BOTTOM_GAP = 64          # на сколько поднять над низом рабочей области
CAP_ALPHA = 238


class Capsule(MessageWindow):
    """Капсула поверх всех окон: видно, что программа слушает.

    Почему отдельное окно Windows, а не плашка в окне программы. Диктуют в
    ЧУЖОЕ окно — в письмо, в чат, — а окно «Hagen» в этот момент обычно
    спрятано в трей. Плашку внутри него никто не увидит, что и обнаружилось
    на первой живой проверке.

    Три флага, без которых капсула вредна, а не полезна:
      WS_EX_NOACTIVATE — окно НИКОГДА не забирает фокус. Иначе Ctrl+V вставил бы
        текст в капсулу, а не туда, где стоял курсор, — то есть в никуда;
      WS_EX_TOOLWINDOW — нет кнопки на панели задач и в Alt+Tab;
      WS_EX_TOPMOST — поверх чужих окон, ради чего всё и затевалось.

    Своё окно живёт в своём потоке со своим циклом сообщений — общее
    основание `MessageWindow`, то же, что у значка в трее и у кружка.
    """

    WM_SHOW = 0x0400 + 31        # WM_USER + 31
    WM_HIDE = 0x0400 + 32
    BLINK_MS = 600
    CLASS_NAME = "HagenCapsule"
    THREAD_NAME = "dictate-capsule"
    FAIL_TEXT = "капсула диктовки не создалась"
    STYLE = 0x80000000            # WS_POPUP
    EX_STYLE = 0x00000008 | 0x00000080 | 0x08000000 | 0x00080000   # TOPMOST|TOOLWINDOW|NOACTIVATE|LAYERED
    SIZE = (10, CAP_HEIGHT)
    BACKGROUND = 0

    def __init__(self) -> None:
        super().__init__()
        self._text = ""
        self._kind = "listening"
        self._bright = True
        self._font: int = 0
        self._font_obj: Any = None
        self._lock = threading.Lock()
        self.start(5.0)

    # -------- наружу (можно звать из любого потока)
    def show(self, text: str, kind: str = "listening") -> None:
        if not self.hwnd:
            return
        with self._lock:
            self._text, self._kind = str(text or ""), str(kind or "listening")
        self.post(self.WM_SHOW)

    def hide(self) -> None:
        self.post(self.WM_HIDE)

    def stop(self, timeout: float = 3.0) -> None:
        super().stop(timeout)

    # -------- своё окно
    def _handlers(self) -> dict[int, Callable[..., int]]:
        import win32con

        return {
            win32con.WM_PAINT: self._on_paint,
            win32con.WM_TIMER: self._on_timer,
            self.WM_SHOW: self._on_show,
            self.WM_HIDE: self._on_hide,
        }

    def _created(self, hwnd: int) -> None:
        import win32con
        import win32gui

        win32gui.SetLayeredWindowAttributes(hwnd, 0, CAP_ALPHA, win32con.LWA_ALPHA)
        self._font = self._make_font()

    def _make_font(self) -> int:
        """Шрифт надписи. Держим и сам объект, и его номер.

        Объект отпускать нельзя: пока он жив, жив и шрифт. Отпустим — Windows
        удалит шрифт из-под нас, и надпись нарисуется системным по умолчанию.
        """
        import win32ui

        self._font_obj = win32ui.CreateFont({
            "name": "Segoe UI", "height": 17, "weight": 600,
        })
        return int(self._font_obj.GetSafeHandle())

    # -------- сообщения
    def _on_show(self, hwnd: int, msg: int, wparam: int, lparam: int) -> int:
        import win32con
        import win32gui

        with self._lock:
            text = self._text
        w, h = self._measure(text)
        x, y = self._place(w, h)
        win32gui.SetWindowPos(hwnd, win32con.HWND_TOPMOST, x, y, w, h,
                              win32con.SWP_NOACTIVATE | win32con.SWP_SHOWWINDOW)
        rgn = win32gui.CreateRoundRectRgn(0, 0, w + 1, h + 1, h, h)
        win32gui.SetWindowRgn(hwnd, rgn, True)
        self._bright = True
        set_timer(hwnd, self.BLINK_MS)
        win32gui.InvalidateRect(hwnd, None, True)
        return 0

    def _on_hide(self, hwnd: int, msg: int, wparam: int, lparam: int) -> int:
        import win32con
        import win32gui

        kill_timer(hwnd)
        win32gui.ShowWindow(hwnd, win32con.SW_HIDE)
        return 0

    def _on_timer(self, hwnd: int, msg: int, wparam: int, lparam: int) -> int:
        import win32gui

        self._bright = not self._bright
        win32gui.InvalidateRect(hwnd, None, False)
        return 0

    # -------- размер, место, рисование
    def _measure(self, text: str) -> tuple[int, int]:
        """Ширина по длине надписи: короткое «Слушаю…» не должно быть во весь экран."""
        import win32gui

        width = 220
        try:
            hdc = win32gui.GetDC(0)
            try:
                old = win32gui.SelectObject(hdc, self._font)
                width = win32gui.GetTextExtentPoint32(hdc, text)[0]
                win32gui.SelectObject(hdc, old)
            finally:
                win32gui.ReleaseDC(0, hdc)
        except Exception:
            log.debug("ширину надписи измерить не вышло", exc_info=True)
        return max(150, 22 + 11 + 11 + int(width) + 22), CAP_HEIGHT

    @staticmethod
    def _place(w: int, h: int) -> tuple[int, int]:
        """Внизу по центру рабочей области — над панелью задач, а не под ней."""
        left, _top, right, bottom = work_area()
        return left + (right - left - w) // 2, bottom - h - CAP_BOTTOM_GAP

    def _on_paint(self, hwnd: int, msg: int, wparam: int, lparam: int) -> int:
        import win32con
        import win32gui

        hdc, ps = win32gui.BeginPaint(hwnd)
        try:
            left, top, right, bottom = win32gui.GetClientRect(hwnd)
            with self._lock:
                text, kind = self._text, self._kind
            fill = win32gui.CreateSolidBrush(bgr(_CAP_BG))
            win32gui.FillRect(hdc, (left, top, right, bottom), fill)
            win32gui.DeleteObject(fill)
            # Точка слева: в «слушаю» мигает, чтобы капсулу заметили боковым зрением.
            dot = _CAP_DOT.get(kind, _CAP_DOT["listening"])
            if kind == "listening" and not self._bright:
                dot = tuple(int(c * 0.35 + _CAP_BG[i] * 0.65) for i, c in enumerate(dot))
            cy = (bottom - top) // 2
            brush = win32gui.CreateSolidBrush(bgr(dot))
            pen = win32gui.CreatePen(win32con.PS_SOLID, 1, bgr(dot))
            old_b = win32gui.SelectObject(hdc, brush)
            old_p = win32gui.SelectObject(hdc, pen)
            win32gui.Ellipse(hdc, 22, cy - 5, 32, cy + 5)
            win32gui.SelectObject(hdc, old_b)
            win32gui.SelectObject(hdc, old_p)
            win32gui.DeleteObject(brush)
            win32gui.DeleteObject(pen)
            old_f = win32gui.SelectObject(hdc, self._font)
            win32gui.SetBkMode(hdc, win32con.TRANSPARENT)
            win32gui.SetTextColor(hdc, bgr(_CAP_TEXT))
            win32gui.DrawText(hdc, text, -1, (43, top, right - 18, bottom),
                              win32con.DT_SINGLELINE | win32con.DT_VCENTER
                              | win32con.DT_LEFT | win32con.DT_END_ELLIPSIS)
            win32gui.SelectObject(hdc, old_f)
        except Exception:
            log.debug("капсула не нарисовалась", exc_info=True)
        finally:
            win32gui.EndPaint(hwnd, ps)
        return 0


# ------------------------------------------------------- дверь розетки наружу
#
# Имена, которыми пользуется логика. Всё остальное выше — внутренности.


def listen(hotkey: Hotkey, on_press: Callable[[], Any],
           on_release: Callable[[float], Any] | None = None):
    """Занять сочетание и звать обработчики."""
    return make_listener(hotkey, on_press, on_release)


def click(which: str) -> None:
    """Короткий щелчок «начал» или «закончил»."""
    play(which)


def capsule() -> Capsule:
    """Капсула диктовки: окошко «слушаю…» поверх всех окон."""
    return Capsule()
