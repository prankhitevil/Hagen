# -*- coding: utf-8 -*-
"""Выбор реализации розеток по текущей операционной системе.

Пользоваться так::

    from hagen import platform

    meeting = platform.desktop().current_meeting()

Почему через вызов, а не `from hagen.platform import desktop`. Модуль розетки
подключается при ПЕРВОМ обращении, а не при импорте пакета. Иначе `pycaw`,
`comtypes` и `win32com` грузились бы при каждом запуске службы, даже когда
возможность выключена, — а именно ради этого по всему коду и стояли поздние
импорты внутри функций.

Что описывают розетки — в `base.py`. Реализации: `windows/` — то, что работает
сейчас, `fake/` — заглушки для проверок. Папки `macos/` нет: Mac не решён, и
пустая папка в журнале версий всё равно не хранится.
"""
from __future__ import annotations

import importlib
import os
import sys
from typing import Any

from . import base

__all__ = ["audio", "desktop", "input", "shell", "system", "use", "current"]

#: Какую реализацию брать. None — по текущей ОС. Меняется только `use()`.
_forced: str | None = None

#: Уже подключённые модули розеток: {«windows.audio»: модуль}.
_loaded: dict[str, Any] = {}


def current() -> str:
    """Имя выбранной реализации: «windows» или «fake»."""
    if _forced:
        return _forced
    if os.name == "nt":
        return "windows"
    raise RuntimeError(
        "Hagen пока умеет разговаривать только с Windows (эта система — %s). "
        "Реализация для другой ОС кладётся в hagen/platform/." % sys.platform)


def use(family: str | None) -> None:
    """Взять другую реализацию. `None` — вернуться к выбору по ОС.

    Нужно проверкам: `platform.use("fake")` в начале и `platform.use(None)` в
    `finally`. В работающей программе не зовётся.
    """
    global _forced
    if family == _forced:
        return
    _forced = family
    _loaded.clear()


def socket(name: str) -> Any:
    """Модуль розетки по имени. Подключается при первом обращении."""
    if name not in base.SOCKETS:
        raise KeyError("Розетки «%s» нет. Есть: %s"
                       % (name, ", ".join(sorted(base.SOCKETS))))
    family = current()
    key = "%s.%s" % (family, name)
    mod = _loaded.get(key)
    if mod is None:
        mod = importlib.import_module("hagen.platform.%s.%s" % (family, name))
        _loaded[key] = mod
    return mod


def audio() -> base.Audio:
    """Звук: микрофон, петля вывода, устройства, выключатель микрофона."""
    return socket("audio")


def desktop() -> base.Desktop:
    """Окна: звонок, устройство звонка, встреча из календаря."""
    return socket("desktop")


def input() -> base.Input:      # noqa: A001 — розетка «ввод» так и называется
    """Ввод: горячая клавиша, вставка текста в чужое окно, капсула."""
    return socket("input")


def shell() -> base.Shell:
    """Оболочка: значок у часов, уведомления, заставка, автозапуск."""
    return socket("shell")


def system() -> base.System:
    """Система: переносимость папки, ярлыки, снимки экрана."""
    return socket("system")
