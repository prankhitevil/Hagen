# -*- coding: utf-8 -*-
"""Диктовка как часть службы: одна на программу, живёт между правками настроек.

Сам движок — `dictate.py`: клавиша, звук, распознавание, вставка. Здесь то,
что связывает его со службой: единственный экземпляр, запуск по настройке,
остановка при закрытии, какая запись открыта в окне (для голосовых заметок)
и куда девать надиктованное. Раньше это состояние жило в пакете маршрутов
(`api/deps.py`), и маршруты лезли в его приватное поле.
"""
from __future__ import annotations

import logging
from typing import Any

from . import config, platform, store
from .events import hub

log = logging.getLogger("hagen.dictation")

_dictation = None      # dictate.Dictation — горячая клавиша → текст в чужое окно

#: Какая запись открыта на экране (20.09). Клавишу голосовой заметки ловит
#: служба, а что открыто в окне — знает только страница, поэтому она и говорит
#: об этом. Окно закрыли — здесь снова пусто, и заметка ведёт себя как обычная
#: диктовка: текст не пропадает, а вставляется, как вставлялся раньше.
_open_recording: str = ""


def set_open_recording(rec_id: str) -> str:
    """Запомнить, какая запись открыта в окне. Пустое — ни одной."""
    global _open_recording
    _open_recording = str(rec_id or "").strip()
    return _open_recording


def open_recording() -> str:
    return _open_recording


def take_voice_note(text: str, kind: str = "note") -> bool:
    """Положить надиктованное в открытую запись. False — класть некуда.

    Поручение (`kind="task"`) сразу в Todoist НЕ уходит: отправка наружу
    необратима, и она остаётся за кнопкой «Поставить задачи» — тем же
    порядком, что и для поручений из документа.
    """
    rec_id = open_recording()
    if not rec_id or not str(text or "").strip():
        return False
    meta = store.add_note(rec_id, text, kind)
    if meta is None:
        # Запись успели удалить — заметку девать некуда.
        set_open_recording("")
        return False
    hub.publish({"type": "recording", "meta": meta})
    log.info("голосовая %s к записи %s: %d знаков",
             "задача" if kind == "task" else "заметка", rec_id, len(text))
    return True


def _recording_now() -> bool:
    """Идёт ли запись прямо сейчас — диктовке нельзя перебивать эфир."""
    from . import recordings

    return recordings.active_id() is not None


def start() -> None:
    """Поднять диктовку, если она включена в настройках.

    Сочетание клавиш занимается только при включённой настройке: пока диктовка
    не нужна, программа не отбирает у системы ни одной клавиши. Зовётся и при
    запуске службы, и после сохранения настроек диктовки.
    """
    global _dictation
    try:
        from . import dictate
    except Exception as err:
        log.info("диктовка недоступна: %s", err)
        return
    if _dictation is None:
        # Капсула — отдельное окно Windows поверх всех: диктуют в чужое окно, а
        # окно «Hagen» в этот момент обычно спрятано в трей.
        #
        # Заводится она не здесь, а при первой диктовке (20.09). Создание окна
        # идёт через pywin32, а он на время вызова НЕ отпускает GIL: когда
        # Windows подвешивает создание окна — а на запуске, пока на экране
        # заставка, это случается, — встаёт вся программа. Служба не успевает
        # начать слушать порт, окно не открывается, и даже страховка заставки
        # (обычный таймер Python) не срабатывает: исполнять её некому.
        _dictation = dictate.Dictation(
            busy=_recording_now,
            on_state=lambda st: hub.publish({"type": "dictate", "dictate": st}),
            capsule=lambda: platform.input().capsule(),
            on_note=take_voice_note,
        )
    try:
        _dictation.sync()
    except Exception as err:
        log.warning("диктовка не включилась: %s", err)


def stop() -> None:
    if _dictation is not None:
        try:
            _dictation.disable()
        except Exception:
            log.debug("диктовка не выключилась", exc_info=True)


def status() -> dict[str, Any]:
    if _dictation is None:
        return {"enabled": False, "stage": "off", "hotkey": "", "error": "",
                "mode": "smart", "lang": str(config.get("dictate_lang") or "ru"),
                "latched": False, "text": "", "by_hook": False}
    return _dictation.status()


def toggle() -> dict[str, Any]:
    """Начать или закончить диктовку из окна — без горячей клавиши."""
    if _dictation is None:
        raise ValueError("Диктовка не включена в настройках")
    return _dictation.toggle()


def cancel() -> dict[str, Any]:
    """Бросить начатую диктовку, ничего не вставляя."""
    if _dictation is None:
        return status()
    return _dictation.cancel()
