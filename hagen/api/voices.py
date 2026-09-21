# -*- coding: utf-8 -*-
"""Маршруты базы голосов: люди, их образцы, копии базы.

Перенесено из `server.py` без изменений:
адреса и ответы те же. Область самая самостоятельная — трогает только модуль
`voices` и названия записей из `store`.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from .. import store

log = logging.getLogger("hagen.server")

router = APIRouter()


def _voices_payload() -> dict[str, Any]:
    """«Настройки → Голоса»: люди с карточками, возможные дубли, сводка."""
    from .. import voices

    titles: dict[str, str] = {}
    people = []
    for p in voices.list_people():
        det = voices.person_details(p["id"]) or p
        for s in det.get("samples") or []:
            rid = s.get("rec_id")
            if rid and rid not in titles:
                titles[rid] = str((store.get(rid) or {}).get("title") or "запись удалена")
            s["rec_title"] = titles.get(rid, "") if rid else ""
        people.append(det)
    return {"people": people, "duplicates": voices.possible_duplicates(), "stats": voices.stats()}


async def _voice_call(fn, *args) -> JSONResponse:
    from .. import voices

    loop = asyncio.get_running_loop()
    try:
        res = await loop.run_in_executor(None, fn, *args)
    except voices.NameTaken as err:
        return JSONResponse({"detail": str(err), "other": err.other}, status_code=409)
    except ValueError as err:
        raise HTTPException(status_code=400, detail=str(err))
    if res is None or res is False:
        raise HTTPException(status_code=404, detail="Человек не найден")
    payload = await loop.run_in_executor(None, _voices_payload)
    return JSONResponse(dict(payload, result=res))


@router.get("/api/voices")
async def api_voices() -> JSONResponse:
    loop = asyncio.get_running_loop()
    return JSONResponse(await loop.run_in_executor(None, _voices_payload))


@router.delete("/api/voices/{person_id}")
async def api_voice_delete(person_id: str) -> JSONResponse:
    from .. import voices

    return await _voice_call(voices.delete_person, person_id)


@router.patch("/api/voices/{person_id}")
async def api_voice_rename(person_id: str, request: Request) -> JSONResponse:
    """Переименовать. Имя занято другим — 409 с этим человеком (интерфейс предложит объединить)."""
    from .. import voices

    body = await request.json()
    return await _voice_call(voices.rename_person, person_id, str(body.get("name") or ""))


@router.post("/api/voices/{person_id}/merge")
async def api_voice_merge(person_id: str, request: Request) -> JSONResponse:
    """Объединить person_id в into (с копией базы)."""
    from .. import voices

    body = await request.json()
    return await _voice_call(voices.merge_people, person_id, str(body.get("into") or ""))


@router.post("/api/voices/{person_id}/alias")
async def api_voice_alias(person_id: str, request: Request) -> JSONResponse:
    from .. import voices

    body = await request.json()
    alias = str(body.get("alias") or "")
    if body.get("remove"):
        return await _voice_call(voices.remove_alias, person_id, alias)
    return await _voice_call(voices.add_alias, person_id, alias)


@router.post("/api/voices/{person_id}/kind")
async def api_voice_kind(person_id: str, request: Request) -> JSONResponse:
    """«Общее устройство» (переговорка) или обычный человек."""
    from .. import voices

    body = await request.json()
    return await _voice_call(voices.set_kind, person_id, str(body.get("kind") or ""))


@router.post("/api/voices/{person_id}/role")
async def api_voice_role(person_id: str, request: Request) -> JSONResponse:
    """Сторона и должность человека (20.09). Пустые значения роль снимают."""
    from .. import voices

    body = await request.json()
    return await _voice_call(voices.set_role, person_id,
                             body.get("side"), body.get("position"))


@router.delete("/api/voices/{person_id}/samples/{index}")
async def api_voice_forget_sample(person_id: str, index: int) -> JSONResponse:
    from .. import voices

    return await _voice_call(voices.forget_sample, person_id, index)


@router.post("/api/voices/clear")
async def api_voices_clear() -> JSONResponse:
    """Очистить базу голосов целиком (с копией, её можно вернуть)."""
    from .. import voices

    res = voices.clear_all()
    return JSONResponse(dict(_voices_payload(), result=res, backups=voices.list_backups()))


@router.get("/api/voices/backups")
async def api_voices_backups() -> JSONResponse:
    from .. import voices

    return JSONResponse({"backups": voices.list_backups()})


@router.post("/api/voices/restore")
async def api_voices_restore(request: Request) -> JSONResponse:
    """Вернуть базу голосов из копии (текущая перед этим тоже копируется)."""
    from .. import voices

    body = await request.json()
    try:
        res = voices.restore_backup(str(body.get("name") or ""))
    except ValueError as err:
        raise HTTPException(status_code=400, detail=str(err))
    return JSONResponse(dict(_voices_payload(), result=res, backups=voices.list_backups()))


@router.post("/api/voices/not-same")
async def api_voice_not_same(request: Request) -> JSONResponse:
    """«Это разные люди» — пара уходит из возможных дублей."""
    from .. import voices

    body = await request.json()
    return await _voice_call(voices.mark_not_same, str(body.get("a") or ""), str(body.get("b") or ""))
