# -*- coding: utf-8 -*-
"""Маршруты мелких инструментов: модели распознавания, словарь замен, микрофон Windows.

Перенесено из `server.py` без изменений.
Область объединяет короткие наборы, у которых нет своей большой темы:
«повторить загрузку модели» и выбор моделей распознавания (что скачать, что
удалить, «Сбросить всё»), словарь из ручных правок, выключатель микрофона и
папки снимков экрана.
"""
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from .. import config, platform
from ..events import hub
from ..recordings import mic_pill

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


@router.get("/api/asr/models")
async def api_asr_models() -> JSONResponse:
    """Модели распознавания: что выбрано, чего не хватает, что не нужно (решение 21.09)."""
    from .. import needs

    loop = asyncio.get_running_loop()
    return JSONResponse(await loop.run_in_executor(None, needs.models_state))


def _models_job(title: str, work) -> JSONResponse:
    """Скачивание и сброс моделей — задачей в очереди: видно ход, можно остановить."""
    from .. import jobs, needs

    if jobs.busy_with("needs", "asr-models"):
        raise HTTPException(status_code=409, detail="С моделями уже идёт работа")

    def run(handle) -> dict[str, Any]:
        res = work(handle)
        hub.publish({"type": "needs", "parts": needs.state()})
        return res

    return JSONResponse({"job_id": jobs.submit("needs", run, title, rec_id="asr-models"),
                         "title": title})


@router.post("/api/asr/models/download")
async def api_asr_models_download() -> JSONResponse:
    """«Скачать нужное»: всё, чего не хватает выбору в «Моделях»."""
    from .. import needs

    missing = needs.models_state()["missing"]
    if not missing:
        return JSONResponse({"job_id": None, "title": "Всё нужное уже на месте"})

    def work(handle) -> dict[str, Any]:
        done = []
        for item in missing:
            needs.install(item["key"], note=handle.log)
            done.append(item["key"])
        hub.publish({"type": "notice", "level": "ok",
                     "text": "Модели скачаны. Выбор сработает после перезапуска программы."})
        return {"installed": done}

    return _models_job("Скачиваю модели: %s" % ", ".join(m["title"] for m in missing), work)


@router.post("/api/asr/models/cleanup")
async def api_asr_models_cleanup(request: Request) -> JSONResponse:
    """«Удалить» или «Оставить» то, что выбору в «Моделях» не нужно."""
    from .. import needs

    body: dict[str, Any] = {}
    try:
        body = await request.json()
    except Exception:
        pass
    loop = asyncio.get_running_loop()
    try:
        if str(body.get("action") or "") == "keep":
            await loop.run_in_executor(None, needs.keep_unneeded)
            res: dict[str, Any] = {"kept": True}
        else:
            res = await loop.run_in_executor(None, needs.delete_unneeded)
    except needs.NeedError as err:
        raise HTTPException(status_code=409, detail=str(err)) from err
    res["state"] = await loop.run_in_executor(None, needs.models_state)
    return JSONResponse(res)


@router.post("/api/asr/models/reset")
async def api_asr_models_reset() -> JSONResponse:
    """«Сбросить всё»: одна точная модель и рекомендованные настройки."""
    from .. import needs

    def work(handle) -> dict[str, Any]:
        res = needs.reset_models(note=handle.log)
        hub.publish({"type": "settings", "settings": config.public()})
        hub.publish({"type": "notice", "level": "ok",
                     "text": "Модели сброшены. Настройки сработают после перезапуска программы."})
        return res

    return _models_job("Сбрасываю модели распознавания", work)


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


@router.get("/api/screenshots/folders")
async def api_screenshot_folders() -> JSONResponse:
    """Папки снимков экрана для настроек: за какими следим и что нашлось бы само.

    Сам список правится обычным сохранением настроек (`screenshot_folders`).
    """
    loop = asyncio.get_running_loop()
    system = platform.system()
    active = await loop.run_in_executor(None, system.screenshot_folders)
    found = await loop.run_in_executor(None, system.screenshot_folders, True)
    # Строки своего списка — раскрытыми: в настройках папка видна путём, а не
    # «%USERPROFILE%\…», и сразу видно, какой папки нет.
    own = []
    for raw in config.get("screenshot_folders") or []:
        raw = str(raw).strip()
        if raw:
            path = Path(os.path.expandvars(raw)).expanduser()
            own.append({"raw": raw, "path": str(path), "exists": path.is_dir()})
    return JSONResponse({"active": [str(p) for p in active], "auto": [str(p) for p in found],
                         "own": own})


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
