# -*- coding: utf-8 -*-
"""Маршруты видео и источников: файлы, ссылки, SharePoint и Teams.

Перенесено из `server.py` без изменений:
адреса и ответы те же.
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse

from .. import audio_io, config, jobs, store
from ..config import DATA_DIR
from ..events import hub
from .deps import no_recording_now, require_media_parts, require_part

log = logging.getLogger("hagen.server")

#: Куда кладём перетащенный файл до обработки — то же место, что и раньше.
UPLOAD_DIR = DATA_DIR / "_uploads"

router = APIRouter()


def _media_opts(body: dict[str, Any]) -> dict[str, Any]:
    """Настройки задания: что пришло из формы, остальное — из общих настроек."""
    out: dict[str, Any] = {}
    for key in ("category", "keep_video", "video_only", "prefer_transcript",
                "yt_auto_subs", "max_height", "asr_files", "asr_lang",
                "make_summary", "diarize_auto", "video_kind", "store_media"):
        if key in body and body[key] is not None:
            out[key] = body[key]
    return out


@router.get("/api/media/options")
async def api_media_options() -> JSONResponse:
    """Что показать в форме раздела «Видео» и что из этого вообще доступно."""
    from .. import asr, media, subs

    en_ok, en_why = (False, "")
    try:
        en_ok, en_why = asr.english_available()
    except Exception as err:
        en_why = str(err)
    cloud_ok, cloud_why = False, "Распознавание в облаке в этой сборке недоступно."
    try:
        from .. import asr_cloud

        cloud_ok, cloud_why = asr_cloud.available()
    except ImportError:
        pass
    except Exception as err:
        cloud_why = str(err)
    web_ok, web_why = False, ""
    try:
        from ..sources import webauth

        web_ok, web_why = webauth.available()
    except Exception as err:
        web_why = str(err)
    sp_in = False
    try:
        from ..sources import sharepoint

        sp_in = bool(sharepoint.logged_in())
    except Exception:
        pass
    return JSONResponse({
        "defaults": {
            "keep_video": bool(config.get("keep_video")),
            "video_only": bool(config.get("video_only")),
            "prefer_transcript": bool(config.get("prefer_transcript")),
            "yt_auto_subs": bool(config.get("yt_auto_subs")),
            "max_height": int(config.get("max_height") or 720),
            "asr_files": str(config.get("asr_files") or "local"),
            "asr_lang": str(config.get("asr_lang") or "ru"),
            "category": str(config.get("video_category")
                            or config.get("default_category") or ""),
            "chunk_min": int(config.get("chunk_min") or 15),
            "make_summary": bool(config.get("make_summary")),
        },
        "assets_dir": str(store.assets_root()),
        "english": {"ready": en_ok, "why": en_why},
        "cloud": {"ready": cloud_ok, "why": cloud_why},
        "webauth": {"ready": web_ok, "why": web_why},
        "sharepoint": {"logged_in": sp_in},
        "transcript_exts": sorted(subs.TRANSCRIPT_EXTS),
        "stages": list(media.STAGES),
        "categories": config.get("categories") or [],
    })


@router.post("/api/media/probe")
async def api_media_probe(request: Request) -> JSONResponse:
    """Прочитать ссылку: название, длительность, субтитры, доступные качества."""
    from .. import fetch

    body = await request.json()
    url = str((body or {}).get("url") or "").strip()
    if not fetch.looks_like_url(url):
        raise HTTPException(status_code=400,
                            detail="Это не похоже на ссылку. Нужен адрес, начинающийся с http.")
    loop = asyncio.get_running_loop()
    try:
        info = await loop.run_in_executor(None, fetch.probe, url)
    except fetch.FetchError as err:
        raise HTTPException(status_code=400, detail=str(err))
    except Exception as err:
        log.warning("ссылка не прочиталась: %s", err)
        raise HTTPException(status_code=400, detail="Не удалось прочитать ссылку.")
    return JSONResponse(info)


@router.post("/api/media/link")
async def api_media_link(request: Request) -> JSONResponse:
    """Обработать видео по ссылке по заданным в форме правилам."""
    from .. import fetch, media

    no_recording_now()
    body = await request.json()
    url = str((body or {}).get("url") or "").strip()
    if not fetch.looks_like_url(url):
        raise HTTPException(status_code=400,
                            detail="Это не похоже на ссылку. Нужен адрес, начинающийся с http.")
    info = (body or {}).get("info")
    if not isinstance(info, dict) or not info.get("title"):
        loop = asyncio.get_running_loop()
        try:
            info = await loop.run_in_executor(None, fetch.probe, url)
        except fetch.FetchError as err:
            raise HTTPException(status_code=400, detail=str(err))
    return JSONResponse(media.submit_link(url, info, _media_opts(body or {})))


@router.post("/api/media/upload")
async def api_media_upload(file: UploadFile, opts: str = "") -> JSONResponse:
    """Готовый файл: видео, аудио или сами субтитры (.vtt/.srt)."""
    from .. import media, subs

    no_recording_now()
    name = Path(file.filename or "файл").name
    if not (audio_io.is_media(name) or subs.is_transcript_name(name)):
        raise HTTPException(
            status_code=400,
            detail="Такой формат не поддерживается: %s" % Path(name).suffix)
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    tmp = UPLOAD_DIR / ("%s_%s" % (uuid.uuid4().hex[:8], name))
    size = 0
    with open(tmp, "wb") as fh:
        while True:
            chunk = await file.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            fh.write(chunk)
    await file.close()
    if size == 0:
        tmp.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail="Файл пустой")
    try:
        parsed = json.loads(opts) if opts else {}
    except ValueError:
        parsed = {}
    if not isinstance(parsed, dict):
        parsed = {}
    return JSONResponse(media.submit_file(tmp, name, _media_opts(parsed)))


@router.post("/api/media/{rec_id}/summary")
async def api_media_summary(rec_id: str, request: Request) -> JSONResponse:
    """Собрать саммари по уже готовой стенограмме.

    ``engine`` в теле — движок только для этой сборки: так кнопка «Собрать
    через облако» после лимита подписки не меняет общую настройку.
    """
    from .. import media

    body: dict[str, Any] = {}
    try:
        body = await request.json()
    except Exception:
        pass
    if not isinstance(body, dict):
        body = {}
    if store.get(rec_id) is None:
        raise HTTPException(status_code=404, detail="Запись не найдена")
    if jobs.busy_with("summary", rec_id):
        raise HTTPException(status_code=409, detail="Саммари уже собирается")
    engine = str(body.get("engine") or "").strip() or None
    if engine not in (None, "claude_cli", "api"):
        raise HTTPException(status_code=400, detail="Неизвестный движок: %s" % engine)
    document = str(body.get("document") or "").strip() or None
    return JSONResponse({"job_id": media.submit_summary(rec_id, document=document,
                                                        engine=engine)})


@router.post("/api/media/source")
async def api_media_source(request: Request) -> JSONResponse:
    """Сторонние площадки: GetCourse, SharePoint, сайт со входом по паролю."""
    from .. import media

    no_recording_now()
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Ожидался объект")
    kind = str(body.get("kind") or "").strip()
    if kind not in ("gcvh", "sharepoint", "webauth"):
        raise HTTPException(status_code=400, detail="Неизвестный источник: %s" % kind)
    payload = body.get("item")
    if not isinstance(payload, dict):
        payload = {"url": str(body.get("url") or "")}
    if kind != "sharepoint" and not str(payload.get("url") or "").strip():
        raise HTTPException(status_code=400, detail="Не указана ссылка")
    if kind == "webauth":
        require_part("browser")      # вход по паролю идёт через браузер playwright
        payload["login"] = str(body.get("login") or "")
        payload["password"] = str(body.get("password") or "")
    require_media_parts(body)
    try:
        return JSONResponse(media.submit_source(kind, payload, _media_opts(body)))
    except RuntimeError as err:
        raise HTTPException(status_code=400, detail=str(err))


_sp_login_thread: threading.Thread | None = None


@router.post("/api/sharepoint/login")
async def api_sharepoint_login() -> JSONResponse:
    """Начать вход в Microsoft по коду. Ссылку и код показываем в интерфейсе.

    Ожидание подтверждения идёт СВОИМ потоком, а не задачей в очереди: очередь
    однопоточная, и пятнадцать минут ожидания кода держали бы разметку и
    протоколы — а если очередь занята, сам вход не дождался бы своей очереди и
    код бы протух. Политика организации требует вход по коду при каждом запуске,
    поэтому вход живёт только в памяти службы.
    """
    global _sp_login_thread
    from ..sources import sharepoint

    loop = asyncio.get_running_loop()
    try:
        started = await loop.run_in_executor(None, sharepoint.begin_login)
    except Exception as err:
        log.warning("вход в Microsoft не начался: %s", err)
        raise HTTPException(status_code=502, detail=str(err))

    def wait() -> None:
        try:
            ok = sharepoint.wait_login()
            text = ("Вход в Microsoft выполнен — можно искать записи." if ok
                    else "Код не подтвердили вовремя — запросите новый.")
            hub.publish({"type": "sp_login", "state": "ok" if ok else "expired", "text": text})
            hub.publish({"type": "notice", "level": "ok" if ok else "err", "text": text})
        except Exception as err:
            hub.publish({"type": "sp_login", "state": "error", "text": str(err)})
            hub.publish({"type": "notice", "level": "err", "text": str(err)})

    if _sp_login_thread is None or not _sp_login_thread.is_alive():
        _sp_login_thread = threading.Thread(target=wait, name="sp-login", daemon=True)
        _sp_login_thread.start()
    hub.publish({"type": "sp_login", "state": "waiting", "login": started})
    return JSONResponse({"login": started})


@router.get("/api/sharepoint/status")
async def api_sharepoint_status() -> JSONResponse:
    from ..sources import sharepoint

    return JSONResponse({"logged_in": bool(sharepoint.logged_in()),
                         "pending": sharepoint.pending_login()})


def _sp_processed() -> dict[str, dict[str, Any]]:
    """Какие файлы SharePoint уже обрабатывались: {id файла: запись}."""
    out: dict[str, dict[str, Any]] = {}
    for meta in store.list_all():
        iid = str(meta.get("sp_item_id") or "")
        if not iid:
            continue
        # «✓ уже есть» — только если текст правда получен. Упавшая попытка
        # (в списке «остановлено») помечается отдельно и снова выбирается.
        ok = bool((meta.get("stages") or {}).get("text")) and meta.get("status") not in (
            "queued", "processing", "stopped", "error")
        prev = out.get(iid)
        if prev is None or (ok and not prev.get("ok")):
            out[iid] = {"rec_id": meta.get("id"), "title": meta.get("title"),
                        "status": meta.get("status"), "ok": ok}
    return out


def _sp_legacy_titles() -> dict[str, dict[str, Any]]:
    """Старые записи SharePoint без номера файла: узнаём по названию."""
    out: dict[str, dict[str, Any]] = {}
    for meta in store.list_all():
        if meta.get("site") == "sharepoint" and not meta.get("sp_item_id"):
            out[str(meta.get("title") or "").strip().lower()] = {
                "rec_id": meta.get("id"), "title": meta.get("title"), "status": meta.get("status"),
                "ok": bool((meta.get("stages") or {}).get("text"))}
    return out


@router.post("/api/sharepoint/search")
async def api_sharepoint_search(request: Request) -> JSONResponse:
    """Поиск записей: ключевые слова, год, месяц с/по, «только записи встреч»."""
    from ..sources import sharepoint

    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Ожидался объект")
    year = body.get("year")
    months = body.get("months")
    if not isinstance(months, list):
        try:
            m1 = int(body.get("month_from") or 1)
            m2 = int(body.get("month_to") or 12)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="Месяц указан неверно") from None
        m1, m2 = max(1, min(12, m1)), max(1, min(12, m2))
        if m2 < m1:
            m1, m2 = m2, m1
        months = list(range(m1, m2 + 1))
    meetings_only = body.get("meetings_only", True) is not False
    loop = asyncio.get_running_loop()
    try:
        res = await loop.run_in_executor(
            None, lambda: sharepoint.search_ex(
                str(body.get("query") or ""), int(year) if year else None,
                [int(m) for m in months], meetings_only=meetings_only))
    except RuntimeError as err:
        log.info("поиск в SharePoint: %s", err)
        raise HTTPException(status_code=502, detail=str(err))
    except Exception as err:
        log.warning("поиск в SharePoint не удался: %s", err)
        raise HTTPException(status_code=502, detail="Поиск не удался.")
    done = _sp_processed()
    legacy = _sp_legacy_titles()
    from pathlib import PurePath

    for it in res["items"]:
        hit = done.get(str(it.get("id"))) or legacy.get(PurePath(str(it.get("name") or "")).stem.strip().lower())
        it["processed"] = hit
    return JSONResponse(res)


@router.post("/api/sharepoint/transcripts")
async def api_sharepoint_transcripts(request: Request) -> JSONResponse:
    """Пометка 📝: есть ли у найденных записей расшифровка Teams (Stream)."""
    from ..sources import sharepoint

    body = await request.json()
    items = body.get("items") if isinstance(body, dict) else None
    if not isinstance(items, list):
        raise HTTPException(status_code=400, detail="Ожидался список записей")
    loop = asyncio.get_running_loop()
    try:
        flags = await loop.run_in_executor(None, sharepoint.check_transcripts,
                                           [i for i in items if isinstance(i, dict)])
    except RuntimeError as err:
        raise HTTPException(status_code=502, detail=str(err))
    return JSONResponse({"transcripts": flags})


_sp_batches: dict[str, list[str]] = {}


@router.post("/api/sharepoint/batch")
async def api_sharepoint_batch(request: Request) -> JSONResponse:
    """Обработать выбранные записи пачкой — все по настройкам формы.

    Каждая запись — своя задача в общей очереди: они идут по одной, а
    «Остановить» снимает всю пачку разом.
    """
    import uuid as _uuid

    from .. import media

    no_recording_now()
    body = await request.json()
    items = body.get("items") if isinstance(body, dict) else None
    if not isinstance(items, list) or not items:
        raise HTTPException(status_code=400, detail="Не выбрано ни одной записи")
    batch = _uuid.uuid4().hex[:10]
    opts = _media_opts(body)
    started: list[dict[str, Any]] = []
    failed: list[str] = []
    for it in items[:200]:
        if not isinstance(it, dict) or not it.get("id"):
            continue
        try:
            res = media.submit_source("sharepoint", it, opts, job_extra={"batch": batch})
            started.append({"job_id": res["job_id"], "rec_id": res["rec_id"],
                            "name": it.get("name")})
        except Exception as err:
            failed.append("%s: %s" % (it.get("name"), err))
    _sp_batches[batch] = [s["job_id"] for s in started]
    log.info("SharePoint: пачка %s — записей %d", batch, len(started))
    return JSONResponse({"batch": batch, "started": started, "failed": failed})


@router.post("/api/sharepoint/batch/{batch}/cancel")
async def api_sharepoint_batch_cancel(batch: str) -> JSONResponse:
    """«Остановить»: снять все ещё не сделанные задачи пачки."""
    ids = _sp_batches.get(batch)
    if ids is None:
        raise HTTPException(status_code=404, detail="Такой пачки нет")
    stopped = sum(1 for jid in ids if jobs.cancel(jid))
    log.info("SharePoint: пачка %s остановлена, снято задач %d", batch, stopped)
    return JSONResponse({"stopped": stopped})


# ====================================================================== протокол
