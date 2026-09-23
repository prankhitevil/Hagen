# -*- coding: utf-8 -*-
"""Живая запись как одно целое: старт, продолжение, стоп, устройства, снимки.

Раньше это лежало в `server.py` вперемешку с маршрутами, а часть — в
`api/deps.py`: трей и автоматика звонков звали приватные функции службы, а
пакет маршрутов держал состояние записи. Здесь — только логика: маршрут
принимает запрос, зовёт сюда и отдаёт ответ; трей и звонки зовут то же самое
напрямую, без прогона через цикл событий.

Ошибки — обычными исключениями: `LookupError` — записи нет, `ValueError` — так
нельзя, `jobs.Busy` — уже идёт, `Locked` — файлы держит другая программа.
В коды ответов их переводит `api/deps.as_http`.
"""
from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Any, Callable

from . import audio_io, config, jobs, live, platform, store
from .events import hub

log = logging.getLogger("hagen.recordings")

# Короче этого автоматическую разметку говорящих не запускаем: на обрывке в
# пару секунд разделять нечего, а разметка отнимет минуты процессорного времени.
MIN_DIARIZE_SECONDS = 8.0

NO_AUDIO_HINT = (
    "Нет доступа к звукозаписи. Обычно это правило антивируса: нужно разрешить "
    "доступ к микрофону файлу python.exe из папки приложения. Подробности — "
    "в памятке «Диктофон — как пользоваться», раздел «Если запись не идёт»."
)

#: Три ответа диалога удаления — три объёма работы.
DELETE_SCOPES = ("all", "media", "history")


class Locked(RuntimeError):
    """Файлы записи держит другая программа — плеер или антивирус."""


#: Живые записи. Запись переживает закрытие и перезагрузку окна.
sessions = live.SessionManager(on_event=hub.publish)

_captures: dict[str, Any] = {}
_shot_watchers: dict[str, Any] = {}
_level_pump: threading.Thread | None = None
_level_stop = threading.Event()
#: Кому сказать, что запись остановлена: автоматике звонков.
_stopped_listeners: list[Callable[[str], Any]] = []


# ------------------------------------------------------------------ состояние


def active_id() -> str | None:
    """Номер идущей записи или None."""
    sess = sessions.active_session()
    return sess.rec_id if sess is not None else None


def is_active(rec_id: str) -> bool:
    sess = sessions.get(rec_id)
    return sess is not None and sess.active


def busy(rec_id: str) -> bool:
    """Занята ли запись: идёт или по ней работает задача (звук удалять нельзя)."""
    if is_active(rec_id):
        return True
    return any(jobs.busy_with(kind, rec_id) for kind in
               ("diarize", "split", "retranscribe", "minutes", "summary", "media", "echo"))


def watching_shots(rec_id: str) -> bool:
    """Следим ли за снимками экрана для этой записи."""
    return rec_id in _shot_watchers


def on_stopped(fn: Callable[[str], Any]) -> None:
    """Подписаться на остановку записи (кнопкой или автоматикой)."""
    if fn not in _stopped_listeners:
        _stopped_listeners.append(fn)


def rec_paths(rec_id: str, meta: dict[str, Any]) -> list[dict[str, Any]]:
    """Где у этой записи что лежит — для показа в карточке.

    Отдаём только то, что ПРАВДА есть на диске: обещать файл, которого нет,
    хуже, чем не обещать ничего.
    """
    out: list[dict[str, Any]] = []

    def add(key: str, title: str, raw: str | Path | None) -> None:
        if not raw:
            return
        p = Path(str(raw))
        try:
            if not p.exists():
                return
        except OSError:
            return
        out.append({"key": key, "title": title, "path": str(p),
                    "is_dir": p.is_dir()})

    add("video", "Видео", meta.get("video_path"))
    folder = str(meta.get("assets_folder") or "")
    if folder:
        d = store.assets_dir(rec_id, folder)
        add("transcript", "Текстовая копия стенограммы", d / "transcript.txt")
        add("assets", "Папка с файлами записи", d)
    add("note", "Заметка в хранилище", meta.get("vault_path"))
    shots = [s for s in (meta.get("screenshots") or []) if s.get("path")]
    if shots:
        add("shots", "Снимки экрана (%d)" % len(shots), Path(str(shots[0]["path"])).parent)
    add("data", "Служебная папка записи", store.rec_dir(rec_id))
    return out


