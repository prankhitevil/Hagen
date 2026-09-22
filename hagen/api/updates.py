# -*- coding: utf-8 -*-
"""Маршруты «Настройки → О программе»: версия и обновление по кнопке.

Тонкие: приняли, позвали hagen/updates.py, отдали. Скачивание и подготовка
идут задачей в очереди «net» — это минуты, а окно всё это время работает.
Адрес архива со страницы не принимается: его программа берёт у GitHub сама,
при проверке внутри задачи.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, UploadFile
from fastapi.responses import JSONResponse

from .. import jobs
from ..events import hub

log = logging.getLogger("hagen.server")

router = APIRouter()


@router.get("/api/updates")
async def api_updates() -> JSONResponse:
    """Версия, можно ли обновлять, что уже готово, чем кончилось прошлое обновление."""
    from .. import updates

    return JSONResponse(updates.status())


@router.post("/api/updates/check")
async def api_updates_check() -> JSONResponse:
    """«Проверить обновления»: спросить GitHub. Только по кнопке, никогда само."""
    from .. import github, updates

    loop = asyncio.get_running_loop()
    try:
        res = await loop.run_in_executor(None, updates.check)
    except (updates.UpdateError, github.NotAvailable) as err:
        return JSONResponse({"error": str(err)})
    return JSONResponse(res)


def _submit(title: str, work: Any) -> JSONResponse:
    if jobs.busy_with("update"):
        raise HTTPException(status_code=409, detail="Обновление уже готовится")

    def run(handle) -> dict[str, Any]:
        plan = work(handle)
        hub.publish({"type": "notice", "level": "ok",
                     "text": "Обновление до %s готово. Закройте программу (значок у часов → "
                             "«Выход») и откройте снова — оно встанет при запуске." % plan["to"]})
        hub.publish({"type": "updates"})
        return plan

    return JSONResponse({"job_id": jobs.submit("update", run, title)})


@router.post("/api/updates/download")
async def api_updates_download() -> JSONResponse:
    """«Скачать и подготовить»: архив свежего выпуска и всё, что поменялось в описи."""
    from .. import updates

    def work(handle) -> dict[str, Any]:
        return updates.prepare(updates.download_latest(handle), handle)

    return _submit("Обновление программы", work)


@router.post("/api/updates/file")
async def api_updates_file(file: UploadFile) -> JSONResponse:
    """«Установить из файла…»: архив выпуска, который человек скачал сам."""
    from .. import updates

    name = Path(file.filename or "").name
    if not name.lower().endswith(".zip"):
        raise HTTPException(status_code=400, detail="Нужен архив выпуска .zip")
    incoming = updates.work_dir(updates.PROJECT_DIR) / "incoming"
    incoming.mkdir(parents=True, exist_ok=True)
    tmp = incoming / ("%s_%s" % (uuid.uuid4().hex[:8], name))
    with open(tmp, "wb") as fh:
        while True:
            chunk = await file.read(1024 * 1024)
            if not chunk:
                break
            fh.write(chunk)
    await file.close()

    def work(handle) -> dict[str, Any]:
        try:
            return updates.prepare(tmp, handle)
        finally:
            tmp.unlink(missing_ok=True)

    return _submit("Обновление программы из файла", work)


@router.post("/api/updates/discard")
async def api_updates_discard() -> JSONResponse:
    """Передумали ставить подготовленное обновление."""
    from .. import updates

    updates.discard()
    return JSONResponse(updates.status())


@router.post("/api/updates/seen")
async def api_updates_seen() -> JSONResponse:
    """Итог прошлого обновления показан — больше не показывать."""
    from .. import updates

    updates.mark_seen()
    return JSONResponse({"ok": True})
