# -*- coding: utf-8 -*-
"""Заглушки розеток: проверкам не нужно живое железо.

Зачем. Треть проверок программы просила у списка «тяжёлых» то микрофон, то
значок у часов, то Outlook — и не потому, что проверяла их, а потому что
логика по дороге дёргала систему. Теперь дорога одна, и на ней можно
поставить заглушку.

Как пользоваться::

    from hagen import platform
    from hagen.platform import fake

    platform.use("fake")
    try:
        fake.reset()
        fake.desktop.MEETING = {"subject": "Планёрка", "attendees": ["Иван Петров"]}
        ...
    finally:
        platform.use(None)

Чего заглушки НЕ делают. Они не изображают Windows и не заменяют проверки на
живом железе: «микрофон слышит звук», «значок появился у часов», «сочетание
занялось» проверяются только на настоящей машине. Заглушка отвечает на вопрос
«правильно ли логика пользуется системой», а не «работает ли система».
"""
from __future__ import annotations

from . import audio, desktop, input, shell, system  # noqa: A004, F401

__all__ = ["audio", "desktop", "input", "shell", "system", "reset"]


def reset() -> None:
    """Вернуть все заглушки в исходное состояние. Звать в начале проверки."""
    audio.reset()
    desktop.reset()
    input.reset()
    shell.reset()
    system.reset()