def payload(rec_id: str) -> dict[str, Any]:
    """Карточка записи целиком: описание, реплики, сеанс, задачи, пути."""
    meta = store.get(rec_id)
    if meta is None:
        raise LookupError("Запись не найдена")
    # У записей, сделанных до появления счётчика голосов, поля ещё нет:
    # считаем один раз при открытии записи, а не при каждом показе списка.
    if meta.get("voices") is None and meta.get("diarized"):
        try:
            store.refresh_participants(rec_id)
            meta = store.get(rec_id) or meta
        except Exception:
            log.debug("счётчик голосов не посчитался", exc_info=True)
    sess = sessions.get(rec_id)
    return {
        "meta": meta,
        "segments": store.sorted_segments(rec_id),
        "session": sess.status() if sess is not None else None,
        "jobs": jobs.for_recording(rec_id),
        "paths": rec_paths(rec_id, meta),
    }


# ------------------------------------------------------------ заметка Obsidian


def refresh_note(rec_id: str, why: str) -> None:
    """Заметка уже в Obsidian — переписать её после изменения данных записи (15.09).

    Раньше заметка оставалась такой, какой её записал «Стоп»: разметка голосов,
    подписи имён и «Перечитать точнее» до неё не доходили. Заметка — выгрузка
    из программы, в Obsidian её не правят (решение 15.09). Пишет на диск —
    из цикла событий звать только через run_in_executor.
    """
    from . import obsidian

    try:
        res = obsidian.refresh_note(rec_id)
    except Exception as err:
        log.warning("запись %s: заметка в Obsidian не обновилась после «%s»: %s", rec_id, why, err)
        hub.publish({"type": "notice", "level": "err",
                     "text": "Заметка в Obsidian не обновилась: %s" % err})
        return
    if res.get("skipped"):
        return
    log.info("запись %s: заметка в Obsidian обновлена после «%s»", rec_id, why)
    hub.publish({"type": "recording", "meta": store.get(rec_id)})


# ------------------------------------------------- кружок микрофона поверх окон
#
# Сам кружок живёт в розетке звука, это только обвязка: создать по надобности,
# показать на время записи, перекрасить. Его дёргают и маршруты `/api/mic`,
# и старт с остановкой записи.

_mic_pill = None


def _pill_click() -> dict[str, Any]:
    """Щелчок по кружку: переключить микрофон Windows."""
    return platform.audio().mic_toggle()


def mic_pill(create: bool = True):
    """Кружок микрофона. Не создаём, пока он не понадобился."""
    global _mic_pill

    if _mic_pill is None and create:
        try:
            _mic_pill = platform.audio().mic_pill(
                on_click=_pill_click,
                on_change=lambda st: hub.publish({"type": "mic", "mic": st}))
        except Exception as err:                      # noqa: BLE001
            log.warning("кружок микрофона недоступен: %s", err)
            _mic_pill = None
    return _mic_pill


def sync_mic_pill(recording: bool) -> None:
    """Показать кружок на время записи или убрать его.

    Показываем только при записи и только если включено в настройках
    (решение 17.09).
    """
    want = bool(recording) and bool(config.get("mic_pill", True))
    pill = mic_pill(create=want)
    if pill is None:
        return
    try:
        if want:
            # Цвет говорит про микрофон, движение — про запись (17.09).
            pill.show(bool(platform.audio().mic_state().get("muted")), recording=True)
        else:
            pill.hide()
    except Exception as err:                          # noqa: BLE001
        log.debug("кружок микрофона не переключился: %s", err)


