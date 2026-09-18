# -*- coding: utf-8 -*-
"""Заглушка розетки «Ввод»: клавиша нажимается рукой проверки.

Разбор сочетания берётся у настоящей розетки: «ctrl+shift+space» превращается
в коды клавиш одной и той же таблицей, и это не разговор с системой, а
чтение строки. Если бы заглушка разбирала по-своему, она бы однажды разошлась
с настоящей — и проверка перестала бы что-либо значить.

Всё остальное — свои: занятие клавиши, вставка текста, щелчки, капсула.
"""
from __future__ import annotations

from typing import Any, Callable

from ..windows.input import Hotkey, format_hotkey, parse_hotkey  # noqa: F401

__all__ = ["parse_hotkey", "format_hotkey", "listen", "paste_text", "click", "capsule"]

#: Куда «вставился» текст: проверка смотрит сюда вместо чужого окна.
PASTED: list[str] = []

#: Прозвучавшие щелчки, по порядку.
CLICKS: list[str] = []

#: Заведённые слушатели клавиши.
LISTENERS: list["Listener"] = []

#: Сочетания, которые «уже заняты кем-то»: занять их не выйдет.
TAKEN: set[str] = set()


def reset() -> None:
    PASTED.clear()
    CLICKS.clear()
    LISTENERS.clear()
    TAKEN.clear()


class Listener:
    """Слушатель клавиши, которого нажимает проверка."""

    def __init__(self, hotkey: Hotkey, on_press: Callable[[], Any],
                 on_release: Callable[[float], Any] | None = None) -> None:
        self.hotkey = hotkey
        self.on_press = on_press
        self.on_release = on_release
        self.running = False
        self.error: str | None = None

    def start(self, timeout: float = 5.0) -> bool:
        if self.hotkey.text in TAKEN:
            self.error = "сочетание занято другой программой"
            return False
        self.running = True
        TAKEN.add(self.hotkey.text)
        return True

    def stop(self) -> None:
        self.running = False
        TAKEN.discard(self.hotkey.text)

    # -------- руки проверки
    def press(self, held: float = 1.0) -> None:
        """Нажать и отпустить клавишу, продержав её `held` секунд."""
        self.on_press()
        if self.on_release is not None:
            self.on_release(held)


def listen(hotkey: Hotkey, on_press: Callable[[], Any],
           on_release: Callable[[float], Any] | None = None) -> Listener:
    lis = Listener(hotkey, on_press, on_release)
    LISTENERS.append(lis)
    return lis


def paste_text(text: str, restore: bool = True) -> bool:
    PASTED.append(str(text))
    return True


def click(which: str) -> None:
    CLICKS.append(str(which))


class Capsule:
    """Капсула, которая никуда не показывается, но помнит своё состояние."""

    def __init__(self) -> None:
        self.hwnd = 1
        self.state = ""
        self.text = ""
        self.shown = False

    def show(self, state: str = "", text: str = "") -> None:
        self.shown = True
        self.state = state
        self.text = text

    def hide(self) -> None:
        self.shown = False

    def close(self) -> None:
        self.shown = False
        self.hwnd = 0


def capsule() -> Capsule:
    return Capsule()
