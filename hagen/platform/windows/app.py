# -*- coding: utf-8 -*-
"""Программа как процесс Windows: консоль, окно ошибки, выбор папки, второй запуск, файлы.

Сюда собраны обращения к Windows, которые жили вне розеток: в `run.py`
(заголовок и пряталка консоли, окно ошибки, подъём уже открытого окна), в
`server.py` (открыть папку или файл), в `config.py` (права на файл с ключами) и
в шести модулях, где каждый держал свою копию `CREATE_NO_WINDOW`. Код перенесён
как был.

Модуль нарочно ничего не берёт из `hagen`: часть его зовётся в самом начале
запуска, до настройки журнала, и окно ошибки должно показаться даже тогда,
когда остальная программа не поднялась. Выбор папки зовёт ещё и установщик —
до того, как появится окружение с библиотеками программы.

Наружу он выходит через двери «Системы» (`system.py`) и «Оболочки» (`shell.py`).
"""
from __future__ import annotations

import os
import threading
from pathlib import Path

#: CREATE_NO_WINDOW: дочерняя программа (ffmpeg, ffprobe, Claude CLI) без
#: чёрного окна консоли — иначе на каждый вызов оно мигало бы на экране.
NO_WINDOW = 0x08000000


def hidden_process_flags() -> int:
    """Флаги запуска дочерней программы без окна консоли."""
    return NO_WINDOW


def open_path(path: str | Path) -> None:
    """Открыть файл или папку программой, которая стоит для них по умолчанию."""
    os.startfile(str(path))       # noqa: S606 — путь всегда свой, проверен вызывающим


def open_link(url: str) -> None:
    """Открыть ссылку: почтовую (`mailto:`), Telegram (`tg://`) или обычную.

    Схемы — закрытым списком из описания розетки: `os.startfile` запускает то,
    что назначено схеме в реестре, и чужая ссылка запустила бы что угодно.
    """
    from ..base import LINK_SCHEMES

    link = str(url or "").strip()
    if not any(link.lower().startswith(s) for s in LINK_SCHEMES):
        raise ValueError("Такие ссылки программа не открывает: %s" % link[:40])
    os.startfile(link)            # noqa: S606 — схема проверена выше


def set_console_title(text: str) -> None:
    """Заголовок окна ставим из Python.

    Пусковой .cmd намеренно написан только латиницей: cmd.exe не умеет читать
    пакетный файл с кириллицей в UTF-8 — он рвёт строки посередине, и запуск
    ломается. Поэтому всё русское печатает Python, а заголовок ставим через API
    Windows, который понимает Юникод.
    """
    try:
        import ctypes

        ctypes.windll.kernel32.SetConsoleTitleW(str(text))
    except Exception:
        pass


def hide_console() -> None:
    """Спрятать чёрное окно: в режиме приложения оно не нужно."""
    try:
        import ctypes

        hwnd = ctypes.windll.kernel32.GetConsoleWindow()
        if hwnd:
            ctypes.windll.user32.ShowWindow(hwnd, 0)   # SW_HIDE
    except Exception:
        pass


def show_error(title: str, text: str) -> None:
    """Показать ошибку окном, когда консоль спрятана."""
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, str(text), str(title), 0x10)
    except Exception:
        print("%s: %s" % (title, text))


#: Окно выбора папки открыто: второе поверх первого только запутает.
_picking = threading.Lock()


def pick_folder(title: str, start: str | None = None) -> str | None:
    """Выбрать папку окном Windows. None — человек передумал.

    Tk 8.6 показывает системное окно выбора — то же, что в Проводнике, с
    адресной строкой и быстрым доступом, а не старое дерево папок, как
    диалог WinForms. tkinter есть и в переносимом Python программы, и в
    окружении, так что окно одно на установщик и на настройки.

    Окно держится поверх остальных: служба зовёт его из своего потока, и без
    этого оно могло бы открыться за окном программы.
    """
    if not _picking.acquire(blocking=False):
        raise RuntimeError("уже открыто другое такое окно")
    try:
        import tkinter
        from tkinter import filedialog

        here = Path(os.path.expandvars(str(start or "").strip())).expanduser() if start else None
        if here is not None and not here.is_absolute():
            here = None
        while here is not None and not here.is_dir():
            here = here.parent if here.parent != here else None
        root = tkinter.Tk()
        try:
            root.withdraw()
            root.attributes("-topmost", True)
            got = filedialog.askdirectory(parent=root, title=str(title or "Выбор папки"),
                                          initialdir=str(here) if here else None)
        finally:
            root.destroy()
        return os.path.normpath(got) if got else None
    finally:
        _picking.release()


def focus_existing(title: str) -> bool:
    """Поднять уже открытое окно программы с таким заголовком. True — нашли и подняли."""
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        found = []

        EnumProc = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

        def cb(hwnd, _lparam):
            if not user32.IsWindowVisible(hwnd):
                return True
            length = user32.GetWindowTextLengthW(hwnd)
            if length <= 0:
                return True
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            if buf.value.strip() == title:
                pid = wintypes.DWORD()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                if int(pid.value) != os.getpid():
                    found.append(hwnd)
                    return False
            return True

        user32.EnumWindows(EnumProc(cb), 0)
        if not found:
            return False
        hwnd = found[0]
        user32.ShowWindow(hwnd, 9)        # SW_RESTORE
        user32.SetForegroundWindow(hwnd)
        return True
    except Exception:
        return False


def restrict_to_owner(path: str | Path) -> None:
    """Файл с токенами читает только владелец учётной записи Windows."""
    try:
        user = os.environ.get("USERNAME")
        if user:
            import subprocess

            subprocess.run(
                ["icacls", str(path), "/inheritance:r", "/grant:r", f"{user}:(R,W)"],
                capture_output=True, check=False, timeout=20,
            )
    except Exception:
        pass