# --------------------------------------------------------------- старт записи


def start(body: dict[str, Any] | None = None) -> dict[str, Any]:
    """Начать или продолжить живую запись. Общий путь кнопки «Старт», звонка и трея.

    Устройства открываются В ФОНЕ, ответ уходит сразу: иначе при недоступном
    звуке он ждал бы больше десяти секунд, страница выглядела бы «зависшей»,
    и человек нажимал бы «Старт» ещё раз. Итог открытия приходит событием
    `capture`.
    """
    body = body or {}
    # Одна запись за раз. Без этой проверки нетерпеливый повторный клик плодит
    # пустые записи — ровно это и случилось на первом живом прогоне.
    if sessions.active_session() is not None:
        raise jobs.Busy("Запись уже идёт. Сначала нажмите «Стоп».")

    title = (body.get("title") or "").strip()
    mode = body.get("mode") or config.get("mode") or "online"
    category = body.get("category") or config.get("default_category")
    want_far = bool(config.get("record_far")) and mode != "offline"

    # Продолжение уже открытой заметки: «Старт» после «Стоп» не должен плодить
    # новые записи — человек ведёт ОДНУ заметку и дополняет её, сколько нужно.
    resume_id = str(body.get("rec_id") or "").strip()
    if resume_id:
        meta = store.get(resume_id)
        if meta is None:
            raise LookupError("Заметка не найдена")
        if meta.get("source") != "live":
            raise ValueError("К заметке с импортированным файлом нельзя дописать запись. "
                             "Создайте новую заметку.")
        if meta.get("media_removed"):
            # Звук этой заметки стёрли, а время новых реплик считается от
            # начала записи: дописанное встало бы поверх старого текста.
            raise ValueError("У этой заметки звук удалён — дописать к ней запись нельзя. "
                             "Создайте новую заметку.")
        store.update(resume_id, {"status": "recording", "mode": mode, "category": category})
        sess = sessions.start(resume_id, mode=mode)
        _capture_in_background(resume_id, sess, want_far, created=False)
        meta = store.get(resume_id) or meta
        _announce_started(resume_id, meta)
        log.info("продолжаю запись %s: %s", resume_id, meta.get("title"))
        out = payload(resume_id)
        out["capture"] = {"state": "starting"}
        out["resumed"] = True
        return out

    meeting = body.get("meeting") if isinstance(body.get("meeting"), dict) else None
    if not title and meeting is None and config.get("outlook_enabled"):
        try:
            desktop = platform.desktop()
            meeting = desktop.current_meeting()
            if meeting:
                title = desktop.suggest_title(meeting)
        except Exception as err:
            log.debug("Outlook не дал встречу: %s", err)
    if not title and meeting:
        title = platform.desktop().suggest_title(meeting)

    meta = store.create(title=title, mode=mode, category=category, source="live")
    rec_id = meta["id"]
    if meeting:
        store.update(rec_id, {"meeting": meeting})
    sess = sessions.start(rec_id, mode=mode)
    store.update(rec_id, {"status": "recording"})
    _capture_in_background(rec_id, sess, want_far, created=True)
    meta = store.get(rec_id) or meta
    _announce_started(rec_id, meta)
    log.info("начата запись %s: %s", rec_id, meta["title"])
    out = payload(rec_id)
    out["capture"] = {"state": "starting"}
    return out


def _announce_started(rec_id: str, meta: dict[str, Any]) -> None:
    hub.publish({"type": "recording", "meta": meta})
    hub.publish({"type": "recordings"})
    hub.publish({"type": "recording_state", "rec_id": rec_id, "active": True})
    sync_mic_pill(True)


def _capture_in_background(rec_id: str, sess: Any, want_far: bool, created: bool) -> None:
    threading.Thread(target=_start_device_capture, args=(rec_id, sess, want_far, created),
                     name="capture-%s" % rec_id[-4:], daemon=True).start()


