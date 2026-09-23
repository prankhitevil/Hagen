# -*- coding: utf-8 -*-
"""Маршруты говорящих и голосов записи: разметка, подписи, разделение.

Сама работа — в ядре: постановка разметки и разделения в очередь и
переиспользование готовой разметки живут в `diarize_jobs`, подписи и
разделение — в `speakers`. Здесь маршруты только принимают запрос, зовут ядро
и переводят его ошибки в коды ответов.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from .. import diarize_jobs, jobs, recordings, store
from ..events import hub
from .deps import as_http, refresh_note_async

log = logging.getLogger("hagen.server")

router = APIRouter()

CORE_ERRORS = (LookupError, ValueError, jobs.Busy)


@router.post("/api/recordings/{rec_id}/diarize")
async def api_diarize(rec_id: str, request: Request) -> JSONResponse:
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    try:
        job_id = diarize_jobs.queue_diarize(rec_id, body.get("min_speakers"), body.get("max_speakers"))
    except CORE_ERRORS as err:
        raise as_http(err) from None
    return JSONResponse({"job_id": job_id})


@router.post("/api/recordings/{rec_id}/speaker")
async def api_speaker(rec_id: str, request: Request) -> JSONResponse:
    """Подписать говорящего. Нужен выбор человека — 409 с вопросом и кнопками.

    decision — ответы на прошлые вопросы: person_id (выбран из списка),
    alias_of, new_person, namesake, merge, keep_both, me, voice (add/keep/no/use:<id>).
    """
    from .. import speakers

    body = await request.json()
    key = str(body.get("speaker_key") or "").strip()
    name = str(body.get("name") or "").strip()
    decision = body.get("decision") if isinstance(body.get("decision"), dict) else {}
    if not key:
        raise HTTPException(status_code=400, detail="Не указан голос")
    remember = bool(body.get("remember", True))
    loop = asyncio.get_running_loop()
    try:
        res = await loop.run_in_executor(
            None, lambda: speakers.apply(rec_id, key, name, decision, remember=remember))
    except speakers.Conflict as err:
        return JSONResponse({"conflict": err.plan["conflict"], "target": err.plan["target"],
                             "notes": err.plan["notes"]}, status_code=409)
    except (LookupError, ValueError) as err:
        raise as_http(err) from None
    meta = store.get(rec_id)
    segs = store.sorted_segments(rec_id)
    hub.publish({"type": "recording", "meta": meta})
    hub.publish({"type": "segments", "rec_id": rec_id, "segments": segs})
    await refresh_note_async(rec_id, "подпись говорящего")
    return JSONResponse(dict(res, meta=store.get(rec_id), segments=segs))


@router.get("/api/recordings/{rec_id}/speaker/search")
async def api_speaker_search(rec_id: str, key: str = "", q: str = "") -> JSONResponse:
    """Подсказки для поля имени: люди из базы, похожесть голоса этого говорящего."""
    from .. import speakers

    if store.get(rec_id) is None:
        raise HTTPException(status_code=404, detail="Запись не найдена")
    return JSONResponse(speakers.search_for(rec_id, key, q))


def voices_asked(body: dict[str, Any] | None) -> int:
    """Сколько голосов назвал человек в окне. 0 — «определить самой»."""
    try:
        want = int((body or {}).get("speakers") or 0)
    except (TypeError, ValueError):
        return 0
    return want if 1 <= want <= 10 else 0


@router.post("/api/recordings/{rec_id}/speaker/split")
async def api_speaker_split(rec_id: str, request: Request) -> JSONResponse:
    body = await request.json()
    key = str(body.get("speaker_key") or "").strip()
    try:
        return JSONResponse({"job_id": diarize_jobs.queue_split(rec_id, key)})
    except CORE_ERRORS as err:
        raise as_http(err) from None


@router.post("/api/recordings/{rec_id}/speaker/unsplit")
async def api_speaker_unsplit(rec_id: str, request: Request) -> JSONResponse:
    from .. import speakers

    body = await request.json()
    key = str(body.get("speaker_key") or "").strip()
    if store.get(rec_id) is None:
        raise HTTPException(status_code=404, detail="Запись не найдена")
    changed = speakers.unsplit(rec_id, key)
    meta = store.get(rec_id)
    segs = store.sorted_segments(rec_id)
    hub.publish({"type": "recording", "meta": meta})
    hub.publish({"type": "segments", "rec_id": rec_id, "segments": segs})
    await refresh_note_async(rec_id, "отмена разделения голосов")
    return JSONResponse({"changed": changed, "meta": store.get(rec_id), "segments": segs})


@router.post("/api/recordings/{rec_id}/speaker/one-person")
async def api_speaker_one_person(rec_id: str, request: Request) -> JSONResponse:
    """«Это один человек» — не предлагать разделение голосов этого имени."""
    from .. import speakers

    body = await request.json()
    key = str(body.get("speaker_key") or "").strip()
    if store.get(rec_id) is None:
        raise HTTPException(status_code=404, detail="Запись не найдена")
    speakers.dismiss_multi(rec_id, key)
    diarize_jobs.drop_media_if_done(rec_id)
    meta = store.get(rec_id)
    hub.publish({"type": "recording", "meta": meta})
    return JSONResponse({"meta": meta})


@router.post("/api/recordings/{rec_id}/room")
async def api_recording_room(rec_id: str, request: Request) -> JSONResponse:
    """«Со мной в комнате были ещё люди»: разделить голоса моей дорожки или отменить."""
    from .. import speakers

    body = await request.json()
    meta = store.get(rec_id)
    if meta is None:
        raise HTTPException(status_code=404, detail="Запись не найдена")
    if recordings.is_active(rec_id):
        raise HTTPException(status_code=409, detail="Идёт запись — разделю голоса после «Стоп».")
    if body.get("on"):
        if not speakers.segments_of(rec_id, "me"):
            raise HTTPException(status_code=400, detail="В вашей дорожке пока нет реплик")
        # «speakers» — сколько человек говорило рядом со мной, считая меня
        # (решение 14.09: разделение начинается только по кнопке,
        # и число можно назвать в том же окне).
        want = voices_asked(body)
        try:
            job_id = diarize_jobs.queue_split(rec_id, "me", want)
        except CORE_ERRORS as err:
            raise as_http(err) from None
        diarize_jobs.remember_voices(rec_id, mine=want)
        store.update(rec_id, {"room_shared": True})
        hub.publish({"type": "recording", "meta": store.get(rec_id)})
        return JSONResponse({"job_id": job_id})
    speakers.unsplit(rec_id, "me")
    meta = store.get(rec_id)
    hub.publish({"type": "recording", "meta": meta})
    hub.publish({"type": "segments", "rec_id": rec_id, "segments": store.sorted_segments(rec_id)})
    await refresh_note_async(rec_id, "со мной в комнате — снято")
    return JSONResponse({"meta": store.get(rec_id)})


@router.post("/api/recordings/{rec_id}/owner_voice")
async def api_recording_owner_voice(rec_id: str) -> JSONResponse:
    """«Да, это мой голос — запомнить»: подтвердить угаданный голос владельца (15.09)."""
    from .. import speakers

    try:
        res = speakers.confirm_owner_voice(rec_id)
    except (LookupError, ValueError) as err:
        raise as_http(err) from None
    meta = store.get(rec_id)
    hub.publish({"type": "recording", "meta": meta})
    return JSONResponse(dict(res, meta=meta))


@router.post("/api/recordings/{rec_id}/speaker/add_sample")
async def api_speaker_add_sample(rec_id: str, request: Request) -> JSONResponse:
    """«Добавить образец» узнанному по голосу, но неуверенно человеку (15.09)."""
    from .. import speakers

    body = await request.json()
    key = str(body.get("speaker_key") or "").strip()
    if not key:
        raise HTTPException(status_code=400, detail="Не указан голос")
    try:
        res = speakers.add_offered_sample(rec_id, key)
    except LookupError as err:
        raise as_http(err) from None
    except ValueError as err:
        meta = store.get(rec_id)
        if meta is not None:
            hub.publish({"type": "recording", "meta": meta})
        raise as_http(err) from None
    meta = store.get(rec_id)
    hub.publish({"type": "recording", "meta": meta})
    return JSONResponse(dict(res, meta=meta))


@router.post("/api/recordings/{rec_id}/owner_absent")
async def api_recording_owner_absent(rec_id: str, request: Request) -> JSONResponse:
    """«Меня в этой записи не было»: реплики микрофона — не «Я» (решение 15.09).

    Включение разделяет голоса дорожки заново, не ставя «Я» никому; выключение
    возвращает все реплики микрофона владельцу.
    """
    from .. import speakers

    body = await request.json()
    meta = store.get(rec_id)
    if meta is None:
        raise HTTPException(status_code=404, detail="Запись не найдена")
    if recordings.is_active(rec_id):
        raise HTTPException(status_code=409, detail="Идёт запись — отмечу после «Стоп».")
    if not body.get("on"):
        speakers.unsplit(rec_id, "me")
        meta = store.get(rec_id)
        hub.publish({"type": "recording", "meta": meta})
        hub.publish({"type": "segments", "rec_id": rec_id, "segments": store.sorted_segments(rec_id)})
        await refresh_note_async(rec_id, "меня не было — снято")
        return JSONResponse({"meta": store.get(rec_id)})
    if (meta.get("splits") or {}).get("me"):
        speakers.unsplit(rec_id, "me")        # делим заново, уже без «Я»
    if not speakers.segments_of(rec_id, "me"):
        raise HTTPException(status_code=400, detail="В дорожке микрофона нет реплик")
    store.update(rec_id, {"owner_absent": True})
    try:
        job_id = diarize_jobs.queue_split(rec_id, "me")
    except CORE_ERRORS as err:
        raise as_http(err) from None
    hub.publish({"type": "recording", "meta": store.get(rec_id)})
    return JSONResponse({"job_id": job_id})


@router.post("/api/recordings/{rec_id}/speaker/reject")
async def api_speaker_reject(rec_id: str, request: Request) -> JSONResponse:
    """«Нет, это другой человек» — убрать подсказку, имя не подставлять."""
    body = await request.json()
    key = (body.get("speaker_key") or "").strip()
    if store.get(rec_id) is None:
        raise HTTPException(status_code=404, detail="Запись не найдена")
    segs = store.sorted_segments(rec_id, include_echo=True)   # список идёт на перезапись
    for seg in segs:
        if seg.get("speaker_key") == key:
            seg["suggestion"] = None
    store.replace_segments(rec_id, segs)
    meta = store.get(rec_id) or {}
    speakers = dict(meta.get("speakers") or {})
    if key in speakers:
        speakers[key] = dict(speakers[key])
        speakers[key]["suggestion"] = None
        store.update(rec_id, {"speakers": speakers})
    hub.publish({"type": "segments", "rec_id": rec_id, "segments": segs})
    await refresh_note_async(rec_id, "подсказка имени отклонена")
    return JSONResponse({"ok": True, "segments": segs})
