# -*- coding: utf-8 -*-
"""Разметка голосов как задачи очереди: поставить, применить итог, переиспользовать.

Движок разметки — `diarize.py`, база голосов — `voices.py`, подписи и
разделение говорящих — `speakers.py`. Здесь то, что их связывает в задачу:
какую дорожку размечать, что делать с итогом, когда готовую разметку можно
взять заново, а когда звук больше не нужен. Раньше это лежало в пакете
маршрутов (`api/speakers.py`), и остановка записи звала логику из маршрутов.

Ошибки — обычными исключениями: `LookupError`, `ValueError`, `jobs.Busy`;
в коды ответов их переводит `api/deps.as_http`.
"""
from __future__ import annotations

import logging
from typing import Any

from . import config, jobs, platform, store
from .events import hub
from .recordings import refresh_note

log = logging.getLogger("hagen.diarize_jobs")


def _target_track(rec_id: str) -> str | None:
    """Какую дорожку размечать: собеседников; нет её (очная встреча) — микрофон."""
    tracks = store.existing_tracks(rec_id)
    target = store.TRACK_FAR if store.TRACK_FAR in tracks else (
        store.TRACK_FILE if store.TRACK_FILE in tracks else store.TRACK_MIC)
    return target if target in tracks else None


def queue_diarize(rec_id: str, min_speakers: int | None = None,
                  max_speakers: int | None = None) -> str:
    """Поставить разметку голосов в очередь. Возвращает номер задачи."""
    if jobs.busy_with("diarize", rec_id):
        raise jobs.Busy("Разметка уже выполняется")
    meta = store.get(rec_id)
    if meta is None:
        raise LookupError("Запись не найдена")
    target = _target_track(rec_id)
    if target is None:
        raise ValueError("Нет звука для разметки")

    if max_speakers is None:
        max_speakers = config.get("max_speakers")
        meeting = meta.get("meeting")
        if max_speakers is None and meeting:
            try:
                max_speakers = platform.desktop().suggest_max_speakers(meeting)
            except Exception:
                log.debug("число голосов по встрече не подсказалось", exc_info=True)

    # Имена уже пришли из расшифровки (Teams): реплики не переподписываем, а
    # учим по звуку голоса этих людей (решение 13.09).
    names_mode = meta.get("transcript_source") == "subs" and any(
        isinstance(i, dict) and i.get("name") for i in (meta.get("speakers") or {}).values())

    from . import diarize as diarize_door

    # Где считать, решается при постановке в очередь: разметка в помощнике
    # начинается и во время записи — уступать ей нечего, приоритет у неё и так
    # низкий (решение 22.09).
    isolated = diarize_door.runs_in_helper()

    def work(handle) -> dict[str, Any]:
        from . import diarize, voices
        from . import speakers as spk

        handle.log("читаю дорожку %s" % target)
        store.update(rec_id, {"diarize_status": "running", "diarize_progress": 0.0})
        hub.publish({"type": "recording", "meta": store.get(rec_id)})

        result = diarize.diarize_track(
            store.track_path(rec_id, target), min_speakers=min_speakers,
            max_speakers=max_speakers, handle=handle, isolated=isolated,
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

        _name_segments(new_segs, speakers, target)

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
            from . import echo as echo_mod

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
                     "text": "Голоса размечены: «%s»" % (meta_now or {}).get("title", "")})
        refresh_note(rec_id, "разметка голосов")
        return {
            "speakers": len(result.get("labels") or []),
            "turns": len(turns),
            "rtf": result.get("rtf"),
            "elapsed_s": result.get("elapsed_s"),
        }

    title = "Разметка голосов: %s" % meta.get("title")
    job_id = jobs.submit("diarize", work, title, rec_id=rec_id,
                         extra={"where": "helper"} if isolated else None)
    store.update(rec_id, {"diarize_status": "queued"})
    hub.publish({"type": "recording", "meta": store.get(rec_id)})
    return job_id


def _name_segments(segs: list[dict[str, Any]], speakers: dict[str, Any], target: str) -> None:
    """Подставить имена из базы голосов и пронумеровать остальных по-русски."""
    order: dict[str, int] = {}
    for seg in segs:
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


def remember_voices(rec_id: str, **patch: int) -> None:
    """Запомнить названное число голосов: окно подсветит его в следующий раз."""
    meta = store.get(rec_id) or {}
    hint = dict(meta.get("voices_hint") or {})
    hint.update({k: int(v) for k, v in patch.items() if v})
    if hint != (meta.get("voices_hint") or {}):
        store.update(rec_id, {"voices_hint": hint})


def queue_split(rec_id: str, key: str, want_voices: int = 0) -> str:
    """«Разделить голоса» говорящего — задачей в очереди."""
    from . import speakers

    meta = store.get(rec_id)
    if meta is None:
        raise LookupError("Запись не найдена")
    if not speakers.segments_of(rec_id, key):
        raise LookupError("Такого голоса в записи нет")
    track = speakers.track_of(rec_id, key) or ""
    if not store.track_path(rec_id, track).exists():
        raise ValueError("Звук этой записи не сохранён — разделить голоса нельзя.")
    if jobs.busy_with("split", rec_id):
        raise jobs.Busy("Голоса этой записи уже разделяются")
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
    from . import speakers

    meta = store.get(rec_id) or {}
    if meta.get("store_media") != "none" or meta.get("media_removed"):
        return
    if speakers.pending_split(meta) or meta.get("diarize_status") in ("queued", "running"):
        return
    res = store.drop_media(rec_id)
    log.info("звук записи %s убран: %.1f МБ", rec_id, res.get("freed_bytes", 0) / 1048576.0)


def reuse_diarization(rec_id: str, handle: Any = None) -> bool:
    """Применить СОХРАНЁННУЮ разметку голосов к новым репликам.

    Зовётся после переразбора текста. Возвращает True, если разметку удалось
    переиспользовать, и False — если её нет или она уже не про этот звук.
    """
    from . import diarize

    handle = jobs.as_handle(handle)
    result = diarize.load_result(rec_id)
    if not result:
        return False
    turns = result.get("exclusive_turns") or result.get("turns") or []
    if not turns:
        return False

    # Сверяем длительность: если запись дописали или заменили, старая разметка
    # к новому звуку не относится, и переиспользовать её нельзя.
    target = _target_track(rec_id)
    if target is None:
        return False
    try:
        import wave

        with wave.open(str(store.track_path(rec_id, target)), "rb") as wf:
            now_s = wf.getnframes() / float(wf.getframerate())
    except Exception:
        return False
    was_s = float(result.get("duration_s") or 0.0)
    if was_s <= 0 or abs(now_s - was_s) > 1.0:
        handle.log("звук изменился с прошлой разметки, размечу заново")
        return False

    segs = store.sorted_segments(rec_id, include_echo=True)
    new_segs = diarize.relabel(segs, turns, track=target)
    # Имена и подсказки берём из того, что уже было подтверждено человеком.
    meta = store.get(rec_id) or {}
    _name_segments(new_segs, dict(meta.get("speakers") or {}), target)

    store.replace_segments(rec_id, new_segs)
    store.update(rec_id, {"diarized": True, "diarize_status": "done",
                          "diarize_progress": 1.0, "diarize_eta_s": None})
    store.refresh_participants(rec_id)
    handle.log("разметка голосов взята готовой, заново не считаю")
    log.info("запись %s: разметка переиспользована, интервалов %d", rec_id, len(turns))
    return True
