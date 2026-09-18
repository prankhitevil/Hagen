# -*- coding: utf-8 -*-
"""Маршруты говорящих и голосов записи: разметка, подписи, разделение.

Перенесено из `server.py` без изменений
вместе со своими помощниками: очередь разметки и разделения голосов, память о
числе голосов, переиспользование готовой разметки. Их зовёт и остальной
`server.py` — там оставлены прежние имена.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from .. import audio_io, config, jobs, platform, store
from ..events import hub
from .deps import refresh_note, refresh_note_async

log = logging.getLogger("hagen.server")

def _sessions():
    """Живые записи. Поздний импорт: server тянет этот модуль, не наоборот."""
    from ..server import sessions

    return sessions


router = APIRouter()


def queue_diarize(rec_id: str, min_speakers=None, max_speakers=None) -> str:
    if jobs.busy_with("diarize", rec_id):
        raise HTTPException(status_code=409, detail="Разметка уже выполняется")
    meta = store.get(rec_id)
    if meta is None:
        raise HTTPException(status_code=404, detail="Запись не найдена")

    tracks = store.existing_tracks(rec_id)
    # диаризуем дорожку собеседников; если её нет (очная встреча) — микрофон
    target = store.TRACK_FAR if store.TRACK_FAR in tracks else (
        store.TRACK_FILE if store.TRACK_FILE in tracks else store.TRACK_MIC)
    if target not in tracks:
        raise HTTPException(status_code=400, detail="Нет звука для разметки")

    if max_speakers is None:
        max_speakers = config.get("max_speakers")
        meeting = meta.get("meeting")
        if max_speakers is None and meeting:
            try:
                max_speakers = platform.desktop().suggest_max_speakers(meeting)
            except Exception:
                pass

    # Имена уже пришли из расшифровки (Teams): реплики не переподписываем, а
    # учим по звуку голоса этих людей (решение 13.09).
    names_mode = meta.get("transcript_source") == "subs" and any(
        isinstance(i, dict) and i.get("name") for i in (meta.get("speakers") or {}).values())

    def work(handle) -> dict[str, Any]:
        from .. import diarize, voices
        from .. import speakers as spk

        handle.log("читаю дорожку %s" % target)
        pcm, sr = audio_io.read_wav(store.track_path(rec_id, target))
        store.update(rec_id, {"diarize_status": "running", "diarize_progress": 0.0})
        hub.publish({"type": "recording", "meta": store.get(rec_id)})

        est = diarize.estimate_seconds(pcm)
        handle.eta(est)
        handle.log("примерная оценка: %.0f мин" % (est / 60.0))

        result = diarize.diarize_pcm(
            pcm, sr=sr, min_speakers=min_speakers, max_speakers=max_speakers,
            handle=handle,
        )
        diarize.save_result(rec_id, result)

        if names_mode:
            spk.link_names(rec_id)
            report = spk.learn_from_names(rec_id, result)["report"]
            store.update(rec_id, {"diarized": True, "diarize_status": "done", "diarize_progress": 1.0,
                                  "diarize_eta_s": None, "diarize_rtf": result.get("rtf")})
            added = [r["name"] for r in report if r["status"] == "added"]
            multi = [r["name"] for r in report if r["status"] == "multi"]
            parts = ["выучено голосов: %d" % len(added)]
            if multi:
                parts.append("несколько голосов под именем %s — можно разделить"
                             % ", ".join("«%s»" % n for n in multi))
            meta_now = store.get(rec_id)
            hub.publish({"type": "recording", "meta": meta_now})
            hub.publish({"type": "notice", "level": "ok",
                         "text": "Голоса из расшифровки «%s»: %s" % ((meta_now or {}).get("title", ""),
                                                                    "; ".join(parts))})
            refresh_note(rec_id, "разметка голосов")
            return {"learned": len(added), "multi": len(multi)}

        turns = result.get("exclusive_turns") or result.get("turns") or []
        # Здесь список идёт на ПЕРЕЗАПИСЬ стенограммы, поэтому берём и скрытые
        # эхом реплики: иначе разметка говорящих их физически вытрет.
        segs = store.sorted_segments(rec_id, include_echo=True)
        new_segs = diarize.relabel(segs, turns, track=target)

        embeddings = result.get("embeddings") or {}
        speakers = {}
        if embeddings:
            try:
                speakers = voices.apply_to_meta(rec_id, embeddings)
            except Exception as err:
                handle.log("база голосов не отработала: %s" % err)

        # подставляем имена из базы голосов и нумеруем остальных по-русски
        order = {}
        for seg in new_segs:
            key = seg.get("speaker_key")
            if seg.get("track") != target or not key or key in ("me", "far"):
                continue
            if key not in order:
                order[key] = len(order) + 1
            info = speakers.get(key) or {}
            name = info.get("name")
            if name:
                seg["speaker"] = name
            else:
                seg["speaker"] = "Спикер %d" % order[key]
            sug = info.get("suggestion")
            seg["suggestion"] = sug if (sug and not name) else None

        store.replace_segments(rec_id, new_segs)
        # говорящие, которых после новой разметки нет ни в одной реплике (например,
        # голоса прежнего разделения), из meta убираем — иначе висели бы призраками
        present = {s.get("speaker_key") for s in new_segs}
        speakers = {k: v for k, v in speakers.items() if k in present}
        splits = {k: v for k, v in ((store.get(rec_id) or {}).get("splits") or {}).items()
                  if k == "me" or k in present or any(x in present for x in v.get("keys") or [])}
        store.update(rec_id, {
            "diarized": True,
            "diarize_status": "done",
            "diarize_progress": 1.0,
            "diarize_eta_s": None,
            "speakers": speakers,
            "splits": splits,
            "diarize_rtf": result.get("rtf"),
        })
        store.refresh_participants(rec_id)
        # Теперь, когда голоса известны, эхо колонок видно и без сверки текстов:
        # реплика микрофона, похожая не на владельца, а на собеседника (17.09).
        try:
            from .. import echo as echo_mod

            by_voice = echo_mod.mark_by_voice(rec_id)
            if by_voice:
                handle.log("эхо колонок по голосу: %d реплик" % by_voice)
        except Exception as err:                       # noqa: BLE001
            handle.log("отсев эха по голосу не отработал: %s" % err)
        meta_now = store.get(rec_id)
        hub.publish({"type": "recording", "meta": meta_now})
        hub.publish({"type": "segments", "rec_id": rec_id,
                     "segments": store.sorted_segments(rec_id)})
        hub.publish({"type": "notice", "level": "ok",
                     "text": "Говорящие размечены: «%s»" % (meta_now or {}).get("title", "")})
        refresh_note(rec_id, "разметка голосов")
        return {
            "speakers": len(result.get("labels") or []),
            "turns": len(turns),
            "rtf": result.get("rtf"),
            "elapsed_s": result.get("elapsed_s"),
        }

    title = "Разметка говорящих: %s" % meta.get("title")
    job_id = jobs.submit("diarize", work, title, rec_id=rec_id)
    store.update(rec_id, {"diarize_status": "queued"})
    hub.publish({"type": "recording", "meta": store.get(rec_id)})
    return job_id


@router.post("/api/recordings/{rec_id}/diarize")
async def api_diarize(rec_id: str, request: Request) -> JSONResponse:
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    job_id = queue_diarize(rec_id, body.get("min_speakers"), body.get("max_speakers"))
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
        raise HTTPException(status_code=400, detail="Не указан говорящий")
    remember = bool(body.get("remember", True))
    loop = asyncio.get_running_loop()
    try:
        res = await loop.run_in_executor(
            None, lambda: speakers.apply(rec_id, key, name, decision, remember=remember))
    except speakers.Conflict as err:
        return JSONResponse({"conflict": err.plan["conflict"], "target": err.plan["target"],
                             "notes": err.plan["notes"]}, status_code=409)
    except LookupError as err:
        raise HTTPException(status_code=404, detail=str(err))
    except ValueError as err:
        raise HTTPException(status_code=400, detail=str(err))
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


def remember_voices(rec_id: str, **patch: int) -> None:
    """Запомнить названное число голосов: окно подсветит его в следующий раз."""
    meta = store.get(rec_id) or {}
    hint = dict(meta.get("voices_hint") or {})
    hint.update({k: int(v) for k, v in patch.items() if v})
    if hint != (meta.get("voices_hint") or {}):
        store.update(rec_id, {"voices_hint": hint})


def queue_split(rec_id: str, key: str, want_voices: int = 0) -> str:
    """«Разделить голоса» говорящего — задачей в очереди."""
    from .. import speakers

    meta = store.get(rec_id)
    if meta is None:
        raise HTTPException(status_code=404, detail="Запись не найдена")
    if not speakers.segments_of(rec_id, key):
        raise HTTPException(status_code=404, detail="Такого говорящего в записи нет")
    track = speakers.track_of(rec_id, key) or ""
    if not store.track_path(rec_id, track).exists():
        raise HTTPException(status_code=400,
                            detail="Звук этой записи не сохранён — разделить голоса нельзя.")
    if jobs.busy_with("split", rec_id):
        raise HTTPException(status_code=409, detail="Голоса этой записи уже разделяются")
    title = speakers.display_name(meta, key)
    # Имя не «speakers»: внутри этой функции так зовётся модуль говорящих.
    speakers_want = int(want_voices or 0)

    def work(handle) -> dict[str, Any]:
        res = speakers.split_speaker(rec_id, key, handle=handle, speakers=speakers_want)
        drop_media_if_done(rec_id)
        meta_now = store.get(rec_id)
        hub.publish({"type": "recording", "meta": meta_now})
        hub.publish({"type": "segments", "rec_id": rec_id, "segments": store.sorted_segments(rec_id)})
        # Один чужой голос в «моих» репликах — тоже разделение: «Я» оттуда ушло.
        text = ("«%s»: голосов %d — подпишите их" % (title, res["voices"]) if res.get("keys")
                else "«%s»: нашёлся один голос, делить нечего" % title)
        hub.publish({"type": "notice", "level": "ok", "text": text})
        refresh_note(rec_id, "разделение голосов")
        return {"voices": res["voices"]}

    return jobs.submit("split", work, "Разделить голоса: %s" % title, rec_id=rec_id)


def drop_media_if_done(rec_id: str) -> None:
    """Хранить звук не просили — убрать, когда он больше не нужен ни разметке, ни разделению."""
    from .. import speakers

    meta = store.get(rec_id) or {}
    if meta.get("store_media") != "none" or meta.get("media_removed"):
        return
    if speakers.pending_split(meta) or meta.get("diarize_status") in ("queued", "running"):
        return
    res = store.drop_media(rec_id)
    log.info("звук записи %s убран: %.1f МБ", rec_id, res.get("freed_bytes", 0) / 1048576.0)


@router.post("/api/recordings/{rec_id}/speaker/split")
async def api_speaker_split(rec_id: str, request: Request) -> JSONResponse:
    body = await request.json()
    key = str(body.get("speaker_key") or "").strip()
    return JSONResponse({"job_id": queue_split(rec_id, key)})


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
    drop_media_if_done(rec_id)
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
    if _sessions().get(rec_id) is not None and (_sessions().get(rec_id).active):
        raise HTTPException(status_code=409, detail="Идёт запись — разделю голоса после «Стоп».")
    if body.get("on"):
        if not speakers.segments_of(rec_id, "me"):
            raise HTTPException(status_code=400, detail="В вашей дорожке пока нет реплик")
        # «speakers» — сколько человек говорило рядом со мной, считая меня
        # (решение 14.09: разделение начинается только по кнопке,
        # и число можно назвать в том же окне).
        want = voices_asked(body)
        job_id = queue_split(rec_id, "me", want)
        remember_voices(rec_id, mine=want)
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
    except LookupError as err:
        raise HTTPException(status_code=404, detail=str(err))
    except ValueError as err:
        raise HTTPException(status_code=400, detail=str(err))
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
        raise HTTPException(status_code=400, detail="Не указан говорящий")
    try:
        res = speakers.add_offered_sample(rec_id, key)
    except LookupError as err:
        raise HTTPException(status_code=404, detail=str(err))
    except ValueError as err:
        meta = store.get(rec_id)
        if meta is not None:
            hub.publish({"type": "recording", "meta": meta})
        raise HTTPException(status_code=400, detail=str(err))
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
    if _sessions().get(rec_id) is not None and (_sessions().get(rec_id).active):
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
    job_id = queue_split(rec_id, "me")
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


# ====================================================================== переразбор


def reuse_diarization(rec_id: str, handle=None) -> bool:
    """Применить СОХРАНЁННУЮ разметку голосов к новым репликам.

    Зовётся после переразбора текста. Возвращает True, если разметку удалось
    переиспользовать, и False — если её нет или она уже не про этот звук.
    """
    from .. import diarize

    def say(msg: str) -> None:
        if handle is not None:
            try:
                handle.log(msg)
            except Exception:
                pass

    result = diarize.load_result(rec_id)
    if not result:
        return False
    turns = result.get("exclusive_turns") or result.get("turns") or []
    if not turns:
        return False

    # Сверяем длительность: если запись дописали или заменили, старая разметка
    # к новому звуку не относится, и переиспользовать её нельзя.
    tracks = store.existing_tracks(rec_id)
    target = store.TRACK_FAR if store.TRACK_FAR in tracks else (
        store.TRACK_FILE if store.TRACK_FILE in tracks else store.TRACK_MIC)
    if target not in tracks:
        return False
    try:
        import wave

        with wave.open(str(store.track_path(rec_id, target)), "rb") as wf:
            now_s = wf.getnframes() / float(wf.getframerate())
    except Exception:
        return False
    was_s = float(result.get("duration_s") or 0.0)
    if was_s <= 0 or abs(now_s - was_s) > 1.0:
        say("звук изменился с прошлой разметки, размечу заново")
        return False

    segs = store.sorted_segments(rec_id, include_echo=True)
    new_segs = diarize.relabel(segs, turns, track=target)

    # Имена и подсказки берём из того, что уже было подтверждено человеком.
    meta = store.get(rec_id) or {}
    speakers = dict(meta.get("speakers") or {})
    order: dict[str, int] = {}
    for seg in new_segs:
        key = seg.get("speaker_key")
        if seg.get("track") != target or not key or key in ("me", "far"):
            continue
        if key not in order:
            order[key] = len(order) + 1
        info = speakers.get(key) or {}
        name = info.get("name")
        seg["speaker"] = name or ("Спикер %d" % order[key])
        sug = info.get("suggestion")
        seg["suggestion"] = sug if (sug and not name) else None

    store.replace_segments(rec_id, new_segs)
    store.update(rec_id, {"diarized": True, "diarize_status": "done",
                          "diarize_progress": 1.0, "diarize_eta_s": None})
    store.refresh_participants(rec_id)
    say("разметка голосов взята готовой, заново не считаю")
    log.info("запись %s: разметка переиспользована, интервалов %d", rec_id, len(turns))
    return True
