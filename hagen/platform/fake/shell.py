# -*- coding: utf-8 -*-
"""Заглушка розетки «Оболочка»: ни значка, ни уведомлений, ни заставки.

Всё, что программа велела показать, складывается в списки — проверка смотрит
туда вместо экрана.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

__all__ = [
    "icon_paths", "autostart_enabled", "set_autostart", "sync_autostart",
    "app_shell", "make_notifier", "ask_yes_no", "show_error", "focus_existing",
    "splash_show", "splash_close",
]

#: Значки: настоящие файлы программы, если они на месте.
ICON_DIR = Path(__file__).resolve().parents[2] / "icons"

#: Стоит ли программа в автозапуске.
AUTOSTART = False

#: Что ответить на вопрос «Да/Нет».
ANSWER = True

#: Показанные уведомления и заданные вопросы.
TOASTS: list[dict[str, Any]] = []
QUESTIONS: list[tuple[str, str]] = []

#: Сколько раз поднимали и убирали заставку.
SPLASH: dict[str, int] = {"shown": 0, "closed": 0}

#: Показанные окна ошибок.
ERRORS: list[tuple[str, str]] = []

#: Открыто ли уже другое окно программы — для проверки второго запуска.
RUNNING = False


def reset() -> None:
    global AUTOSTART, ANSWER, RUNNING
    AUTOSTART = False
    ANSWER = True
    RUNNING = False
    TOASTS.clear()
    QUESTIONS.clear()
    ERRORS.clear()
    SPLASH["shown"] = 0
    SPLASH["closed"] = 0


def icon_paths() -> dict[str, Path]:
    if not ICON_DIR.exists():
        return {}
    return {p.stem.replace("hagen-", ""): p for p in ICON_DIR.glob("hagen-*.ico")}


def autostart_enabled(folder: Path | None = None) -> bool:
    return bool(AUTOSTART)


def set_autostart(enabled: bool, folder: Path | None = None) -> dict[str, Any]:
    global AUTOSTART
    AUTOSTART = bool(enabled)
    return {"ok": True, "enabled": AUTOSTART, "path": "(заглушка)"}


def sync_autostart() -> None:
    pass


def ask_yes_no(title: str, text: str) -> bool:
    QUESTIONS.append((title, text))
    return bool(ANSWER)


def show_error(title: str, text: str) -> None:
    ERRORS.append((title, text))


def focus_existing(title: str) -> bool:
    """«Уже открытое окно» — только если проверка сама сказала, что оно есть."""
    return bool(RUNNING)


class Notifier:
    """Уведомления, которые никуда не всплывают."""

    def __init__(self, on_answer: Callable[[str, str], Any] | None = None) -> None:
        self.on_answer = on_answer

    def show(self, prompt: dict[str, Any] | None) -> None:
        TOASTS.append(dict(prompt or {}))

    def close(self) -> None:
        pass

    def answer(self, prompt_id: str, button: str) -> None:
        """Нажать кнопку в уведомлении — рукой проверки."""
        if self.on_answer is not None:
            self.on_answer(prompt_id, button)


class AppShell:
    """Связка окна и значка, в которой нет ни окна, ни значка."""

    def __init__(self, window: Any = None, server_mod: Any = None, **kw: Any) -> None:
        self.window = window
        self.server = server_mod
        self.notifier: Notifier | None = kw.get("notifier")
        self.running = False
        self.opened = 0

    def start(self) -> bool:
        self.running = True
        return True

    def stop(self) -> None:
        self.running = False

    def open_window(self) -> None:
        self.opened += 1


def app_shell(window: Any, server_mod: Any, **kw: Any) -> AppShell:
    return AppShell(window, server_mod, **kw)


def make_notifier(server_mod: Any, on_open: Callable[[], Any]) -> Notifier:
    return Notifier()


class Splash:
    def close(self, wait: bool = True) -> None:
        SPLASH["closed"] += 1


def splash_show() -> Splash:
    SPLASH["shown"] += 1
    return Splash()


def splash_close() -> None:
    SPLASH["closed"] += 1
