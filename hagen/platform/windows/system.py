# -*- coding: utf-8 -*-
"""Розетка «Система» для Windows: папка программы, ярлыки, снимки, процессы.

Что описывает розетка — в `hagen/platform/base.py`, класс `System`.

Код лежит рядом файлами, как и лежал: `portable.py` — пути и ярлыки,
`shots.py` — снимки экрана, `app.py` — консоль, дочерние программы без окна,
открыть файл, права на файл. Здесь только дверь наружу: имена, которые зовёт
логика, и ничего больше.

Соседи подключаются при первом обращении, а не при импорте двери: заголовок
консоли ставится в самом начале запуска, до настройки журнала, и тянуть за ним
`portable` и `shots` (а с ними настройки и базу записей) было бы рано.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from . import app

__all__ = [
    "ensure_portable",
    "write_shortcut",
    "screenshot_folders",
    "watch_screenshots",
    "delete_shots",
    "shift_shots",
    "hidden_process_flags",
    "open_path",
    "set_console_title",
    "hide_console",
    "restrict_to_owner",
]


def ensure_portable(shortcuts: bool = True) -> dict[str, Any]:
    """Всё, что нужно для работы из текущей папки: пути и ярлыки."""
    from . import portable

    return portable.ensure(shortcuts=shortcuts)


def write_shortcut(link: Path, arguments: str, description: str,
                   icon: str | None = None) -> None:
    """Написать ярлык на программу."""
    from . import portable

    portable.write_shortcut(link, arguments, description, icon)


def screenshot_folders() -> list[Path]:
    """Куда система сама складывает снимки экрана."""
    from . import shots

    return shots.screenshot_folders()


def watch_screenshots(rec_id: str, position_s: Callable[[], float],
                      on_shot: Callable[[dict[str, Any]], Any]) -> Any:
    """Сторож снимков на время записи."""
    from . import shots

    return shots.ScreenshotWatcher(rec_id, position_s=position_s, on_shot=on_shot)


def delete_shots(rec_id: str) -> int:
    """Убрать файлы снимков записи. Только свои."""
    from . import shots

    return shots.delete_shots(rec_id)


def shift_shots(items: list[dict[str, Any]], offset_s: float) -> list[dict[str, Any]]:
    """Сдвинуть время снимков: нужно при склейке дописанной части к записи.

    Windows здесь ни при чём — это арифметика, и место ей в логике записей.
    Пока живёт тут, вместе со снимками, чтобы переезд остался переездом.
    """
    from . import shots

    return shots.shifted(items, offset_s)


def hidden_process_flags() -> int:
    """Флаги запуска дочерней программы без окна консоли."""
    return app.hidden_process_flags()


def open_path(path: str | Path) -> None:
    """Открыть файл или папку программой по умолчанию."""
    app.open_path(path)


def set_console_title(text: str) -> None:
    """Заголовок окна консоли — Юникодом, мимо пускового .cmd."""
    app.set_console_title(text)


def hide_console() -> None:
    """Спрятать окно консоли."""
    app.hide_console()


def restrict_to_owner(path: str | Path) -> None:
    """Оставить файл доступным только владельцу учётной записи."""
    app.restrict_to_owner(path)
