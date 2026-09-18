# -*- coding: utf-8 -*-
"""Маршруты мелких инструментов: модель эфира, словарь замен, микрофон Windows.

Перенесено из `server.py` без изменений.
Область объединяет три коротких набора, у которых нет своей большой темы:
«повторить загрузку модели», словарь из ручных правок и выключатель микрофона.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from .. import config, platform
from ..events import hub
from .deps import mic_pill

log = logging.getLogger("hagen.server")

router = APIRouter()


@router.post("/api/asr/warmup")
async def api_asr_warmup() -> JSONResponse:
    """Загрузить модель эфира заново — кнопка «Повторить» у состояния программы.

    Нужна, когда модель не загрузилась: папку переименовали, места на диске не
    было. Раньше оставалось только перезапускать программу.
    """
    from .. import asr

    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, asr.warmup, True, False)
    state = asr.live_state()
    hub.publish({"type": "ready", "asr": state})
    return JSONResponse(state)


@router.get("/api/fixes")
async def api_fixes() -> JSONResponse:
    """Словарь из ручных правок: постоянные замены и что пора предложить."""
    from .. import fixes

    return JSONResponse({"rules": fixes.rules("all"), "suggestions": fixes.suggestions(),
                         "after": config.get("fix_suggest_after")})


@router.post("/api/fixes")
async def api_fixes_post(request: Request) -> JSONResponse:
    """Добавить замену, забыть замену или перестать предлагать пару."""
    from .. import fixes

    body: dict[str, Any] = {}
    try:
        body = await request.json()
    except Exception:
        pass
    action = str(body.get("action") or "add")
    was, became = str(body.get("from") or ""), str(body.get("to") or "")
    where = str(body.get("where") or "both")
    try:
        if action == "forget":
            fixes.forget_rule(was)
        elif action == "dismiss":
            fixes.dismiss(was, became)
        elif action == "scope":
            fixes.set_scope(was, where)
        else:
            fixes.add_rule(was, became, where)
    except ValueError as err:
        raise HTTPException(status_code=400, detail=str(err)) from err
    return JSONResponse({"rules": fixes.rules("all"), "suggestions": fixes.suggestions()})


@router.get("/api/mic")
async def api_mic_state() -> JSONResponse:
    """Выключен ли сейчас микрофон Windows (решение 17.09)."""
    loop = asyncio.get_running_loop()
    return JSONResponse(await loop.run_in_executor(None, platform.audio().mic_state))


@router.post("/api/mic")
async def api_mic_set(request: Request) -> JSONResponse:
    """Включить или выключить микрофон Windows — как клавиша на ноутбуке.

    Гасится микрофон целиком, поэтому вас не слышат ни собеседники, ни
    программа: в записи будет тишина. Это и есть смысл кнопки.
    """
    body: dict[str, Any] = {}
    try:
        body = await request.json()
    except Exception:
        pass
    loop = asyncio.get_running_loop()
    if body.get("toggle") or "muted" not in body:
        out = await loop.run_in_executor(None, platform.audio().mic_toggle)
    else:
        out = await loop.run_in_executor(None, platform.audio().mic_set_muted,
                                         bool(body.get("muted")))
    pill = mic_pill(create=False)
    if pill is not None:
        pill.set_muted(bool(out.get("muted")))      # кружок не должен врать
    hub.publish({"type": "mic", "mic": out})
    return JSONResponse(out)
