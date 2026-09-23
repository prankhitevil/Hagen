# -*- coding: utf-8 -*-
"""«Перечитать точнее»: пересобрать стенограмму записи точной моделью.

Текст распознаётся заново по сохранённому звуку, а всё, что человек уже
решил руками, возвращается на место: словарь замен, отметки «чьи это слова»,
разделение микрофона. Готовая разметка голосов берётся заново без счёта, если
звук с тех пор не менялся. Раньше этот конвейер целиком лежал в обработчике
маршрута; здесь он — задача очереди, которую маршрут только ставит.
"""
from __future__ import annotations

import logging
from typing import Any

from . import asr, audio_io, config, jobs, store, vad
from .events import hub
from .recordings import refresh_note

log = logging.getLogger("hagen.reread")


def submit(rec_id: str, want: int = 0) -> str:
    """Поставить переразбор в очередь. Возвращает номер задачи.

    ``want`` — сколько голосов у собеседников назвал человек (решение 14.09:
    число спрашивается только здесь, и только если он сам его назвал).
    Названное число означает «ровно столько»: разметка считается заново, а не
    берётся готовая, — иначе указывать число было бы незачем. 0 — не называл.
    """
    from . import diarize_jobs

    meta = store.get(rec_id)
    if meta is None:
        raise LookupError("Запись не найдена")
    if jobs.busy_with("retranscribe", rec_id):
        raise jobs.Busy("Уже перечитываю")
    tracks = store.existing_tracks(rec_id)
    if not tracks:
        raise ValueError("Нет сохранённого звука")
    diarize_jobs.remember_voices(rec_id, others=want)

    def work(handle: Any) -> dict[str, Any]:
        return _reread(rec_id, tracks, want, handle)

    return jobs.submit("retranscribe", work,
                       "Перечитать точнее: %s" % meta.get("title"), rec_id=rec_id)


def _reread(rec_id: str, tracks: list[str], want: int, handle: Any) -> dict[str, Any]:
    from . import diarize_jobs, echo, fixes
    from . import edits as edits_mod

    # Разделение микрофона помним ДО пересборки: реплики собираются заново,
    # и прежнее разделение к ним не относится, но просьба «со мной в комнате
    # были ещё люди» / «меня не было» остаётся в силе (15.09:
    # раньше она молча пропадала, и все реплики снова становились «Я»).
    before = store.get(rec_id) or {}
    # Ручные решения «чьи это слова» помним отрезками времени: реплики
    # пересобираются, а звук остаётся тем же (решение 17.09).
    hand = edits_mod.hand_marks(rec_id)
    mic_split = bool(before.get("room_shared") or (before.get("splits") or {}).get("me"))
    mic_absent = bool(before.get("owner_absent"))
    mic_voices = int((before.get("voices_hint") or {}).get("mine") or 0)
    handle.log("перечитываю точной моделью")
    all_segs: list[dict[str, Any]] = []
    for i, track in enumerate(tracks):
        pcm, _sr = audio_io.read_wav(store.track_path(rec_id, track))
        spans = vad.split_for_asr(pcm)
        handle.log("дорожка %s: %d фрагментов" % (track, len(spans)))

        def prog(frac, _i=i, _track=track):
            base = _i / float(len(tracks))
            handle.progress(base + frac / float(len(tracks)), "дорожка %s" % _track)

        pieces = asr.transcribe_spans(pcm, spans, precise=True, words=True, progress=prog)
        for p in pieces:
            # Словарь из ручных правок применяем и здесь: текст распознан
            # заново, значит имена и термины опять «как услышала модель».
            seg = store.make_segment(track, p["start"], p["end"], fixes.apply(p["text"]))
            seg["words"] = fixes.apply_words(p.get("words") or [])
            all_segs.append(seg)

    all_segs.sort(key=lambda s: (float(s["start"]), s["track"]))
    store.replace_segments(rec_id, all_segs)
    # Реплики собраны заново — прежнее разделение голосов к ним не относится.
    store.update(rec_id, {"splits": {}, "room_shared": False})
    # Стенограмма пересобрана с нуля, значит и пометки эха пропали — иначе
    # эхо колонок вернулось бы в текст после каждого переразбора.
    echo.mark(rec_id)

    # Переразбор меняет только ТЕКСТ реплик, звук остаётся тот же. Поэтому
    # гонять разметку заново незачем: готовая лежит в diarization.json, и
    # разнести новые реплики по голосам — это сравнение отрезков времени, без
    # нейросети. На записи 9 минут это 5 минут против 0,1 секунды. Если
    # сохранённой разметки нет или звук с тех пор изменился — честно ставим
    # задачу в очередь, как раньше.
    reused = False if want else diarize_jobs.reuse_diarization(rec_id, handle)
    # Ручные решения кладём ПОСЛЕ разметки: человек главнее модели.
    # Текст правок не возвращается — он распознан заново, счётчик обнуляем.
    back = edits_mod.apply_hand_marks(rec_id, hand)
    if back:
        handle.log("ручные решения о голосах вернулись на %d реплик" % back)
    store.update(rec_id, {"edits_count": 0})
    visible = store.sorted_segments(rec_id)
    if not reused:
        store.update(rec_id, {"diarized": False, "diarize_status": "none", "speakers": {}})
    hub.publish({"type": "recording", "meta": store.get(rec_id)})
    hub.publish({"type": "segments", "rec_id": rec_id, "segments": visible})
    hub.publish({"type": "notice", "level": "ok",
                 "text": "Перечитано точной моделью: %d реплик" % len(visible)})
    refresh_note(rec_id, "Перечитать точнее")

    # Число назвали — размечаем даже при выключенной авторазметке: человек
    # попросил об этом сам.
    if not reused and (want or config.get("diarize_auto")):
        try:
            diarize_jobs.queue_diarize(rec_id, want or None, want or None)
        except Exception as err:                           # noqa: BLE001
            log.info("запись %s: разметка после переразбора не встала в очередь: %s", rec_id, err)
    # Микрофон был разделён — делим заново уже новые реплики. «Я» встанет
    # по образцу, остальные голоса узнаются по базе.
    resplit = False
    if (mic_split or mic_absent) and store.TRACK_MIC in tracks:
        from . import speakers

        if speakers.segments_of(rec_id, "me"):
            store.update(rec_id, {"room_shared": mic_split, "owner_absent": mic_absent})
            try:
                diarize_jobs.queue_split(rec_id, "me", mic_voices if not mic_absent else 0)
                resplit = True
                handle.log("микрофон был разделён — разделяю голоса заново")
            except Exception as err:                       # noqa: BLE001
                log.warning("запись %s: микрофон заново не разделился: %s", rec_id, err)
                store.update(rec_id, {"room_shared": False, "owner_absent": False})
                hub.publish({"type": "notice", "level": "err",
                             "text": "Голоса микрофона заново не разделились: %s" % err})
            hub.publish({"type": "recording", "meta": store.get(rec_id)})
    return {"segments": len(visible), "diarization_reused": reused,
            "speakers": want or None, "mic_resplit": resplit}
