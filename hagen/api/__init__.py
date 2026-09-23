# -*- coding: utf-8 -*-
"""Маршруты службы, разложенные по областям.

`server.py` вырос до 3900 строк и 113 маршрутов: за одни сутки он прибавил 300
строк, и каждая новая возможность удорожала следующую. Здесь маршруты лежат
областями, по роутеру FastAPI на файл; в `server.py` остаются создание
приложения, защита, рассылка событий, запуск и остановка.

Адреса и формат ответов НЕ меняются: это перекладывание кода, а не правка
поведения. Слепок маршрутов сверяется до и после каждого шага.

Круг импортов разорван так: роутеры берут общее из `hagen.events` и
`hagen.api.deps`, а `server` подключает роутеры последней строкой, когда всё
остальное в нём уже создано.
"""
from __future__ import annotations

from fastapi import FastAPI


def include_all(app: FastAPI) -> None:
    """Подключить все роутеры к приложению."""
    from . import dictation, media, phone, speakers, tools, transcript, updates, voices

    app.include_router(transcript.router)
    app.include_router(voices.router)
    app.include_router(tools.router)
    app.include_router(media.router)
    app.include_router(dictation.router)
    app.include_router(speakers.router)
    app.include_router(updates.router)
    app.include_router(phone.router)