def _start_device_capture(rec_id: str, sess: Any, want_far: bool,
                          created: bool = False) -> dict[str, Any]:
    """Открыть устройства. Выполняется в фоне, результат уходит событием.

    created — запись заведена этим же «Стартом». Только такую, пустую, можно
    убрать, если звук не открылся. Продолжение существующей заметки («Старт»
    после «Стоп», «дописать в прошлую» по звонку) при отказе устройств должно
    остаться как было: раньше папка записи удалялась целиком вместе со
    стенограммой и звуком прошлых сеансов.
    """
    cap = live.DeviceCapture(sess)
    try:
        status = cap.start(want_far=want_far)
    except Exception as err:
        log.error("захват не запустился: %s", err)
        status = {"mic_error": str(err), "far_error": None, "mic": None, "far": None}

    got_mic = status.get("mic") is not None
    got_far = status.get("far") is not None

    if got_mic or got_far:
        _captures[rec_id] = cap
        _ensure_level_pump()
        _start_shots(rec_id, sess)
        status["state"] = "ok"
        if got_mic and not got_far and want_far:
            hub.publish({"type": "notice", "level": "err",
                         "text": "Пишется только микрофон: звук собеседников не открылся. "
                                 "Подключение повторяется в фоне. Проверьте, на "
                                 "какое устройство выводится звонок."})
        else:
            hub.publish({"type": "notice", "level": "ok",
                         "text": "Идёт запись: %s"
                                 % ("микрофон и собеседники" if got_far else "только микрофон")})
        hub.publish({"type": "capture", "rec_id": rec_id, "status": status})
        return status

    # Ни одна дорожка не открылась. «Стоп» мог прийти раньше, чем устройства
    # успели открыться, — тогда это не отказ звука, а просто короткая запись.
    status["state"] = "failed"
    stopped_early = bool(getattr(sess, "_stopping", False)) or not sess.active
    try:
        cap.stop()
    except Exception:
        log.debug("захват не закрылся", exc_info=True)
    try:
        sessions.stop(rec_id)
    except Exception:
        log.debug("сеанс уже остановлен", exc_info=True)
    # Записи не будет — снимаем и кружок микрофона: он поднят «Стартом», а
    # гасят его только «Стоп» и удаление, и он висел бы поверх всех окон.
    sync_mic_pill(False)

    # Убираем только пустышку, заведённую этим же «Стартом»: без звука и без
    # реплик. Заметку с прошлыми сеансами не трогаем ни при каких условиях.
    fresh = created and not store.existing_tracks(rec_id) and not store.sorted_segments(rec_id)
    if fresh:
        log.error("запись %s: звук недоступен, отменяю её", rec_id)
        try:
            store.delete(rec_id)
        except Exception:
            log.debug("пустую запись удалить не вышло", exc_info=True)
    else:
        log.error("запись %s: звук недоступен, прошлые сеансы заметки сохранены", rec_id)
    status["deleted"] = fresh

    hub.publish({"type": "capture", "rec_id": rec_id, "status": status})
    hub.publish({"type": "recordings"})
    if stopped_early:
        hub.publish({"type": "notice", "level": "ok",
                     "text": "Запись остановлена раньше, чем открылись устройства."})
    else:
        hub.publish({"type": "notice", "level": "err", "text": NO_AUDIO_HINT})
    return status


def _stop_device_capture(rec_id: str) -> None:
    cap = _captures.pop(rec_id, None)
    if cap is not None:
        try:
            cap.stop()
        except Exception as err:
            log.warning("устройства не закрылись чисто: %s", err)
    watcher = _shot_watchers.pop(rec_id, None)
    if watcher is not None:
        try:
            watcher.stop()
        except Exception as err:
            log.warning("наблюдение за снимками экрана не остановилось: %s", err)


