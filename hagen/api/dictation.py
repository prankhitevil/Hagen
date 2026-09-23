# -*- coding: utf-8 -*-
"""Маршруты диктовки и сервисов, которыми делают документы.

Диктовка — ввод текста голосом в чужие окна; сервисы — подключения к облаку
(документы и распознавание) и списки их моделей.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from .. import dictation, platform
from .deps import as_http

log = logging.getLogger("hagen.server")

router = APIRouter()


@router.get("/api/dictate")
async def api_dictate_get() -> JSONResponse:
    return JSONResponse(dictation.status())


@router.post("/api/dictate/toggle")
async def api_dictate_toggle() -> JSONResponse:
    """Начать или закончить диктовку из окна — без горячей клавиши."""
    loop = asyncio.get_running_loop()
    try:
        return JSONResponse(await loop.run_in_executor(None, dictation.toggle))
    except ValueError as err:
        raise as_http(err) from None


@router.post("/api/dictate/cancel")
async def api_dictate_cancel() -> JSONResponse:
    loop = asyncio.get_running_loop()
    return JSONResponse(await loop.run_in_executor(None, dictation.cancel))


@router.post("/api/dictate/open-recording")
async def api_dictate_open_recording(request: Request) -> JSONResponse:
    """Какая запись сейчас открыта в окне — для голосовых заметок (20.09).

    Клавишу заметки ловит служба, а что открыто на экране, знает только
    страница: она об этом и сообщает. Пустое значение — ни одной, и тогда
    заметка ведёт себя как обычная диктовка.
    """
    body = await request.json()
    rec_id = dictation.set_open_recording(body.get("rec_id"))
    return JSONResponse({"rec_id": rec_id})


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


@router.get("/api/connections")
async def api_connections_get() -> JSONResponse:
    """Подключения к облаку — документы и распознавание: адрес, вид, модель, без ключей.

    suggested — подходящие адреса для подсказки «?» у поля адреса.
    """
    from .. import providers

    return JSONResponse({"connections": providers.public_connections(),
                         "suggested": providers.SUGGESTED})


@router.post("/api/models")
async def api_models(role: str = "docs", refresh: bool = False) -> JSONResponse:
    """Список моделей сервиса этого подключения. Ходит в сеть, поэтому только по кнопке.

    POST, а не GET: запрос уходит к сервису с ключом, и его не должен уметь
    вызвать чужой сайт простой картинкой-ссылкой.

    Запрос синхронный и может занять десятки секунд, поэтому уводим его в
    отдельный поток: иначе на это время встанет и рассылка событий, и запись.
    """
    from .. import providers

    if role not in providers.ROLES:
        raise HTTPException(status_code=400, detail="Неизвестное подключение: %s" % role)
    loop = asyncio.get_running_loop()
    try:
        data = await loop.run_in_executor(None, providers.list_models, role, bool(refresh))
    except RuntimeError as err:
        raise HTTPException(status_code=502, detail=str(err))
    except Exception as err:
        log.warning("список моделей не получен: %s", err)
        raise HTTPException(status_code=502, detail="Не удалось получить список моделей.")
    return JSONResponse(data)


@router.post("/api/engines/claude_cli/check")
async def api_claude_cli_check() -> JSONResponse:
    """Проверить Claude CLI: найден ли, выполнен ли вход, отвечает ли.

    POST — по той же причине, что у списка моделей: запускает CLI и тратит
    крошечную часть лимита подписки. Ответ ждёт до минуты — в отдельном потоке.
    """
    from .. import minutes

    loop = asyncio.get_running_loop()
    return JSONResponse(await loop.run_in_executor(None, minutes.check_claude_cli))


@router.post("/api/engines/claude_cli/network")
async def api_claude_cli_network(request: Request) -> JSONResponse:
    """Сеть для Claude CLI: режим, порты VPN-клиентов, выбранный путь.

    С «probe»: true в теле — ещё проба выхода (адрес, страна, ответ Anthropic)
    тем путём, каким пойдёт CLI. Проба идёт в сеть, поэтому POST, и только по
    кнопке; ждёт до нескольких секунд — в отдельном потоке.
    """
    from .. import minutes

    try:
        body = await request.json()
    except Exception:
        body = {}
    probe = bool(isinstance(body, dict) and body.get("probe"))
    loop = asyncio.get_running_loop()
    return JSONResponse(await loop.run_in_executor(None, minutes.network_report, probe))


# ====================================================================== записи
