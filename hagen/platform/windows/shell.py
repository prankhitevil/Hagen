# -*- coding: utf-8 -*-
"""Розетка «Оболочка» для Windows: значок у часов, уведомления, заставка, окна.

Что описывает розетка — в `hagen/platform/base.py`, класс `Shell`.

Код лежит рядом: `tray.py` — значок, меню, уведомления с кнопками и
автозапуск; `splash.py` — заставка при запуске; `app.py` — окно ошибки и подъём
уже открытого окна программы. Здесь только дверь наружу.

Заставка разговаривает с Windows тем же способом, что и трей, — своё окно и
свой цикл сообщений, — поэтому она здесь, а не отдельно.

Соседи подключаются при первом обращении: окно ошибки должно показаться даже
тогда, когда трей не поднялся, а заставка — раньше, чем всё остальное.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from . import app

__all__ = [
    "icon_paths",
    "autostart_enabled",
    "set_autostart",
    "sync_autostart",
    "app_shell",
    "make_notifier",
    "ask_yes_no",
    "show_error",
    "focus_existing",
    "splash_show",
    "splash_close",
]


def icon_paths() -> dict[str, Path]:
    """Пути к значкам по состояниям программы."""
    from . import tray

    return tray.icon_paths()


def autostart_enabled(folder: Path | None = None) -> bool:
    """Стоит ли программа в автозапуске."""
    from . import tray

    return tray.autostart_enabled(folder)


def set_autostart(enabled: bool, folder: Path | None = None) -> dict[str, Any]:
    """Поставить или убрать автозапуск."""
    from . import tray

    return tray.set_autostart(enabled, folder)


def sync_autostart() -> None:
    """Привести автозапуск в соответствие с настройкой."""
    from . import tray

    tray.sync_autostart()


def app_shell(window: Any, server_mod: Any, **kw: Any) -> Any:
    """Связка окна программы, значка и уведомлений."""
    from . import tray

    return tray.AppShell(window, server_mod, **kw)


def make_notifier(server_mod: Any, on_open: Callable[[], Any]) -> Any | None:
    """Уведомления с кнопками. Нет — программа работает молча."""
    from . import tray

    return tray.make_notifier(server_mod, on_open)


def ask_yes_no(title: str, text: str) -> bool:
    """Вопрос «Да/Нет» окном системы."""
    from . import tray

    return tray.ask_yes_no(title, text)


def show_error(title: str, text: str) -> None:
    """Сообщить об ошибке окном — когда консоль спрятана и больше сказать негде."""
    app.show_error(title, text)


def focus_existing(title: str) -> bool:
    """Поднять уже открытое окно программы вместо второго запуска."""
    return app.focus_existing(title)


def splash_show() -> Any | None:
    """Показать заставку. Не вышло — и ладно."""
    from . import splash

    return splash.show()


def splash_close() -> None:
    """Убрать заставку. Запуск при этом не задерживается."""
    from . import splash

    splash.close()