def _start_shots(rec_id: str, sess: Any) -> None:
    """Снимки экрана, сделанные во время записи, — в заметку по времени."""
    if not config.get("screenshots_enabled", True) or rec_id in _shot_watchers:
        return
    try:
        from . import obsidian

        def on_shot(entry: dict[str, Any]) -> None:
            hub.publish({"type": "notice", "level": "ok",
                         "text": "Снимок экрана добавлен в запись — %s"
                                 % obsidian.hms(entry.get("at_s"))})
            hub.publish({"type": "recording", "meta": store.get(rec_id)})

        watcher = platform.system().watch_screenshots(
            rec_id, position_s=lambda: sess.duration, on_shot=on_shot,
            folder=lambda: obsidian.shots_dir(store.get(rec_id) or {}))
        watcher.start()
        _shot_watchers[rec_id] = watcher
    except Exception as err:
        log.warning("наблюдение за снимками экрана не включилось: %s", err)


def _ensure_level_pump() -> None:
    """Шлёт в интерфейс уровни индикаторов, пока идёт запись."""
    global _level_pump
    if _level_pump is not None and _level_pump.is_alive():
        return
    _level_stop.clear()

    def run() -> None:
        while not _level_stop.is_set():
            if not _captures:
                time.sleep(0.3)
                continue
            for rec_id, cap in list(_captures.items()):
                try:
                    levels = cap.levels
                    st = cap.status()
                except Exception:
                    continue
                hub.publish({
                    "type": "levels", "rec_id": rec_id, "levels": levels,
                    "mic_silent": bool((st.get("mic") or {}).get("silent")),
                    "far_silent": bool((st.get("far") or {}).get("silent")),
                    "far_on": st.get("far") is not None or bool(st.get("far_lost")),
                    "mic_lost": bool(st.get("mic_lost")),
                    "far_lost": bool(st.get("far_lost")),
                })
            time.sleep(0.25)

    _level_pump = threading.Thread(target=run, name="levels", daemon=True)
    _level_pump.start()


# ------------------------------------------------------------------ остановка


def stop(rec_id: str) -> dict[str, Any]:
    """Остановить запись: закрыть устройства, дописать последние фразы, отсеять эхо.

    То же самое делает кнопка «Стоп», автоматика звонков и меню значка у часов.
    Разметка голосов ставится в очередь сама, если размечать есть что:
    случайный «Старт» даёт запись на пару секунд, и гонять на ней модель
    минуты незачем. Кнопкой «Разметить говорящих» можно запустить на любой.
    """
    meta = store.get(rec_id)
    if meta is None:
        raise LookupError("Запись не найдена")
    _stop_device_capture(rec_id)
    status = sessions.stop(rec_id)

    # Эхо колонок отсеиваем до разметки говорящих: иначе модель будет разводить
    # голоса на дорожке, где чужие слова числятся за владельцем микрофона.
    try:
        from . import echo

        marked = echo.mark(rec_id)
        if marked:
            hub.publish({"type": "notice", "level": "ok",
                         "text": "Убрал из стенограммы эхо колонок: реплик — %d. "
                                 "В наушниках этого не происходит." % marked})
    except Exception as err:
        log.warning("отсев эха не выполнен: %s", err)

    meta = store.get(rec_id) or meta
    hub.publish({"type": "recording", "meta": meta})
    hub.publish({"type": "recordings"})
    hub.publish({"type": "recording_state", "rec_id": rec_id, "active": False})
    sync_mic_pill(False)
    for fn in list(_stopped_listeners):
        try:
            fn(rec_id)
        except Exception:
            log.debug("слушатель остановки записи упал", exc_info=True)

    duration = float((meta or {}).get("duration_s") or 0.0)
    if config.get("diarize_auto") and store.existing_tracks(rec_id):
        if duration < MIN_DIARIZE_SECONDS:
            log.info("запись %s: %.1f c — коротко для разметки, пропускаю", rec_id, duration)
        else:
            try:
                from . import diarize_jobs

                diarize_jobs.queue_diarize(rec_id)
            except Exception as err:
                log.warning("автоматическая разметка не запустилась: %s", err)

    return {"meta": meta, "session": status, "segments": store.sorted_segments(rec_id)}


