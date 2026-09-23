# -*- coding: utf-8 -*-
"""Маршруты «С телефона»: папки входящих и страница в локальной сети.

Тонкие: списки папок правятся обычным сохранением настроек (`inbox_folders`),
служба на порту поднимается и останавливается по настройкам (`lan.apply`) —
здесь только показать состояние и сменить ключ.
"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter
from fastapi.responses import JSONResponse

router = APIRouter()


@router.get("/api/phone/inbox")
async def api_phone_inbox() -> JSONResponse:
    """Папки входящих: какие заданы, какие есть на диске, сколько файлов взято."""
    from .. import inbox

    return JSONResponse(inbox.state())


@router.post("/api/phone/inbox/scan")
async def api_phone_inbox_scan() -> JSONResponse:
    """Обойти папки сейчас — не ждать очередного опроса."""
    from .. import inbox

    loop = asyncio.get_running_loop()
    taken = await loop.run_in_executor(None, inbox.scan_now)
    return JSONResponse({"taken": [p.name for p in taken]})


@router.get("/api/phone/lan")
async def api_phone_lan() -> JSONResponse:
    """Страница в сети: адреса, ссылка с ключом, QR-код, слушает ли порт."""
    from .. import lan

    loop = asyncio.get_running_loop()
    return JSONResponse(await loop.run_in_executor(None, lan.state))


@router.post("/api/phone/lan/key")
async def api_phone_lan_key() -> JSONResponse:
    """«Новый ключ»: прежняя ссылка перестаёт работать."""
    from .. import lan

    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, lan.new_key)
    return JSONResponse(await loop.run_in_executor(None, lan.state))
