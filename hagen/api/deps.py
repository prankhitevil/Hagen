# -*- coding: utf-8 -*-
"""Общее для роутеров: то, что нужно нескольким областям сразу.

Здесь только связка маршрутов с ядром: перевод ошибок ядра в коды ответов,
сторожа «нужна скачанная часть» и «идёт запись», асинхронная обёртка над
обновлением заметки. Состояния программы здесь нет — оно в ядре
(`recordings`, `dictation`): пакет маршрутов не хранит ничего своего.
"""
from __future__ import annotations

import asyncio
from typing import Any

from fastapi import HTTPException

from .. import jobs, recordings


def as_http(err: Exception) -> HTTPException:
    """Ошибка ядра → ответ службы. Ядро о кодах не знает, маршруты — о смыслах.

    `LookupError` — 404, `ValueError` — 400, `jobs.Busy` и `recordings.Locked`
    — 409 (занято, попробуйте позже), остальное — 500 с текстом.

    Сверяем ТОЧНЫЙ вид ошибки, а не род: `KeyError` и `IndexError` — тоже
    `LookupError`, и промах по ключу внутри программы показывался бы человеку
    как «запись не найдена», пряча настоящий сбой.
    """
    if type(err) is LookupError:
        code = 404
    elif type(err) is ValueError:
        code = 400
    elif isinstance(err, (jobs.Busy, recordings.Locked)):
        code = 409
    else:
        code = 500
    return HTTPException(status_code=code, detail=str(err) or type(err).__name__)


async def refresh_note_async(rec_id: str, why: str) -> None:
    """Заметка в Obsidian — переписать после правки данных записи, не держа цикл событий."""
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, recordings.refresh_note, rec_id, why)


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
    if recordings.active_id() is not None:
        raise HTTPException(
            status_code=409,
            detail=("Идёт запись. Обработка видео заняла бы процессор и могла "
                    "испортить эфир — начну сразу, как нажмёте «Стоп»."),
        )


def require_media_parts(body: dict[str, Any]) -> None:
    """Обработка видео: на этом компьютере нужна точная модель, а для английского — своя."""
    from .. import media

    opts = body if isinstance(body, dict) else {}
    where = media.asr_where(opts)     # выбор задания, иначе — из настроек
    if where == "cloud":
        return                        # облако своих моделей не требует
    if str(opts.get("asr_lang") or "ru").strip().lower().startswith("en"):
        require_part("english")
    else:
        from .. import asr

        require_part(asr.precise_part())


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