def stop_and_save(rec_id: str) -> None:
    """Остановка по звонку или из трея: то же, что «Стоп», плюс заметка в Obsidian.

    Окно в это время может быть спрятано, и нажать «Сохранить» некому.
    """
    from . import obsidian

    stop(rec_id)
    if store.sorted_segments(rec_id):
        try:
            obsidian.save_note(rec_id)
            hub.publish({"type": "recording", "meta": store.get(rec_id)})
        except Exception as err:
            log.warning("стенограмму после остановки сохранить не вышло: %s", err)


def discard(rec_id: str) -> None:
    """«Не писать»: снять запись и удалить её вместе со всем, что успело появиться."""
    _stop_device_capture(rec_id)
    sessions.stop(rec_id)
    hub.publish({"type": "recording_state", "rec_id": rec_id, "active": False})
    sync_mic_pill(False)
    delete(rec_id, "all")


def merge_and_resume(src_id: str, dst_id: str) -> None:
    """Звонок вернулся и человек сказал «дописать в прошлую»: переносим то, что
    уже успели записать, в прошлую заметку и продолжаем писать туда."""
    from . import calls

    dst = store.get(dst_id)
    if not dst or dst.get("source") != "live" or dst.get("media_removed"):
        raise RuntimeError("к прошлой заметке дописать нельзя")
    _stop_device_capture(src_id)
    sessions.stop(src_id)
    hub.publish({"type": "recording_state", "rec_id": src_id, "active": False})
    sync_mic_pill(False)
    for job in jobs.for_recording(src_id):
        if job.get("status") in ("queued", "running"):
            jobs.cancel(job["id"])
    calls.merge_recordings(src_id, dst_id)
    hub.publish({"type": "recordings"})
    start({"rec_id": dst_id, "category": dst.get("category")})


# ------------------------------------------------------------------- удаление


def delete(rec_id: str, scope: str = "all") -> dict[str, Any]:
    """Удалить запись в одном из трёх объёмов.

    all     — запись, видео и звук рядом с ней, заметка в хранилище;
    media   — только видео и звук. Стенограмма остаётся, и по ней потом
              собирается протокол или саммари: ради этого всё и затевалось —
              гигабайты уходят, работа сохраняется;
    history — убрать из программы вместе со стенограммой. Видео и заметка в
              хранилище остаются лежать на диске.
    """
    from . import obsidian

    scope = (scope or "all").strip().lower()
    if scope not in DELETE_SCOPES:
        raise ValueError("Неизвестный вид удаления: %s" % scope)
    meta = store.get(rec_id)
    if meta is None:
        raise LookupError("Запись не найдена")
    if is_active(rec_id):
        raise jobs.Busy("Сначала остановите запись")

    # Задачи по этой записи снимаем заранее: иначе обработка продолжит писать
    # в папку, которой уже нет, и вывалит непонятную ошибку. При удалении
    # ОДНИХ МЕДИАФАЙЛОВ трогаем только то, чему нужен звук: сборка протокола
    # или саммари идёт по стенограмме, она остаётся — обрывать её незачем.
    kinds = ("diarize", "retranscribe", "media") if scope == "media" else None
    stopped = 0
    for job in jobs.for_recording(rec_id):
        if job.get("status") not in ("queued", "running"):
            continue
        if kinds is not None and job.get("kind") not in kinds:
            continue
        if jobs.cancel(job["id"]):
            stopped += 1

    out: dict[str, Any] = {"scope": scope, "jobs_stopped": stopped,
                           "deleted": scope != "media"}

    if scope == "media":
        res = store.drop_media(rec_id)
        out.update({"freed_bytes": res.get("freed_bytes", 0),
                    "files": res.get("files", 0),
                    "ok": bool(res.get("ok")),
                    "left": list(res.get("left") or [])})
        hub.publish({"type": "recording", "meta": store.get(rec_id)})
        hub.publish({"type": "recordings"})
        log.info("удалены медиафайлы записи %s: %d файлов, %.1f МБ%s",
                 rec_id, out["files"], out["freed_bytes"] / 1048576.0,
                 (", не отдались: " + ", ".join(out["left"])) if out["left"] else "")
        return out

    freed = 0
    note_gone = None
    if scope == "all":
        freed = store.drop_assets(rec_id)
        try:
            out["shots_deleted"] = platform.system().delete_shots(
                rec_id, obsidian.shots_dir(store.get(rec_id) or {}))
        except Exception as err:            # снимки не должны мешать удалению
            log.warning("снимки экрана убрать не вышло: %s", err)
        try:
            note = obsidian.delete_note(rec_id)
            note_gone = bool(note.get("deleted"))
        except Exception as err:            # заметка не должна мешать удалению
            log.warning("заметку убрать не вышло: %s", err)
            note_gone = False
        out["note_deleted"] = note_gone

    if not store.delete(rec_id):
        # Различаем «нечего удалять» и «файл держит другая программа»: на
        # Windows плеер или антивирус не отдают файл, и прежний ответ «Запись
        # не найдена» звучал как неправда — запись-то на месте.
        if store.rec_dir(rec_id).exists():
            raise Locked("Файлы записи занял другой процесс — обычно это открытый "
                         "плеер или антивирус. Закройте его и попробуйте снова.")
        raise LookupError("Запись не найдена")
    out["freed_bytes"] = freed
    # Что осталось снаружи — говорим по факту, а не по обещанию из диалога.
    out["kept_video"] = bool(meta.get("video_path")) and scope == "history"
    out["kept_note"] = bool(meta.get("vault_path")) and scope == "history"
    hub.publish({"type": "recordings"})
    log.info("запись %s удалена (%s)", rec_id, scope)
    return out


