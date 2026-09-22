# -*- coding: utf-8 -*-
"""Заглушка розетки «Система»: ярлыки — обычные файлы, снимки не появляются.

Ничего за пределами временной папки не трогается: ни настоящих ярлыков, ни
настоящей папки снимков экрана.
"""
from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any, Callable

__all__ = [
    "ensure_portable", "write_shortcut", "make_shortcuts", "screenshot_folders",
    "watch_screenshots", "delete_shots", "shift_shots",
    "hidden_process_flags", "open_path", "open_link", "set_console_title",
    "hide_console", "restrict_to_owner",
]

#: Куда заглушка пишет «ярлыки» и откуда берёт «снимки».
ROOT = Path(tempfile.gettempdir()) / "hagen-fake-system"

#: Написанные ярлыки: путь → чем запускать.
SHORTCUTS: dict[str, dict[str, Any]] = {}

#: Заведённые сторожа снимков.
WATCHERS: list["Watcher"] = []

#: Сколько файлов «убрал» delete_shots.
DELETED: list[str] = []

#: Что просили открыть программой по умолчанию.
OPENED: list[str] = []

#: Какие ссылки просили открыть (mailto:, tg://).
LINKS: list[str] = []

#: Файлы, которые просили закрыть от чужих.
RESTRICTED: list[str] = []

#: Что сделали с консолью.
CONSOLE: dict[str, Any] = {"title": "", "hidden": False}


def reset() -> None:
    SHORTCUTS.clear()
    WATCHERS.clear()
    DELETED.clear()
    OPENED.clear()
    RESTRICTED.clear()
    CONSOLE.update({"title": "", "hidden": False})


def ensure_portable(shortcuts: bool = True) -> dict[str, Any]:
    return {"pth": False, "cfg": False, "shortcuts": []}


def write_shortcut(link: Path, arguments: str, description: str,
                   icon: str | None = None) -> None:
    SHORTCUTS[str(link)] = {"arguments": arguments, "description": description,
                            "icon": icon}


def make_shortcuts(folders: dict[str, Path] | None = None) -> list[str]:
    """«Рабочий стол» и «Пуск» — папки внутри временной папки заглушки."""
    folders = folders or {"desktop": ROOT / "desktop", "menu": ROOT / "menu"}
    made = []
    for folder in folders.values():
        link = folder / "Hagen.lnk"
        write_shortcut(link, "run.py --app", "Hagen, Your Consigliere", "hagen-idle.ico")
        made.append(str(link))
    return made


def screenshot_folders() -> list[Path]:
    ROOT.mkdir(parents=True, exist_ok=True)
    return [ROOT]


class Watcher:
    """Сторож снимков, которому снимки подкладывает проверка."""

    def __init__(self, rec_id: str, position_s: Callable[[], float],
                 on_shot: Callable[[dict[str, Any]], Any]) -> None:
        self.rec_id = rec_id
        self.position_s = position_s
        self.on_shot = on_shot
        self.running = False

    def start(self) -> None:
        self.running = True

    def stop(self) -> None:
        self.running = False

    # -------- руки проверки
    def fire(self, name: str = "снимок.png") -> dict[str, Any]:
        """Сказать логике, что появился новый снимок."""
        entry = {"file": name, "at_s": round(float(self.position_s()), 1)}
        self.on_shot(entry)
        return entry


def watch_screenshots(rec_id: str, position_s: Callable[[], float],
                      on_shot: Callable[[dict[str, Any]], Any]) -> Watcher:
    w = Watcher(rec_id, position_s, on_shot)
    WATCHERS.append(w)
    return w


def delete_shots(rec_id: str) -> int:
    DELETED.append(str(rec_id))
    return 0


def shift_shots(items: list[dict[str, Any]], offset_s: float) -> list[dict[str, Any]]:
    return [dict(s, at_s=round(float(s.get("at_s") or 0) + offset_s, 1))
            for s in items or []]


def hidden_process_flags() -> int:
    return 0


def open_path(path: str | Path) -> None:
    OPENED.append(str(path))


def open_link(url: str) -> None:
    """Ссылка не открывается, а записывается. Схемы проверяем так же, как в Windows."""
    from ..base import LINK_SCHEMES

    link = str(url or "").strip()
    if not any(link.lower().startswith(s) for s in LINK_SCHEMES):
        raise ValueError("Такие ссылки программа не открывает: %s" % link[:40])
    LINKS.append(link)


def set_console_title(text: str) -> None:
    CONSOLE["title"] = str(text)


def hide_console() -> None:
    CONSOLE["hidden"] = True


def restrict_to_owner(path: str | Path) -> None:
    RESTRICTED.append(str(path))
