# -*- coding: utf-8 -*-
"""Общее для роутеров: то, что нужно нескольким областям сразу.

Сюда переезжают помощники из `server.py`, которыми пользуются маршруты, — иначе
роутер тянул бы за собой `server`, а `server` тянет роутеры. Код перенесён без
изменений; в `server.py` прежние имена остаются и берутся отсюда, чтобы
остальной его код и проверки не заметили перемены.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import HTTPException

from .. import config, platform
from ..events import hub

log = logging.getLogger("hagen.server")


def refresh_note(rec_id: str, why: str) -> None:
    """Заметка уже в Obsidian — переписать её после изменения данных записи (15.09).

    Раньше заметка оставалась такой, какой её записал «Стоп»: разметка голосов,
    подписи имён и «Перечитать точнее» до неё не доходили. Заметка — выгрузка
    из программы, в Obsidian её не правят (решение 15.09).
    Зовётся из задач и из обработчиков через run_in_executor — пишет на диск.
    """
    from .. import obsidian, store

    try:
        res = obsidian.refresh_note(rec_id)
    except Exception as err:
        log.warning("запись %s: заметка в Obsidian не обновилась после «%s»: %s", rec_id, why, err)
        hub.publish({"type": "notice", "level": "err",
                     "text": "Заметка в Obsidian не обновилась: %s" % err})
        return
    if res.get("skipped"):
        return
    log.info("запись %s: заметка в Obsidian обновлена после «%s»", rec_id, why)
    hub.publish({"type": "recording", "meta": store.get(rec_id)})


async def refresh_note_async(rec_id: str, why: str) -> None:
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, refresh_note, rec_id, why)


def plural_ru(n: int, one: str, few: str, many: str) -> str:
    """«1 реплика», «2 реплики», «11 реплик» — для сообщений человеку."""
    k = abs(int(n)) % 100
    if 10 < k < 20:
        return many
    k %= 10
    if k == 1:
        return one
    if 2 <= k <= 4:
        return few
    return many


# ------------------------------------------------- кружок микрофона поверх окон
#
# Держим здесь, а не в server.py: его дёргают и маршруты `/api/mic`, и старт с
# остановкой записи. Сам кружок живёт в розетке звука, это только
# обвязка: создать по надобности, показать на время записи, перекрасить.

_mic_pill = None


def _pill_click() -> dict[str, Any]:
    """Щелчок по кружку: переключить микрофон Windows."""
    return platform.audio().mic_toggle()


def mic_pill(create: bool = True):
    """Кружок микрофона. Не создаём, пока он не понадобился."""
    global _mic_pill

    if _mic_pill is None and create:
        try:
            _mic_pill = platform.audio().mic_pill(
                on_click=_pill_click,
                on_change=lambda st: hub.publish({"type": "mic", "mic": st}))
        except Exception as err:                      # noqa: BLE001
            log.warning("кружок микрофона недоступен: %s", err)
            _mic_pill = None
    return _mic_pill


def sync_mic_pill(recording: bool) -> None:
    """Показать кружок на время записи или убрать его.

    Показываем только при записи и только если включено в настройках
    (решение 17.09).
    """
    want = bool(recording) and bool(config.get("mic_pill", True))
    pill = mic_pill(create=want)
    if pill is None:
        return
    try:
        if want:
            # Цвет говорит про микрофон, движение — про запись (17.09).
            pill.show(bool(platform.audio().mic_state().get("muted")), recording=True)
        else:
            pill.hide()
    except Exception as err:                          # noqa: BLE001
        log.debug("кружок микрофона не переключился: %s", err)


# ------------------------------------------------------------ общие сторожа
#
# «Нужна скачанная часть» и «идёт запись» зовут и маршруты видео, и остальной
# server.py, поэтому живут здесь.

def no_recording_now() -> None:
    """Не браться за видео во время записи.

    Процессор один и видеокарты нет: распознавание чужого файла посреди
    совещания съело бы эфир и потеряло живые фразы. Проверка именно на сервере,
    а не только в интерфейсе: вторая вкладка или повторный запрос обошли бы её.
    """
    from ..server import sessions          # поздний импорт: круга нет

    active = sessions.active_session()
    if active is not None:
        raise HTTPException(
            status_code=409,
            detail=("Идёт запись. Обработка видео заняла бы процессор и могла "
                    "испортить эфир — начну сразу, как нажмёте «Стоп»."),
        )


def require_media_parts(body: dict[str, Any]) -> None:
    """Обработка видео: на этом компьютере нужна точная модель, а для английского — своя."""
    opts = body if isinstance(body, dict) else {}
    where = str(opts.get("asr_files") or opts.get("where") or "").strip()
    if where == "cloud":
        return                        # облако своих моделей не требует
    if str(opts.get("asr_lang") or "ru").strip().lower().startswith("en"):
        require_part("english")
    else:
        require_part("precise")


def require_part(key: str) -> None:
    """Не начинать работу, для которой нужна нескачанная часть (решение 16.09).

    Молчаливое скачивание посреди задачи выглядит как зависание: человек ждёт
    стенограмму, а программа тянет из сети сотни мегабайт. Лучше отказать и
    сказать, где нажать кнопку.
    """
    from .. import needs

    if needs.ready(key):
        return
    part = needs.PARTS[key]
    raise HTTPException(status_code=409,
                        detail="Нужна часть «%s» (%d МБ), она ещё не скачана. "
                               "Скачать: «Настройки → Модели»."
                               % (part["title"], part["size_mb"]))


# ------------------------------------------------------------------ диктовка
#
# Горячая клавиша живёт своим потоком и переживает перезапуск настроек, поэтому
# её держат тут: зовут и жизненный цикл службы, и маршруты, и сохранение
# настроек.


def recording_now() -> bool:
    """Идёт ли запись прямо сейчас — диктовке нельзя перебивать эфир."""
    from ..server import sessions          # поздний импорт: круга нет

    return sessions.active_session() is not None


_dictation = None      # dictate.Dictation — горячая клавиша → текст в чужое окно


def start_dictation() -> None:
    """Поднять диктовку, если она включена в настройках.

    Сочетание клавиш занимается только при включённой настройке: пока диктовка
    не нужна, программа не отбирает у системы ни одной клавиши.
    """
    global _dictation
    try:
        from .. import dictate
    except Exception as err:
        log.info("диктовка недоступна: %s", err)
        return
    if _dictation is None:
        # Капсула — отдельное окно Windows поверх всех: диктуют в чужое окно, а
        # окно «Hagen» в этот момент обычно спрятано в трей.
        try:
            capsule = platform.input().capsule()
        except Exception as err:
            log.warning("капсула диктовки недоступна: %s", err)
            capsule = None
        _dictation = dictate.Dictation(
            busy=lambda: bool(recording_now()),
            on_state=lambda st: hub.publish({"type": "dictate", "dictate": st}),
            capsule=capsule,
        )
    try:
        _dictation.sync()
    except Exception as err:
        log.warning("диктовка не включилась: %s", err)


def stop_dictation() -> None:
    global _dictation
    if _dictation is not None:
        try:
            _dictation.disable()
        except Exception:
            log.debug("диктовка не выключилась", exc_info=True)


def dictation_status() -> dict[str, Any]:
    if _dictation is None:
        return {"enabled": False, "stage": "off", "hotkey": "", "error": "",
                "mode": "smart", "lang": str(config.get("dictate_lang") or "ru"),
                "latched": False, "text": "", "by_hook": False}
    return _dictation.status()
