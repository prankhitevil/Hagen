# -*- coding: utf-8 -*-
"""Маршруты правки стенограммы и отсева эха.

Разрез реплики, передача слов соседу, передача выбранному говорящему, правка
текста, отмена и пересчёт эха. Перенесено из `server.py` без изменений:
адреса и ответы те же.
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from .. import config, jobs, store
from ..events import hub
from .deps import plural_ru, refresh_note_async

log = logging.getLogger("hagen.server")

router = APIRouter()


@router.post("/api/recordings/{rec_id}/transcript/split")
async def api_transcript_split(rec_id: str, request: Request) -> JSONResponse:
    """Разрезать реплику по границе слова — режим правки стенограммы (16.09)."""
    from .. import edits

    body = await request.json()
    seg_id = str(body.get("segment_id") or "").strip()
    if store.get(rec_id) is None:
        raise HTTPException(status_code=404, detail="Запись не найдена")
    try:
        res = edits.split_segment(rec_id, seg_id, int(body.get("word_index") or 0))
    except edits.EditError as err:
        raise HTTPException(status_code=400, detail=str(err))
    segs = store.sorted_segments(rec_id)
    hub.publish({"type": "recording", "meta": store.get(rec_id)})
    hub.publish({"type": "segments", "rec_id": rec_id, "segments": segs})
    await refresh_note_async(rec_id, "правка стенограммы")
    return JSONResponse({"exact": res["exact"], "segments": segs, "meta": store.get(rec_id)})


@router.post("/api/recordings/{rec_id}/transcript/text")
async def api_transcript_text(rec_id: str, request: Request) -> JSONResponse:
    """Поправить текст реплики руками — режим правки стенограммы."""
    from .. import edits

    body = await request.json()
    if store.get(rec_id) is None:
        raise HTTPException(status_code=404, detail="Запись не найдена")
    try:
        res = edits.edit_text(rec_id, str(body.get("segment_id") or "").strip(),
                              str(body.get("text") or ""))
    except edits.EditError as err:
        raise HTTPException(status_code=400, detail=str(err))
    segs = store.sorted_segments(rec_id)
    if res["changed"]:
        hub.publish({"type": "recording", "meta": store.get(rec_id)})
        hub.publish({"type": "segments", "rec_id": rec_id, "segments": segs})
        await refresh_note_async(rec_id, "правка стенограммы")
    return JSONResponse({"changed": res["changed"], "kept_times": res["kept_times"],
                         "segments": segs, "meta": store.get(rec_id)})


@router.get("/api/recordings/{rec_id}/transcript/speakers")
async def api_transcript_speakers(rec_id: str) -> JSONResponse:
    """Кто говорит в записи — список для выбора «кому отдать слова»."""
    from .. import edits

    if store.get(rec_id) is None:
        raise HTTPException(status_code=404, detail="Запись не найдена")
    return JSONResponse({"speakers": edits.speakers_of(rec_id)})


@router.get("/api/recordings/{rec_id}/transcript/edits")
async def api_transcript_edits(rec_id: str) -> JSONResponse:
    """Сколько в записи ручной работы — предупредить перед «Перечитать точнее»."""
    from .. import edits

    if store.get(rec_id) is None:
        raise HTTPException(status_code=404, detail="Запись не найдена")
    return JSONResponse(edits.counts(rec_id))


@router.post("/api/recordings/{rec_id}/transcript/assign")
async def api_transcript_assign(rec_id: str, request: Request) -> JSONResponse:
    """Отдать выделенные слова выбранному говорящему записи."""
    from .. import edits

    body = await request.json()
    if store.get(rec_id) is None:
        raise HTTPException(status_code=404, detail="Запись не найдена")
    try:
        res = edits.assign_speaker(rec_id, str(body.get("segment_id") or "").strip(),
                                   int(body.get("first") or 0), int(body.get("last") or 0),
                                   str(body.get("speaker_key") or ""))
    except edits.EditError as err:
        raise HTTPException(status_code=400, detail=str(err))
    segs = store.sorted_segments(rec_id)
    hub.publish({"type": "recording", "meta": store.get(rec_id)})
    hub.publish({"type": "segments", "rec_id": rec_id, "segments": segs})
    await refresh_note_async(rec_id, "правка стенограммы")
    return JSONResponse({"exact": res["exact"], "to": res["to"], "segments": segs,
                         "meta": store.get(rec_id)})


@router.post("/api/recordings/{rec_id}/transcript/move")
async def api_transcript_move(rec_id: str, request: Request) -> JSONResponse:
    """Передать выделенные слова соседней реплике — выше (Alt+↑) или ниже (Alt+↓)."""
    from .. import edits

    body = await request.json()
    if store.get(rec_id) is None:
        raise HTTPException(status_code=404, detail="Запись не найдена")
    try:
        res = edits.move_words(rec_id, str(body.get("segment_id") or "").strip(),
                               int(body.get("first") or 0), int(body.get("last") or 0),
                               str(body.get("where") or ""))
    except edits.EditError as err:
        raise HTTPException(status_code=400, detail=str(err))
    segs = store.sorted_segments(rec_id)
    hub.publish({"type": "recording", "meta": store.get(rec_id)})
    hub.publish({"type": "segments", "rec_id": rec_id, "segments": segs})
    await refresh_note_async(rec_id, "правка стенограммы")
    return JSONResponse({"exact": res["exact"], "to": res["to"], "segments": segs,
                         "meta": store.get(rec_id)})


@router.get("/api/recordings/{rec_id}/transcript/undo")
async def api_transcript_undo_info(rec_id: str) -> JSONResponse:
    """Есть ли что отменять: страница показывает кнопку только когда есть."""
    from .. import edits

    if store.get(rec_id) is None:
        raise HTTPException(status_code=404, detail="Запись не найдена")
    return JSONResponse(edits.undo_info(rec_id))


@router.post("/api/recordings/{rec_id}/transcript/undo")
async def api_transcript_undo(rec_id: str) -> JSONResponse:
    """Отменить последнюю правку стенограммы — одну ступень назад."""
    from .. import edits

    if store.get(rec_id) is None:
        raise HTTPException(status_code=404, detail="Запись не найдена")
    try:
        res = edits.undo(rec_id)
    except edits.EditError as err:
        raise HTTPException(status_code=400, detail=str(err))
    segs = store.sorted_segments(rec_id)
    hub.publish({"type": "recording", "meta": store.get(rec_id)})
    hub.publish({"type": "segments", "rec_id": rec_id, "segments": segs})
    await refresh_note_async(rec_id, "отмена правки стенограммы")
    return JSONResponse({"what": res["what"], "steps_left": res["steps_left"],
                         "segments": segs, "meta": store.get(rec_id)})


@router.post("/api/recordings/{rec_id}/echo")
async def api_echo_recheck(rec_id: str) -> JSONResponse:
    """Пересчитать эхо колонок в готовой стенограмме.

    Раньше эхо пересчитывалось только вместе с пересборкой стенограммы: после
    «Стоп», после «Перечитать точнее» и после разметки говорящих. Значит, чтобы
    почистить старую запись, приходилось перечитывать её целиком — минуты
    работы процессора и потеря ручных правок текста (замечено 17.09). Сам
    пересчёт занимает доли секунды.

    Сравнение текстов идёт всегда, проверка по голосу — только если запись уже
    размечена: без разметки не с чем сравнивать голоса собеседников.
    """
    from .. import echo

    meta = store.get(rec_id)
    if meta is None:
        raise HTTPException(status_code=404, detail="Запись не найдена")
    if not echo.enabled():
        raise HTTPException(
            status_code=409,
            detail="Отсев эха выключен в настройках — включите его и повторите.")
    if jobs.busy_with("echo", rec_id):
        raise HTTPException(status_code=409, detail="Уже пересчитываю эхо")

    def work(handle) -> dict[str, Any]:
        handle.log("сверяю реплики микрофона с речью собеседников")
        by_text = echo.mark(rec_id)
        by_voice = 0
        if meta.get("diarized") and echo.voice_enabled():
            handle.log("сверяю голоса")
            try:
                by_voice = echo.mark_by_voice(rec_id)
            except Exception as err:                  # noqa: BLE001
                handle.log("по голосу не вышло: %s" % err)
        left = sum(1 for s in store.sorted_segments(rec_id, include_echo=True)
                   if s.get("echo"))
        hub.publish({"type": "segments", "rec_id": rec_id,
                     "segments": store.sorted_segments(rec_id)})
        total = by_text + by_voice
        if total:
            said = "Отсеяно эхо: %d %s" % (total, plural_ru(total, "реплика", "реплики", "реплик"))
            if by_voice:
                said += " (по голосу — %d)" % by_voice
        else:
            said = ("Новых двойников не нашлось" if left
                    else "Эха в этой записи не нашлось")
        hub.publish({"type": "notice", "level": "ok", "text": said})
        return {"by_text": by_text, "by_voice": by_voice, "hidden": left}

    job_id = jobs.submit("echo", work, "Пересчитать эхо: %s" % meta.get("title"),
                         rec_id=rec_id)
    return JSONResponse({"job_id": job_id})
