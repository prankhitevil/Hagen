# -*- coding: utf-8 -*-
"""Маршруты диктовки и сервисов, которыми делают документы.

Диктовка — ввод текста голосом в чужие окна; сервисы — список облачных
провайдеров и их модели. Перенесено из `server.py` без изменений.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from .. import config, platform
from ..events import hub
from . import deps as deps_mod
from .deps import dictation_status, start_dictation

log = logging.getLogger("hagen.server")

router = APIRouter()


@router.get("/api/dictate")
async def api_dictate_get() -> JSONResponse:
    return JSONResponse(dictation_status())


@router.post("/api/dictate/toggle")
async def api_dictate_toggle() -> JSONResponse:
    """Начать или закончить диктовку из окна — без горячей клавиши."""
    if deps_mod._dictation is None:
        raise HTTPException(status_code=400, detail="Диктовка не включена в настройках")
    loop = asyncio.get_running_loop()
    return JSONResponse(await loop.run_in_executor(None, deps_mod._dictation.toggle))


@router.post("/api/dictate/cancel")
async def api_dictate_cancel() -> JSONResponse:
    if deps_mod._dictation is None:
        return JSONResponse(dictation_status())
    loop = asyncio.get_running_loop()
    return JSONResponse(await loop.run_in_executor(None, deps_mod._dictation.cancel))


@router.get("/api/dictate/history")
async def api_dictate_history() -> JSONResponse:
    from .. import dictate

    loop = asyncio.get_running_loop()
    items = await loop.run_in_executor(None, dictate.history)
    return JSONResponse({"items": items})


@router.delete("/api/dictate/history")
async def api_dictate_history_clear() -> JSONResponse:
    from .. import dictate

    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, dictate.forget_all)
    return JSONResponse({"items": []})


@router.post("/api/dictate/paste")
async def api_dictate_paste(request: Request) -> JSONResponse:
    """Вставить текст из истории туда, где стоит курсор.

    Окно программы при этом на переднем плане, поэтому текст уйдёт в него же.
    Для чужого окна человек сначала переключается туда — но чаще из истории
    просто копируют, и это делает сама страница.
    """
    body = await request.json()
    text = str((body or {}).get("text") or "")
    if not text.strip():
        raise HTTPException(status_code=400, detail="Нечего вставлять")
    loop = asyncio.get_running_loop()
    ok = await loop.run_in_executor(None, platform.input().paste_text, text)
    return JSONResponse({"pasted": bool(ok)})


@router.post("/api/dictate/hotkey")
async def api_dictate_hotkey(request: Request) -> JSONResponse:
    """Проверить сочетание клавиш, ничего не сохраняя: годится ли оно вообще."""
    body = await request.json()
    raw = str((body or {}).get("hotkey") or "")
    try:
        hk = platform.input().parse_hotkey(raw)
    except ValueError as err:
        return JSONResponse({"ok": False, "error": str(err)})
    return JSONResponse({"ok": True, "text": hk.text, "by_hook": hk.by_hook})


# ====================================================================== сервисы и модели


@router.get("/api/providers")
async def api_providers_get() -> JSONResponse:
    from .. import providers

    return JSONResponse({"providers": providers.public_list(),
                         "selected": providers.current_id(),
                         "asr_selected": providers.asr_id()})


@router.post("/api/providers")
async def api_providers_add(request: Request) -> JSONResponse:
    """Добавить свой сервис: адрес плюс ключ. Ключ уходит в общее хранилище ключей."""
    from .. import providers

    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Ожидался объект")
    try:
        entry = providers.add(
            title=str(body.get("title") or ""),
            base_url=str(body.get("base_url") or ""),
            kind=str(body.get("kind") or "openai"),
            key=str(body.get("key") or ""),
        )
    except ValueError as err:
        raise HTTPException(status_code=400, detail=str(err))
    public = config.public()
    hub.publish({"type": "settings", "settings": public})
    return JSONResponse({"provider": {k: v for k, v in entry.items() if k != "key"},
                         "providers": providers.public_list()})


@router.delete("/api/providers/{pid}")
async def api_providers_delete(pid: str) -> JSONResponse:
    from .. import providers

    try:
        ok = providers.remove(pid)
    except ValueError as err:
        raise HTTPException(status_code=400, detail=str(err))
    if not ok:
        raise HTTPException(status_code=404, detail="Такого сервиса нет")
    hub.publish({"type": "settings", "settings": config.public()})
    return JSONResponse({"ok": True, "providers": providers.public_list()})


@router.post("/api/models")
async def api_models(provider: str = "", refresh: bool = False) -> JSONResponse:
    """Список моделей сервиса. Ходит в сеть, поэтому только по кнопке.

    POST, а не GET: запрос уходит к сервису с ключом, и его не должен уметь
    вызвать чужой сайт простой картинкой-ссылкой.

    Запрос синхронный и может занять десятки секунд, поэтому уводим его в
    отдельный поток: иначе на это время встанет и рассылка событий, и запись.
    """
    from .. import providers

    pid = (provider or "").strip() or providers.current_id()
    loop = asyncio.get_running_loop()
    try:
        data = await loop.run_in_executor(None, providers.list_models, pid, bool(refresh))
    except RuntimeError as err:
        raise HTTPException(status_code=502, detail=str(err))
    except Exception as err:
        log.warning("список моделей не получен: %s", err)
        raise HTTPException(status_code=502, detail="Не удалось получить список моделей.")
    return JSONResponse(data)


# ====================================================================== записи