# ------------------------------------------------------- запуск и остановка службы


def recover_orphans() -> None:
    """Записи, помеченные «пишется», после перезапуска службы уже не пишутся.

    Видеозадачи сюда не попадают: у них своя карта этапов в meta["stages"], и
    прерванная обработка продолжится с последнего сделанного этапа, а не
    пометится «запись была прервана».

    Служба могла быть закрыта во время записи. Звук на диске остался, поэтому
    запись не теряем: закрываем её по фактической длине дорожек и помечаем как
    прерванную, чтобы в списке не мигало «пишется» без конца.
    """
    fixed = 0
    unfinished = 0
    for meta in store.list_all():
        if meta.get("status") not in ("recording", "processing"):
            continue
        if isinstance(meta.get("stages"), dict):
            # Это видеозадача: её можно продолжить, а не закрывать как прерванную.
            store.update(meta["id"], {"status": "queued"})
            unfinished += 1
            continue
        rec_id = meta["id"]
        tracks = store.existing_tracks(rec_id)
        duration = 0.0
        for tr in tracks:
            # заголовок после краха отстаёт от файла — сначала чиним, потом меряем
            audio_io.repair_wav_header(store.track_path(rec_id, tr))
            duration = max(duration, audio_io.wav_duration(store.track_path(rec_id, tr)))
        store.update(rec_id, {
            "status": "recorded" if tracks else "empty",
            "duration_s": round(duration, 2),
            "tracks": tracks,
            "interrupted": True,
            "error": "запись была прервана закрытием службы",
        })
        fixed += 1
    if fixed:
        log.warning("восстановлено прерванных записей: %d", fixed)
    if unfinished:
        log.info("недоделанных видеозадач: %d — можно продолжить с последнего этапа",
                 unfinished)


def shutdown() -> None:
    """Служба закрывается: остановить уровни, устройства и записи."""
    _level_stop.set()
    for rec_id in list(_captures.keys()):
        _stop_device_capture(rec_id)
    try:
        sessions.stop_all()
    except Exception:
        log.error("не удалось чисто остановить записи", exc_info=True)
