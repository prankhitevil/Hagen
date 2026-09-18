# -*- coding: utf-8 -*-
"""Локальный сайт «Hagen»: FastAPI на 127.0.0.1.

Защита без пароля. Вход не нужен (это личная машина), но служба отвечает только
на запросы со своей же страницы: проверяются заголовки Host и Origin, а кросс-
доменные запросы (CORS) не разрешаются вообще. Это нужно, чтобы посторонний сайт,
открытый в том же браузере, не мог постучаться на localhost и вычитать стенограммы.

Звук служба пишет сама, двумя дорожками: микрофон («Я») и звук звонка
(«участники»), см. live.DeviceCapture. Окно только показывает и управляет:
команды идут запросами /api/…, ход работы — событиями по WebSocket /ws/events.
Захват звука браузером на этой машине не работал, и от него отказались.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from . import asr, audio_io, config, jobs, live, platform, store, vad

log = logging.getLogger("hagen.server")

STATIC_DIR = Path(__file__).resolve().parent / "static"
UPLOAD_DIR = config.DATA_DIR / "_uploads"

# Короче этого автоматическую разметку говорящих не запускаем: на обрывке в
# пару секунд разделять нечего, а pyannote отнимет минуты процессорного времени.
MIN_DIARIZE_SECONDS = 8.0

SAFE_HOSTS = {"127.0.0.1", "localhost", "[::1]", "::1"}


# ====================================================================== события


# Рассылка событий переехала в hagen/events.py, чтобы её могли брать и
# роутеры. Имена здесь сохранены: остальной код
# и проверки продолжают обращаться к server.hub и server.EventHub.
from .events import EventHub, hub  # noqa: E402  (после импортов модуля)
sessions = live.SessionManager(on_event=hub.publish)


def _job_changed(job: dict[str, Any]) -> None:
    public = {
        k: job.get(k) for k in
        ("id", "kind", "title", "rec_id", "status", "progress", "eta_s", "note",
         "error", "cancel_requested", "error_kind", "resets_at", "retry",
         "created_at", "finished_at")
    }
    # Документ по видео собирается внутри обработки файла: если он упёрся в
    # лимит, повторять надо только сборку документа, а не всю обработку.
    if (public.get("error_kind") == "claude_limit" and not public.get("retry")
            and job.get("kind") == "media" and job.get("rec_id")):
        public["retry"] = {"url": "/api/media/%s/summary" % job["rec_id"], "body": {}}
    hub.publish({"type": "job", "job": public})
    if job.get("status") == "error":
        # Раньше упавшая задача просто исчезала из блока «В работе», и человек
        # не узнавал, что документа не будет.
        hub.publish({"type": "notice", "level": "err",
                     "text": "%s — не получилось: %s" % (job.get("title") or "Задача",
                                                        job.get("note") or job.get("error"))})
    rec_id = job.get("rec_id")
    if rec_id and job.get("status") in ("done", "error", "cancelled"):
        # Разметка ставит в meta пометку «размечаю» и снимает её только при
        # успехе. Оборвалась — пометка висела бы вечно, и запись выглядела бы
        # вечно занятой: ни кнопки, ни понимания, что произошло.
        broken = job.get("status") != "done"
        if job.get("kind") == "diarize" and broken:
            meta = store.get(rec_id) or {}
            if meta.get("diarize_status") in ("queued", "running"):
                store.update(rec_id, {
                    "diarize_status": "cancelled" if job.get("status") == "cancelled" else "error",
                    "diarize_progress": 0.0,
                    "diarize_eta_s": None,
                })
        # То же самое с обработкой файла: запись заводится со статусом
        # «в очереди», а он снимается только при успехе. Остановленная кнопкой
        # обработка оставляла бы в списке вечную метку «обработка».
        # Разметка закончилась, а хранить звук просили «ничего» — убираем его
        # сейчас: до этого мига дорожка была нужна самой разметке.
        # Исключение: под одним именем нашлось несколько голосов — звук держим,
        # пока человек не решит, разделять ли их (решение 13.09).
        if job.get("kind") == "diarize" and job.get("status") == "done":
            _drop_media_if_done(rec_id)
        if job.get("kind") == "media" and broken:
            meta = store.get(rec_id) or {}
            if meta.get("status") in ("queued", "processing"):
                has_text = bool(store.sorted_segments(rec_id))
                store.update(rec_id, {
                    "status": "recorded" if has_text else "stopped",
                    "error": None if has_text else (job.get("note") or "обработка остановлена"),
                })
        meta = store.get(rec_id)
        if meta:
            hub.publish({"type": "recording", "meta": meta})


jobs.on_change(_job_changed)


# ====================================================================== защита


#: фактический порт, на котором служба слушает сейчас. Может отличаться от
#: настройки, если порт по умолчанию был занят, — и тогда проверять Origin надо
#: именно по нему, иначе служба отвергнет собственную страницу.
ACTUAL_PORT: int | None = None


def set_actual_port(port: int) -> None:
    global ACTUAL_PORT
    ACTUAL_PORT = int(port)


def _expected_origins() -> set[str]:
    # Только фактический порт: если порт из настроек занят чужой программой,
    # её страница не должна проходить проверку Origin.
    ports = {int(ACTUAL_PORT)} if ACTUAL_PORT else {int(config.get("port") or 8787)}
    out = set()
    for host in ("127.0.0.1", "localhost"):
        for port in ports:
            out.add("http://%s:%d" % (host, port))
    return out


def _host_ok(request: Request) -> bool:
    host = (request.headers.get("host") or "").strip().lower()
    if not host:
        return False
    name = host.rsplit(":", 1)[0] if host.count(":") == 1 else host
    return name in SAFE_HOSTS


def _origin_ok(origin: str | None) -> bool:
    if not origin:
        return True          # обычный переход по адресу — заголовка Origin нет
    return origin.strip().lower() in _expected_origins()


class GuardMiddleware:
    """Отвечаем только своей же странице. Ничего от пользователя не требуется."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        headers = {k.decode("latin-1").lower(): v.decode("latin-1")
                   for k, v in scope.get("headers") or []}
        host = (headers.get("host") or "").strip().lower()
        name = host.rsplit(":", 1)[0] if host.count(":") == 1 else host
        origin = headers.get("origin")
        method = scope.get("method", "GET")

        bad = None
        if name not in SAFE_HOSTS:
            bad = "чужой заголовок Host: %s" % host
        elif not _origin_ok(origin):
            bad = "чужой Origin: %s" % origin
        elif scope["type"] == "websocket" and not origin:
            bad = "WebSocket без Origin"
        elif method in ("POST", "PUT", "PATCH", "DELETE") and not origin:
            bad = "изменяющий запрос без Origin"

        if bad:
            log.warning("запрос отклонён: %s (%s %s)", bad, method, scope.get("path"))
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 4403})
                return
            body = json.dumps(
                {"error": "Запрос отклонён: страница не с этого приложения.",
                 "detail": bad}, ensure_ascii=False).encode("utf-8")
            await send({"type": "http.response.start", "status": 403,
                        "headers": [(b"content-type", b"application/json; charset=utf-8"),
                                    (b"content-length", str(len(body)).encode()),
                                    (b"x-content-type-options", b"nosniff")]})
            await send({"type": "http.response.body", "body": body})
            return

        await self.app(scope, receive, send)


# ====================================================================== приложение

app = FastAPI(title="Hagen", docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(GuardMiddleware)


@app.middleware("http")
async def no_store(request: Request, call_next):
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


@app.on_event("startup")
async def _startup() -> None:
    config.ensure_dirs()
    # Разовый перенос возможностей с прежней установки: у того, кто обновился,
    # функции уже настроены, и молча пропасть они не должны (п. 9.2).
    try:
        config.adopt_used_features()
    except Exception as err:
        log.warning("возможности не перенеслись: %s", err)
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    _sweep_uploads()
    hub.bind_loop(asyncio.get_running_loop())
    _media_ready()
    _recover_orphans()
    log.info("служба запущена на 127.0.0.1:%s", config.get("port"))
    asyncio.get_running_loop().run_in_executor(None, _warmup_async)
    _start_call_watcher()
    _start_retention()
    _start_dictation()


def _sweep_uploads(older_than_s: float = 86400.0) -> None:
    """Убрать из data\\_uploads файлы старше суток.

    Обработка загруженного файла удаляет его сама; здесь подчищаются остатки
    от служб, закрытых посреди обработки (раньше они копились навсегда).
    """
    limit = time.time() - older_than_s
    try:
        for item in UPLOAD_DIR.iterdir():
            try:
                if item.is_file() and item.stat().st_mtime < limit:
                    item.unlink()
                    log.info("убран старый загруженный файл: %s", item.name)
            except OSError:
                pass
    except OSError:
        pass


_retention_thread: threading.Thread | None = None


def _busy_record(rec_id: str) -> bool:
    if sessions.get(rec_id) is not None and sessions.get(rec_id).active:
        return True
    return any(jobs.busy_with(kind, rec_id) for kind in ("diarize", "split", "retranscribe", "minutes",
                                                          "summary", "media", "echo"))


def _run_retention() -> dict[str, Any]:
    """Удалить звук звонков старше срока из настроек (если срок задан)."""
    from . import storage

    res = storage.purge(busy=_busy_record)
    if res["removed"]:
        for rec_id in res["removed"]:
            hub.publish({"type": "recording", "meta": store.get(rec_id)})
        hub.publish({"type": "notice", "level": "ok",
                     "text": "Звук звонков старше %d дн. удалён: записей %d, освобождено %.0f МБ. "
                             "Стенограммы и документы на месте."
                             % (res["days"], len(res["removed"]), res["freed_bytes"] / 1048576.0)})
    return res


def _start_retention() -> None:
    """Срок хранения звука проверяется через минуту после запуска и дальше раз в 6 часов."""
    global _retention_thread
    if _retention_thread is not None:
        return

    def loop() -> None:
        time.sleep(60)
        while True:
            try:
                _run_retention()
            except Exception as err:
                log.warning("проверка срока хранения звука не удалась: %s", err)
            time.sleep(6 * 3600)

    _retention_thread = threading.Thread(target=loop, name="retention", daemon=True)
    _retention_thread.start()


@app.get("/api/storage")
async def api_storage() -> JSONResponse:
    """Где лежит звук записей и видео и сколько занимает (для «Настройки → Основное»)."""
    from . import storage

    loop = asyncio.get_running_loop()
    audio = await loop.run_in_executor(None, storage.audio_usage)
    assets = await loop.run_in_executor(None, storage.assets_usage)
    days = storage.retention_days()
    due = storage.expired(days, busy=_busy_record) if days else []
    return JSONResponse({"audio": audio, "videos": assets, "retention_days": days,
                         "due": {"records": len(due), "bytes": sum(d["bytes"] for d in due)}})


@app.post("/api/storage/reveal")
async def api_storage_reveal(request: Request) -> JSONResponse:
    """Открыть папку звука записей или папку видео. Путь — только из своего списка."""
    body = await request.json()
    what = str((body or {}).get("what") or "")
    folder = {"audio": config.DATA_DIR, "videos": store.assets_root()}.get(what)
    if folder is None:
        raise HTTPException(status_code=400, detail="Неизвестная папка")
    if not Path(folder).exists():
        raise HTTPException(status_code=404, detail="Папки пока нет")
    try:
        platform.system().open_path(folder)
    except OSError as err:
        raise HTTPException(status_code=500, detail="Не удалось открыть папку: %s" % err) from None
    return JSONResponse({"opened": str(folder)})


@app.post("/api/storage/purge")
async def api_storage_purge() -> JSONResponse:
    """«Удалить сейчас» — звук звонков старше срока, не дожидаясь проверки по расписанию."""
    loop = asyncio.get_running_loop()
    res = await loop.run_in_executor(None, _run_retention)
    return JSONResponse(res)


def _recover_orphans() -> None:
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


def _warmup_async() -> None:
    try:
        # Точную модель на старте не греем никогда — решение 14.09:
        # она вместе с torch занимает около 1,3 ГБ, а нужна не всегда. Грузится
        # при первом обращении (файл, «Перечитать точнее», диктовка) и
        # выгружается после простоя (asr.release_idle, «precise_idle_min»).
        # Диктовка начинает загрузку сама, как только человек начал говорить.
        info = asr.warmup(live=True, precise=False)
        log.info("модель эфира прогрета: %s", info)
        hub.publish({"type": "ready", "asr": asr.live_state()})
    except Exception as err:
        log.error("прогрев модели не удался: %s", err)
        hub.publish({"type": "ready",
                     "asr": {"state": "error", "error": str(err)[:300], "seconds": None}})


@app.on_event("shutdown")
async def _shutdown() -> None:
    _level_stop.set()
    for rec_id in list(_captures.keys()):
        _stop_device_capture(rec_id)
    try:
        sessions.stop_all()
    except Exception:
        log.error("не удалось чисто остановить записи", exc_info=True)
    _stop_call_watcher()
    _stop_dictation()


# ====================================================================== детект звонка

_watcher = None
_call_state: dict[str, Any] = {"active": False, "asked": False, "meeting": None}
_calls = None      # calls.CallAutomation — звонок → запись


def _loop_call(coro, timeout: float = 120.0):
    """Выполнить корутину службы из постороннего потока и дождаться итога."""
    loop = hub._loop
    if loop is None or loop.is_closed():
        coro.close()
        raise RuntimeError("служба ещё не запущена")
    fut = asyncio.run_coroutine_threadsafe(coro, loop)
    try:
        return fut.result(timeout=timeout)
    except HTTPException as err:
        raise RuntimeError(str(err.detail)) from None


def _call_active_recording() -> str | None:
    sess = sessions.active_session()
    return sess.rec_id if sess is not None else None


def _call_start_recording(meeting: dict[str, Any] | None) -> str:
    body: dict[str, Any] = {"category": config.get("default_category")}
    if meeting:
        body["meeting"] = meeting
    payload = _loop_call(_create_recording(body), timeout=60)
    return str(payload["meta"]["id"])


def _call_stop_recording(rec_id: str) -> None:
    """Остановка по звонку делает то же, что кнопка «Стоп» в окне, и сохраняет
    стенограмму: окно в это время может быть спрятано в трей."""
    _loop_call(api_recording_stop(rec_id), timeout=180)
    if store.sorted_segments(rec_id):
        try:
            _loop_call(api_save_note(rec_id), timeout=60)
        except Exception as err:
            log.warning("стенограмму после остановки по звонку сохранить не вышло: %s", err)


def _call_discard_recording(rec_id: str) -> None:
    _stop_device_capture(rec_id)
    sessions.stop(rec_id)
    hub.publish({"type": "recording_state", "rec_id": rec_id, "active": False})
    sync_mic_pill(False)
    _loop_call(api_recording_delete(rec_id, "all"), timeout=60)
    hub.publish({"type": "recordings"})


def _call_merge_and_resume(src_id: str, dst_id: str) -> None:
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
    _loop_call(_create_recording({"rec_id": dst_id, "category": dst.get("category")}),
               timeout=60)


def _start_call_watcher() -> None:
    global _watcher, _calls
    if not config.get("call_watch_enabled"):
        return
    try:
        from . import calls
    except Exception as err:
        log.info("детект звонка недоступен: %s", err)
        return

    _calls = calls.CallAutomation(calls.Hooks(
        active_recording=_call_active_recording,
        start_recording=_call_start_recording,
        stop_recording=_call_stop_recording,
        discard_recording=_call_discard_recording,
        merge_and_resume=_call_merge_and_resume,
        publish=hub.publish,
    ))

    def on_start(info: dict[str, Any]) -> None:
        meeting = None
        try:
            if config.get("outlook_enabled"):
                meeting = platform.desktop().current_meeting()
        except Exception as err:
            log.debug("встречу Outlook прочитать не вышло: %s", err)
        _call_state.update({"active": True, "info": info, "meeting": meeting, "asked": False})
        hub.publish({"type": "call", "call": dict(_call_state)})
        _calls.on_call_start(info, meeting)

    def on_end(info: dict[str, Any]) -> None:
        _call_state.update({"active": False, "info": info, "asked": False})
        hub.publish({"type": "call", "call": dict(_call_state)})
        _calls.on_call_end(info)

    try:
        _watcher = platform.desktop().watch_calls(on_call_start=on_start, on_call_end=on_end)
        _watcher.start()
        log.info("наблюдение за звонками включено")
    except Exception as err:
        log.info("наблюдение за звонками не включилось: %s", err)


def _stop_call_watcher() -> None:
    global _watcher
    if _watcher is not None:
        try:
            _watcher.stop()
        except Exception:
            pass
        _watcher = None


# ====================================================================== диктовка

# Диктовка — свой запуск и остановка: их зовут и жизненный цикл службы, и
# маршруты /api/dictate, и сохранение настроек.
from .api.deps import (dictation_status as _dictation_status,  # noqa: E402
                       mic_pill,
                       start_dictation as _start_dictation,
                       stop_dictation as _stop_dictation,
                       sync_mic_pill)


# ====================================================================== страница


@app.get("/")
async def index(request: Request) -> Response:
    page = STATIC_DIR / "index.html"
    if not page.exists():
        return HTMLResponse("<h1>Интерфейс не собран</h1>", status_code=500)
    # Тему и плотность ставим прямо в страницу: иначе при тёмной теме окно
    # сначала мигало бы светлым, пока скрипт не загрузит настройки.
    html = page.read_text(encoding="utf-8")
    theme = str(config.get("ui_theme") or "light")
    density = str(config.get("ui_density") or "compact")
    if theme not in UI_THEMES:
        theme = "light"
    if density not in UI_DENSITIES:
        density = "compact"
    html = html.replace('data-theme="light" data-density="compact"',
                        'data-theme="%s" data-density="%s"' % (theme, density), 1)
    return HTMLResponse(html)


UI_THEMES = ("light", "dark", "warm", "bright")
UI_DENSITIES = ("tiny", "compact", "normal", "large")


@app.get("/favicon.ico")
async def favicon() -> Response:
    """Значок вкладки: тот же файл, что у окна и ярлыка (17.09).

    Раньше отдавали пустой ответ, и в браузере страница шла со стандартным
    значком-листком. Программа открывается и вкладкой (`ui_mode: browser`),
    поэтому значок нужен и здесь.
    """
    path = platform.shell().icon_paths().get("idle")
    if path is None or not path.exists():
        return Response(status_code=204)
    return Response(content=path.read_bytes(), media_type="image/x-icon",
                    headers={"Cache-Control": "public, max-age=86400"})


if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ====================================================================== состояние


def _segments(rec_id: str) -> list[dict[str, Any]]:
    return store.sorted_segments(rec_id)


def _rec_paths(rec_id: str, meta: dict[str, Any]) -> list[dict[str, Any]]:
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


def _recording_payload(rec_id: str) -> dict[str, Any]:
    meta = store.get(rec_id)
    if meta is None:
        raise HTTPException(status_code=404, detail="Запись не найдена")
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
        "segments": _segments(rec_id),
        "session": sess.status() if sess is not None else None,
        "jobs": jobs.for_recording(rec_id),
        "paths": _rec_paths(rec_id, meta),
    }


@app.get("/api/state")
async def api_state() -> JSONResponse:
    # Сбор состояния ходит в звуковой поток, к сейфу на Яндекс.Диске и ищет
    # Claude CLI — из цикла событий это замораживало всю службу на секунды
    # при каждом «Старте» и «Стопе». Считаем в фоне.
    loop = asyncio.get_running_loop()
    payload = await loop.run_in_executor(None, _state_payload)
    return JSONResponse(payload)


def _state_payload() -> dict[str, Any]:
    active = sessions.active_session()
    payload = {
        "recordings": store.list_all(),
        "settings": config.public(),
        "jobs": jobs.list_all(20),
        "active_recording": active.rec_id if active is not None else None,
        "session": active.status() if active is not None else None,
        "call": dict(_call_state),
        "prompt": _calls.prompt if _calls is not None else None,
        "capabilities": _capabilities(),
        "vault": _vault_status(),
        "categories": _categories(),
    }
    return payload


_caps_cache: tuple[float, dict[str, Any]] | None = None
_caps_lock = threading.Lock()
CAPS_TTL_S = 20.0


def _capabilities() -> dict[str, Any]:
    """Что умеет служба. Дорогие проверки (поиск Claude CLI, устройства,
    Outlook) держим 20 с: страница спрашивает состояние при каждом «Старте»
    и «Стопе», а меняется здесь всё редко."""
    global _caps_cache
    with _caps_lock:
        now = time.time()
        if _caps_cache is not None and now - _caps_cache[0] < CAPS_TTL_S:
            caps = dict(_caps_cache[1])
            caps["call_detect"] = _watcher is not None
            caps["asr"] = _asr_state()
            return caps
        caps = _capabilities_fresh()
        _caps_cache = (now, dict(caps))
        caps["asr"] = _asr_state()
        return caps


def _asr_state() -> dict[str, Any]:
    """Состояние модели эфира — не кэшируем: оно и есть то, что меняется.

    Окно, открытое после прогрева (из трея, после переподключения сокета),
    разового события «ready» не получало и навсегда оставалось с надписью
    «модель загружается…». Теперь состояние
    приходит с каждым запросом состояния.
    """
    try:
        from . import asr

        return asr.live_state()
    except Exception as err:                          # noqa: BLE001
        return {"state": "error", "error": "модуль распознавания недоступен: %s" % err,
                "seconds": None}


def _capabilities_fresh() -> dict[str, Any]:
    caps: dict[str, Any] = {
        "diarize": False,
        "diarize_note": "",
        "outlook": False,
        "call_detect": _watcher is not None,
        "claude_cli": False,
    }
    try:
        from . import diarize

        ok, why = diarize.available()
        caps["diarize"] = bool(ok)
        caps["diarize_note"] = why
    except Exception as err:
        caps["diarize_note"] = "модуль разметки говорящих недоступен: %s" % err
    try:
        from . import minutes

        from . import providers as _prov

        caps["claude_cli"] = bool(minutes.resolve_claude_cli())
        caps["engines"] = minutes.available_engines()
        caps["minutes_models"] = minutes.models_hint()
        caps["providers"] = _prov.public_list()
        caps["models_cached"] = _prov.cached_models(_prov.current_id()) or {}
    except Exception as err:
        caps["engines_note"] = str(err)
    try:
        caps["outlook"] = bool(platform.desktop().outlook_kind().get("com_available"))
    except Exception:
        pass
    try:
        caps["loopback_devices"] = platform.audio().list_loopback_devices()
    except Exception:
        caps["loopback_devices"] = []
    return caps


def _vault_status() -> dict[str, Any]:
    try:
        from . import obsidian

        return obsidian.vault_status()
    except Exception as err:
        return {"root": str(config.vault_root()), "exists": False, "error": str(err)}


def _categories() -> list[str]:
    try:
        from . import obsidian

        return obsidian.list_categories()
    except Exception:
        return list(config.get("categories") or [])


# ====================================================================== настройки


@app.get("/api/settings")
async def api_settings_get() -> JSONResponse:
    return JSONResponse(config.public())


@app.post("/api/settings")
async def api_settings_post(request: Request) -> JSONResponse:
    patch = await request.json()
    if not isinstance(patch, dict):
        raise HTTPException(status_code=400, detail="Ожидался объект настроек")
    for derived in ("hf_token_set", "hf_token_hint", "api_keys_set", "api_keys_hint",
                    "providers_public"):
        patch.pop(derived, None)
    # Список сервисов правится только своими точками (/api/providers): в общем
    # сохранении настроек он приехал бы из интерфейса без ключей и затёр бы их.
    patch.pop("providers", None)
    config.save(patch)
    if "autostart_windows" in patch:
        # Галочка «запускать вместе с Windows» — это ярлык в «Автозагрузке»:
        # кладём или убираем его сразу, а не при следующем запуске.
        try:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, platform.shell().set_autostart,
                                       bool(patch.get("autostart_windows")))
        except Exception as err:
            log.warning("автозапуск не переключился: %s", err)
    if "mic_pill" in patch:
        # Галочку сняли посреди записи — кружок должен исчезнуть сразу.
        sync_mic_pill(_call_active_recording() is not None)
    if any(k.startswith("dictate_") for k in patch):
        # Сочетание клавиш занимается и освобождается сразу, а не при следующем
        # запуске: иначе человек поменял бы клавишу и решил, что она не работает.
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, _start_dictation)
    public = config.public()
    try:
        public["autostart_present"] = platform.shell().autostart_enabled()
    except Exception:
        pass
    hub.publish({"type": "settings", "settings": public})
    return JSONResponse(public)


_app_hooks: dict[str, Any] = {}


def set_app_hooks(**hooks: Any) -> None:
    """Окно программы сообщает службе, как себя показать (для второго запуска)."""
    _app_hooks.update(hooks)


@app.post("/api/app/show")
async def api_app_show() -> JSONResponse:
    """Второй запуск ярлыка: показать окно работающего «Hagen» из трея."""
    fn = _app_hooks.get("show")
    if fn is None:
        return JSONResponse({"shown": False})
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, fn)
    return JSONResponse({"shown": True})


# ====================================================================== диктовка


@app.get("/api/recordings")
async def api_recordings() -> JSONResponse:
    return JSONResponse(store.list_all())


@app.post("/api/recordings")
async def api_recording_create(request: Request) -> JSONResponse:
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    return JSONResponse(await _create_recording(body))


async def _create_recording(body: dict[str, Any]) -> dict[str, Any]:
    """Начать или продолжить живую запись. Общий путь кнопки «Старт» и звонка."""
    # Одна запись за раз. Без этой проверки нетерпеливый повторный клик плодит
    # пустые записи — ровно это и случилось на первом живом прогоне.
    if sessions.active_session() is not None:
        raise HTTPException(status_code=409,
                            detail="Запись уже идёт. Сначала нажмите «Стоп».")

    title = (body.get("title") or "").strip()
    mode = body.get("mode") or config.get("mode") or "online"
    category = body.get("category") or config.get("default_category")

    # Продолжение уже открытой заметки: «Старт» после «Стоп» не должен плодить
    # новые записи — человек ведёт ОДНУ заметку и дополняет её, сколько нужно.
    resume_id = str(body.get("rec_id") or "").strip()
    if resume_id:
        meta = store.get(resume_id)
        if meta is None:
            raise HTTPException(status_code=404, detail="Заметка не найдена")
        if meta.get("source") != "live":
            raise HTTPException(
                status_code=400,
                detail="К заметке с импортированным файлом нельзя дописать запись. "
                       "Создайте новую заметку.")
        if meta.get("media_removed"):
            # Звук этой заметки стёрли, а время новых реплик считается от
            # начала записи: дописанное встало бы поверх старого текста.
            raise HTTPException(
                status_code=400,
                detail="У этой заметки звук удалён — дописать к ней запись нельзя. "
                       "Создайте новую заметку.")
        store.update(resume_id, {"status": "recording", "mode": mode,
                                 "category": category})
        sess = sessions.start(resume_id, mode=mode)
        capture_status: dict[str, Any] = {"state": "starting"}
        want_far = bool(config.get("record_far")) and mode != "offline"
        loop = asyncio.get_running_loop()
        loop.run_in_executor(None, _start_device_capture, resume_id, sess, want_far, False)
        meta = store.get(resume_id) or meta
        hub.publish({"type": "recording", "meta": meta})
        hub.publish({"type": "recordings"})
        hub.publish({"type": "recording_state", "rec_id": resume_id, "active": True})
        sync_mic_pill(True)
        log.info("продолжаю запись %s: %s", resume_id, meta.get("title"))
        payload = _recording_payload(resume_id)
        payload["capture"] = capture_status
        payload["resumed"] = True
        return payload

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
    if meeting:
        store.update(meta["id"], {"meeting": meeting})
        meta = store.get(meta["id"]) or meta

    sess = sessions.start(meta["id"], mode=mode)
    store.update(meta["id"], {"status": "recording"})

    # Устройства открываем В ФОНЕ и отвечаем сразу. Иначе при недоступном звуке
    # ответ ждал бы больше десяти секунд, страница выглядела бы «зависшей»,
    # и человек нажимал бы «Старт» ещё раз.
    capture_status: dict[str, Any] = {"state": "starting"}
    want_far = bool(config.get("record_far")) and mode != "offline"
    loop = asyncio.get_running_loop()
    loop.run_in_executor(None, _start_device_capture, meta["id"], sess, want_far, True)

    meta = store.get(meta["id"]) or meta
    hub.publish({"type": "recording", "meta": meta})
    hub.publish({"type": "recordings"})
    hub.publish({"type": "recording_state", "rec_id": meta["id"], "active": True})
    sync_mic_pill(True)
    log.info("начата запись %s: %s", meta["id"], meta["title"])
    payload = _recording_payload(meta["id"])
    payload["capture"] = capture_status
    return payload


_captures: dict[str, Any] = {}


NO_AUDIO_HINT = (
    "Нет доступа к звукозаписи. Обычно это правило антивируса: нужно разрешить "
    "доступ к микрофону файлу python.exe из папки приложения. Подробности — "
    "в памятке «Диктофон — как пользоваться», раздел «Если запись не идёт»."
)


def _start_device_capture(rec_id: str, sess, want_far: bool,
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
                         "text": "Пишу только микрофон: звук собеседников не открылся. "
                                 "Пробую подключить его снова в фоне. Проверьте, на "
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
        pass
    try:
        sessions.stop(rec_id)
    except Exception:
        log.debug("сеанс уже остановлен", exc_info=True)

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


_shot_watchers: dict[str, Any] = {}


def _start_shots(rec_id: str, sess) -> None:
    """Снимки экрана, сделанные во время записи, — в заметку по времени."""
    if not config.get("screenshots_enabled", True) or rec_id in _shot_watchers:
        return
    try:
        from . import obsidian

        def on_shot(entry: dict[str, Any]) -> None:
            hub.publish({"type": "notice", "level": "ok",
                         "text": "Снимок экрана добавлен в запись — %s"
                                 % obsidian._hms(entry.get("at_s"))})
            hub.publish({"type": "recording", "meta": store.get(rec_id)})

        watcher = platform.system().watch_screenshots(
            rec_id, position_s=lambda: sess.duration, on_shot=on_shot)
        watcher.start()
        _shot_watchers[rec_id] = watcher
    except Exception as err:
        log.warning("наблюдение за снимками экрана не включилось: %s", err)


_level_pump: threading.Thread | None = None
_level_stop = threading.Event()


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


@app.get("/api/recordings/{rec_id}")
async def api_recording_get(rec_id: str) -> JSONResponse:
    return JSONResponse(_recording_payload(rec_id))


@app.patch("/api/recordings/{rec_id}")
async def api_recording_patch(rec_id: str, request: Request) -> JSONResponse:
    body = await request.json()
    patch: dict[str, Any] = {}
    if "title" in body:
        title = (body.get("title") or "").strip()
        if not title:
            raise HTTPException(status_code=400, detail="Пустое название")
        patch["title"] = title[:200]
    if "category" in body:
        patch["category"] = (body.get("category") or "").strip()
    # Проект и теги (17.09): признаки для Obsidian, внутри программы по ним
    # ничего не строится. Введённое запоминается, чтобы в следующий раз подсказать.
    if "project" in body:
        patch["project"] = store.clean_project(body.get("project"))
    if "tags" in body:
        patch["tags"] = store.clean_tags(body.get("tags"))
    if "links" in body:
        patch["links"] = store.clean_links(body.get("links"))
    if "dropped" in body:
        patch["dropped"] = store.clean_dropped(body.get("dropped"))
    # «Продолжение предыдущей записи» (17.09). Связь выбирает человек; на себя
    # и на несуществующую запись не ссылаемся — получилась бы петля или пустота.
    if "continues" in body:
        prev = str(body.get("continues") or "").strip()
        if prev and (prev == rec_id or store.get(prev) is None):
            raise HTTPException(status_code=400,
                                detail="Такой предыдущей записи нет")
        patch["continues"] = prev
    if not patch:
        raise HTTPException(status_code=400, detail="Нечего менять")
    meta = store.update(rec_id, patch)
    if meta is None:
        raise HTTPException(status_code=404, detail="Запись не найдена")
    if patch.get("project"):
        store.remember_project(patch["project"])
    if patch.get("tags"):
        store.remember_tags(patch["tags"])
    # Заметка — выгрузка из программы: признаки должны доехать до шапки файла.
    if ({"project", "tags", "links", "dropped", "continues"} & set(patch)) \
            and str(meta.get("vault_path") or ""):
        try:
            from . import obsidian
            obsidian.refresh_note(rec_id)
        except Exception as err:          # заметка не должна ронять сохранение
            log.warning("заметку %s обновить не вышло: %s", rec_id, err)
    hub.publish({"type": "recording", "meta": meta})
    return JSONResponse(meta)


@app.get("/api/labels")
async def api_labels() -> JSONResponse:
    """Что подсказывать в полях «Проект» и «Теги»: то, что уже вводили."""
    return JSONResponse({"projects": store.known_projects(), "tags": store.known_tags()})


@app.get("/api/tasks/{rec_id}")
async def api_tasks_preview(rec_id: str) -> JSONResponse:
    """Что можно отправить в Todoist и что уже отправлено. Ничего не отправляет."""
    from . import todoist

    try:
        return JSONResponse(await asyncio.to_thread(todoist.preview, rec_id))
    except todoist.TodoistError as err:
        raise HTTPException(status_code=400, detail=str(err)) from None


@app.get("/api/tasks/projects/list")
async def api_tasks_projects() -> JSONResponse:
    """Проекты Todoist. Ходит в сеть, поэтому зовётся только по кнопке."""
    from . import todoist

    try:
        return JSONResponse({"projects": await asyncio.to_thread(todoist.projects)})
    except todoist.TodoistError as err:
        raise HTTPException(status_code=400, detail=str(err)) from None


@app.post("/api/tasks/{rec_id}/send")
async def api_tasks_send(rec_id: str, request: Request) -> JSONResponse:
    """Отправить выбранные поручения в Todoist.

    Единственное действие программы, которое уходит за пределы компьютера и не
    отменяется. Поэтому: без явного confirmed служба отказывает, повторы
    отсекаются отметкой «уже отправлено», объём за раз ограничен.
    """
    from . import todoist

    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Ожидался объект")
    try:
        res = await asyncio.to_thread(
            todoist.send, rec_id,
            [str(t) for t in (body.get("texts") or [])],
            str(body.get("project_id") or ""),
            bool(body.get("confirmed")),
        )
    except todoist.TodoistError as err:
        raise HTTPException(status_code=400, detail=str(err)) from None
    meta = store.get(rec_id)
    if meta is not None:
        hub.publish({"type": "recording", "meta": meta})
    return JSONResponse(res)


@app.get("/api/vault/notes")
async def api_vault_notes(q: str = "") -> JSONResponse:
    """Заметки сейфа по куску названия — для поля «Связать с» (17.09).

    Пустой и однобуквенный запрос отдаёт пустой список намеренно: иначе на
    большом сейфе в окно прилетели бы тысячи строк.
    """
    from . import obsidian

    try:
        found = await asyncio.to_thread(obsidian.find_notes, q)
    except OSError as err:
        raise HTTPException(status_code=400,
                            detail="Сейф Obsidian не читается: %s" % err) from None
    return JSONResponse({"notes": found})


#: Три ответа диалога удаления — три объёма работы.
DELETE_SCOPES = ("all", "media", "history")


@app.delete("/api/recordings/{rec_id}")
async def api_recording_delete(rec_id: str, scope: str = "all") -> JSONResponse:
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
        raise HTTPException(status_code=400,
                            detail="Неизвестный вид удаления: %s" % scope)
    meta = store.get(rec_id)
    if meta is None:
        raise HTTPException(status_code=404, detail="Запись не найдена")
    sess = sessions.get(rec_id)
    if sess is not None and sess.active:
        raise HTTPException(status_code=409, detail="Сначала остановите запись")

    # Удаление файлов и заметки в сейфе — в фоне, чтобы не держать цикл событий.
    loop = asyncio.get_running_loop()
    out = await loop.run_in_executor(None, _delete_recording, rec_id, scope, meta)
    return JSONResponse(out)


def _delete_recording(rec_id: str, scope: str, meta: dict[str, Any]) -> dict[str, Any]:
    from . import obsidian

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
            out["shots_deleted"] = platform.system().delete_shots(rec_id)
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
            raise HTTPException(
                status_code=409,
                detail="Файлы записи занял другой процесс — обычно это открытый "
                       "плеер или антивирус. Закройте его и попробуйте снова.")
        raise HTTPException(status_code=404, detail="Запись не найдена")
    out["freed_bytes"] = freed
    # Что осталось снаружи — говорим по факту, а не по обещанию из диалога.
    out["kept_video"] = bool(meta.get("video_path")) and scope == "history"
    out["kept_note"] = bool(meta.get("vault_path")) and scope == "history"
    hub.publish({"type": "recordings"})
    log.info("запись %s удалена (%s)", rec_id, scope)
    return out


@app.post("/api/recordings/{rec_id}/reveal")
async def api_recording_reveal(rec_id: str, request: Request) -> JSONResponse:
    """Открыть в проводнике папку, где лежит файл записи.

    Открываем ТОЛЬКО папки самой программы: хранилище тяжёлых файлов, сейф
    Obsidian и папку записи. Путь берём не из запроса, а из своего же списка —
    иначе страница могла бы попросить открыть что угодно на диске.
    """
    meta = store.get(rec_id)
    if meta is None:
        raise HTTPException(status_code=404, detail="Запись не найдена")
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    key = str(body.get("what") or "").strip()

    wanted = None
    for item in _rec_paths(rec_id, meta):
        if item["key"] == key:
            wanted = item
            break
    if wanted is None:
        raise HTTPException(status_code=404, detail="Этого файла у записи нет")

    path = Path(wanted["path"])
    # «Открыть заметку в Obsidian» (меню записи): открываем сам
    # файл, а не папку. Путь всё равно из своего списка, так что открыть
    # что-то посторонее страница не может.
    want_file = bool(body.get("open")) and not wanted["is_dir"]
    target = path if want_file else (path if wanted["is_dir"] else path.parent)
    try:
        platform.system().open_path(target)
    except OSError as err:
        raise HTTPException(status_code=500,
                            detail="Не удалось открыть: %s" % err) from None
    log.info("открыто по записи %s: %s", rec_id, target)
    return JSONResponse({"opened": str(target)})


@app.post("/api/recordings/{rec_id}/stop")
async def api_recording_stop(rec_id: str) -> JSONResponse:
    meta = store.get(rec_id)
    if meta is None:
        raise HTTPException(status_code=404, detail="Запись не найдена")
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _stop_device_capture, rec_id)
    status = await loop.run_in_executor(None, sessions.stop, rec_id)

    # Эхо колонок отсеиваем до разметки говорящих: иначе pyannote будет разводить
    # голоса на дорожке, где чужие слова числятся за владельцем микрофона.
    try:
        from . import echo

        marked = await loop.run_in_executor(None, echo.mark, rec_id)
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
    if _calls is not None:
        try:
            _calls.recording_stopped(rec_id)
        except Exception:
            log.debug("автоматика звонков не узнала об остановке", exc_info=True)

    # Разметка сама запускается только если размечать есть что. Случайно нажатый
    # «Старт» даёт запись на пару секунд, и гонять на ней pyannote пять минут
    # незачем — разделять там нечего. Кнопкой «Разметить говорящих» по-прежнему
    # можно запустить вручную на любой записи.
    duration = float((meta or {}).get("duration_s") or 0.0)
    if config.get("diarize_auto") and store.existing_tracks(rec_id):
        if duration < MIN_DIARIZE_SECONDS:
            log.info("запись %s: %.1f c — коротко для разметки, пропускаю",
                     rec_id, duration)
        else:
            try:
                _queue_diarize(rec_id)
            except Exception as err:
                log.warning("автоматическая разметка не запустилась: %s", err)

    return JSONResponse({"meta": meta, "session": status,
                         "segments": _segments(rec_id)})


@app.get("/api/recordings/{rec_id}/audio/{track}")
async def api_recording_audio(rec_id: str, track: str) -> Response:
    if track not in (store.TRACK_MIC, store.TRACK_FAR, store.TRACK_FILE):
        raise HTTPException(status_code=400, detail="Неизвестная дорожка")
    if store.get(rec_id) is None:
        raise HTTPException(status_code=404, detail="Запись не найдена")
    path = store.track_path(rec_id, track)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Дорожки нет")
    return FileResponse(path, media_type="audio/wav")


# ====================================================================== говорящие


# Помощники, которыми пользуются и маршруты, переехали в hagen/api/deps.py.
# Прежние имена оставлены: остальной код
# server.py и проверки продолжают звать _refresh_note и _plural_ru.
from .api.deps import (plural_ru as _plural_ru,  # noqa: E402
                       refresh_note as _refresh_note,
                       refresh_note_async as _refresh_note_async)


# Говорящие и голоса записи переехали в hagen/api/speakers.py вместе со
# своими помощниками: очередь разметки и разделения, память о числе
# голосов, переиспользование готовой разметки.
# Имена здесь сохранены — остальной server.py зовёт их по-прежнему.
from .api.speakers import (drop_media_if_done as _drop_media_if_done,  # noqa: E402
                           queue_diarize as _queue_diarize,
                           queue_split as _queue_split,
                           remember_voices as _remember_voices,
                           reuse_diarization as _reuse_diarization,
                           voices_asked as _voices_asked)


@app.post("/api/recordings/{rec_id}/retranscribe")
async def api_retranscribe(rec_id: str, request: Request) -> JSONResponse:
    """Перечитать точной моделью.

    ``speakers`` в теле — сколько голосов у собеседников (решение 14.09: число
    спрашивается только здесь, и только если человек сам его
    назвал). Названное число означает «ровно столько»: разметка считается
    заново, а не берётся готовая, — иначе указывать число было бы незачем.
    """
    body: dict[str, Any] = {}
    try:
        body = await request.json()
    except Exception:
        pass
    _require_part("precise")          # перечитываем точной моделью — она нужна на месте
    want = _voices_asked(body)
    meta = store.get(rec_id)
    if meta is None:
        raise HTTPException(status_code=404, detail="Запись не найдена")
    if jobs.busy_with("retranscribe", rec_id):
        raise HTTPException(status_code=409, detail="Уже перечитываю")
    tracks = store.existing_tracks(rec_id)
    if not tracks:
        raise HTTPException(status_code=400, detail="Нет сохранённого звука")
    _remember_voices(rec_id, others=want)

    def work(handle) -> dict[str, Any]:
        # Разделение микрофона помним ДО пересборки: реплики собираются заново,
        # и прежнее разделение к ним не относится, но просьба «со мной в комнате
        # были ещё люди» / «меня не было» остаётся в силе (15.09:
        # раньше она молча пропадала, и все реплики снова становились «Я»).
        before = store.get(rec_id) or {}
        # Ручные решения «чьи это слова» помним отрезками времени: реплики
        # пересобираются, а звук остаётся тем же (решение 17.09).
        from . import edits as edits_mod

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
                base = i / float(len(tracks))
                handle.progress(base + frac / float(len(tracks)),
                                "дорожка %s" % _track)

            pieces = asr.transcribe_spans(pcm, spans, precise=True, words=True,
                                          progress=prog)
            from . import fixes

            for p in pieces:
                # Словарь из ручных правок применяем и здесь: текст распознан
                # заново, значит имена и термины опять «как услышала модель».
                seg = store.make_segment(track, p["start"], p["end"],
                                         fixes.apply(p["text"]))
                seg["words"] = fixes.apply_words(p.get("words") or [])
                all_segs.append(seg)

        all_segs.sort(key=lambda s: (float(s["start"]), s["track"]))
        store.replace_segments(rec_id, all_segs)
        # Реплики собраны заново — прежнее разделение голосов к ним не относится.
        store.update(rec_id, {"splits": {}, "room_shared": False})
        # Стенограмма пересобрана с нуля, значит и пометки эха пропали — иначе
        # эхо колонок вернулось бы в текст после каждого переразбора.
        from . import echo

        echo.mark(rec_id)

        # Переразбор меняет только ТЕКСТ реплик, звук остаётся тот же. Поэтому
        # гонять pyannote заново незачем: готовая разметка лежит в
        # diarization.json, и разнести новые реплики по голосам — это сравнение
        # отрезков времени, без нейросети. На записи 9 минут это 5 минут против
        # 0,1 секунды. Если сохранённой разметки нет или звук с тех пор
        # изменился — честно ставим задачу в очередь, как раньше.
        reused = False if want else _reuse_diarization(rec_id, handle)
        # Ручные решения кладём ПОСЛЕ разметки: человек главнее модели.
        # Текст правок не возвращается — он распознан заново, счётчик обнуляем.
        back = edits_mod.apply_hand_marks(rec_id, hand)
        if back:
            handle.log("ручные решения о говорящих вернулись на %d реплик" % back)
        store.update(rec_id, {"edits_count": 0})
        visible = store.sorted_segments(rec_id)
        if not reused:
            store.update(rec_id, {"diarized": False, "diarize_status": "none",
                                  "speakers": {}})
        hub.publish({"type": "recording", "meta": store.get(rec_id)})
        hub.publish({"type": "segments", "rec_id": rec_id, "segments": visible})
        hub.publish({"type": "notice", "level": "ok",
                     "text": "Перечитано точной моделью: %d реплик" % len(visible)})
        _refresh_note(rec_id, "Перечитать точнее")

        # Число назвали — размечаем даже при выключенной авторазметке: человек
        # попросил об этом сам.
        if not reused and (want or config.get("diarize_auto")):
            try:
                _queue_diarize(rec_id, want or None, want or None)
            except Exception:
                pass
        # Микрофон был разделён — делим заново уже новые реплики. «Я» встанет
        # по образцу, остальные голоса узнаются по базе.
        resplit = False
        if (mic_split or mic_absent) and store.TRACK_MIC in tracks:
            from . import speakers

            if speakers.segments_of(rec_id, "me"):
                store.update(rec_id, {"room_shared": mic_split, "owner_absent": mic_absent})
                try:
                    _queue_split(rec_id, "me", mic_voices if not mic_absent else 0)
                    resplit = True
                    handle.log("микрофон был разделён — разделяю голоса заново")
                except Exception as err:
                    detail = getattr(err, "detail", None) or str(err)
                    log.warning("запись %s: микрофон заново не разделился: %s", rec_id, detail)
                    store.update(rec_id, {"room_shared": False, "owner_absent": False})
                    hub.publish({"type": "notice", "level": "err",
                                 "text": "Голоса микрофона заново не разделились: %s" % detail})
                hub.publish({"type": "recording", "meta": store.get(rec_id)})
        return {"segments": len(visible), "diarization_reused": reused,
                "speakers": want or None, "mic_resplit": resplit}

    job_id = jobs.submit("retranscribe", work,
                         "Перечитать точнее: %s" % meta.get("title"), rec_id=rec_id)
    return JSONResponse({"job_id": job_id})


# ====================================================================== файлы


def _transcribe_media(rec_id: str, src: Path, name: str, handle,
                      base_progress: float = 0.0) -> dict[str, Any]:
    """Извлечь звук из медиафайла и распознать его целиком.

    Общий путь для файла, перетащенного в окно, и для видео, скачанного по
    ссылке: дальше извлечения звука разницы между ними нет. base_progress
    сдвигает шкалу — у скачивания часть полосы уже занята загрузкой.
    """
    span = 1.0 - base_progress

    handle.log("извлекаю звук из файла")
    dst = store.track_path(rec_id, store.TRACK_FILE)
    duration = audio_io.ffmpeg_to_wav16k(src, dst)
    store.update(rec_id, {"duration_s": round(duration, 2),
                          "status": "processing",
                          "tracks": [store.TRACK_FILE]})
    handle.progress(base_progress + span * 0.05, "звук извлечён, %.0f c" % duration)

    pcm, _sr = audio_io.read_wav(dst)
    spans = vad.split_for_asr(pcm)
    handle.log("фрагментов речи: %d" % len(spans))
    # скорость точной модели ~0.15 от реального времени
    handle.eta(max(5.0, duration * 0.18))

    segs: list[dict[str, Any]] = []

    def prog(frac):
        handle.progress(base_progress + span * (0.05 + 0.85 * frac),
                        "распознано %.0f%%" % (frac * 100))
        handle.eta(max(0.0, duration * 0.18 * (1.0 - frac)))

    pieces = asr.transcribe_spans(pcm, spans, precise=True, words=True, progress=prog)
    for p in pieces:
        seg = store.make_segment(store.TRACK_FILE, p["start"], p["end"], p["text"],
                                 speaker="Участник", speaker_key="file")
        seg["words"] = p.get("words") or []
        segs.append(seg)
    store.replace_segments(rec_id, segs)
    store.update(rec_id, {"status": "recorded"})
    handle.progress(base_progress + span * 0.95, "готовлю результат")
    hub.publish({"type": "segments", "rec_id": rec_id, "segments": segs})
    hub.publish({"type": "recording", "meta": store.get(rec_id)})
    hub.publish({"type": "notice", "level": "ok",
                 "text": "Распознано: %s (%d реплик)" % (name, len(segs))})
    try:
        src.unlink(missing_ok=True)
    except Exception:
        pass
    if config.get("diarize_auto"):
        try:
            _queue_diarize(rec_id)
        except Exception:
            pass
    return {"segments": len(segs), "duration_s": duration}


# Старые точки /api/upload и /api/fetch удалены намеренно: и файл, и ссылка
# обрабатываются теперь одним конвейером в media.py через /api/media/*. Две
# дороги к одному делу означали бы два разных поведения и двойную починку.


# ====================================================================== видео


def _media_ready() -> None:
    """Связать конвейер видео со службой. Вызывается один раз при импорте."""
    from . import media

    media.hooks["publish"] = hub.publish
    media.hooks["queue_diarize"] = _queue_diarize


@app.post("/api/recordings/{rec_id}/minutes")
async def api_minutes(rec_id: str, request: Request) -> JSONResponse:
    body = await request.json()
    template = (body.get("template") or "protocol").strip()
    question = (body.get("question") or "").strip() or None
    engine = (body.get("engine") or "").strip() or None
    meta = store.get(rec_id)
    if meta is None:
        raise HTTPException(status_code=404, detail="Запись не найдена")
    return JSONResponse({"job_id": _submit_minutes(rec_id, meta, template, question, engine)})


def _submit_minutes(rec_id: str, meta: dict[str, Any], template: str,
                    question: str | None, engine: str | None) -> str:
    """Протокол или ответ на вопрос — задачей в очереди."""
    from . import minutes as _minutes

    if template not in _minutes.TEMPLATES:
        raise HTTPException(status_code=400, detail="Неизвестный документ: %s" % template)
    if template == "question" and not (question or "").strip():
        raise HTTPException(status_code=400, detail="Напишите вопрос")
    if jobs.busy_with("minutes", rec_id):
        raise HTTPException(status_code=409, detail="Документ по этой записи уже готовится")
    info = _minutes.DOC_KINDS[template]

    def work(handle) -> dict[str, Any]:
        from . import minutes, obsidian

        handle.log("отправляю стенограмму")
        res = minutes.generate(rec_id, template, question=question,
                               engine=engine, handle=handle)
        # У каждого документа свой раздел заметки, и в него уходит файл документа.
        # Ответы на вопросы копятся: в раздел идёт весь файл вопросов.
        section = minutes.stored_document(rec_id, template) or res["markdown"]
        try:
            saved = obsidian.append_minutes(rec_id, section, heading=info["heading"])
            res["vault_path"] = saved.get("path")
        except Exception as err:
            # Документ готов и лежит в программе, но в заметку не попал — сказать
            # вслух, а не только строчкой задачи (15.09).
            handle.log("в Obsidian не записалось: %s" % err)
            log.warning("запись %s: документ не вписан в заметку Obsidian: %s", rec_id, err)
            hub.publish({"type": "notice", "level": "err",
                         "text": "Документ готов, но в Obsidian не записался: %s" % err})
        hub.publish({"type": "minutes", "rec_id": rec_id,
                     "markdown": res["markdown"], "template": template})
        hub.publish({"type": "recording", "meta": store.get(rec_id)})
        hub.publish({"type": "notice", "level": "ok",
                     "text": "Ответ на вопрос готов" if template == "question" else "Протокол готов"})
        return {"engine": res.get("engine"), "chunks": res.get("chunks"),
                "elapsed_s": res.get("elapsed_s")}

    retry_body: dict[str, Any] = {"template": template}
    if question:
        retry_body["question"] = question
    return jobs.submit("minutes", work, "%s: %s" % (info["title"], meta.get("title")),
                       rec_id=rec_id,
                       extra={"engine": engine or config.get("minutes_engine"),
                              "retry": {"url": "/api/recordings/%s/minutes" % rec_id,
                                        "body": retry_body}})


@app.post("/api/recordings/{rec_id}/document")
async def api_document(rec_id: str, request: Request) -> JSONResponse:
    """«Сделать документ»: один вход для протокола, саммари, конспекта, выжимки и вопроса.

    video_kind — тип записи, выбранный в окне: запоминается в записи, чтобы в
    следующий раз окно предложило тот же документ.
    """
    from . import media, minutes

    body: dict[str, Any] = {}
    try:
        body = await request.json()
    except Exception:
        pass
    doc = str(body.get("doc") or "").strip()
    if doc not in minutes.DOC_KINDS:
        raise HTTPException(status_code=400, detail="Неизвестный документ: %s" % doc)
    engine = str(body.get("engine") or "").strip() or None
    if engine not in (None, "claude_cli", "api"):
        raise HTTPException(status_code=400, detail="Неизвестный движок: %s" % engine)
    meta = store.get(rec_id)
    if meta is None:
        raise HTTPException(status_code=404, detail="Запись не найдена")
    kind = str(body.get("video_kind") or "").strip()
    if kind in media.KINDS and kind != meta.get("video_kind"):
        meta = store.update(rec_id, {"video_kind": kind}) or meta
        hub.publish({"type": "recording", "meta": meta})
    if doc in ("protocol", "question"):
        job_id = _submit_minutes(rec_id, meta, doc, str(body.get("question") or "").strip() or None,
                                 engine)
    else:
        if jobs.busy_with("summary", rec_id):
            raise HTTPException(status_code=409, detail="Документ по этой записи уже готовится")
        job_id = media.submit_summary(rec_id, document=doc, engine=engine)
    return JSONResponse({"job_id": job_id})


@app.get("/api/recordings/{rec_id}/documents")
async def api_documents(rec_id: str) -> JSONResponse:
    """Все готовые документы записи — для вкладок над текстом документа."""
    from . import minutes

    meta = store.get(rec_id)
    if meta is None:
        raise HTTPException(status_code=404, detail="Запись не найдена")
    return JSONResponse({"documents": minutes.stored_documents(rec_id),
                         "last": meta.get("last_document")})


@app.get("/api/documents/kinds")
async def api_document_kinds(rec_id: str = "") -> JSONResponse:
    """Что показать в окне «Сделать документ»."""
    from . import media, minutes

    meta = store.get(rec_id) if rec_id else None
    kinds = [
        {"key": "meeting", "title": "Встреча — несколько участников",
         "hint": "Разговор нескольких людей: решения, задачи, договорённости."},
        {"key": "lecture", "title": "Лекция / соло-подкаст",
         "hint": "Говорит один человек: объясняет, рассказывает."},
        {"key": "interview", "title": "Интервью или подкаст",
         "hint": "Разговор двух-трёх человек без задач и решений."},
        {"key": "transcript", "title": "Только расшифровка",
         "hint": "Для видео: после обработки ничего не делать сверх текста. "
                 "Документ можно сделать и здесь, вручную."},
    ]
    kinds = [k for k in kinds if k["key"] in media.KINDS]
    return JSONResponse({
        "docs": [{"key": k, "title": v["title"], "hint": v["hint"]}
                 for k, v in minutes.DOC_KINDS.items()],
        "kinds": kinds,
        "video_kind": (meta or {}).get("video_kind") or "meeting",
        "default_doc": minutes.default_document(meta or {}),
        "source": (meta or {}).get("source") or "live",
        "shots": len((meta or {}).get("screenshots") or []),
        "warning": minutes.cloud_warning(config.get("minutes_engine") or "claude_cli"),
    })


@app.get("/api/prompts")
async def api_prompts_get() -> JSONResponse:
    """Инструкции документов для «Настройки → Обработка»."""
    from . import minutes

    return JSONResponse({
        "owner_name": store.owner_name(),
        "prompt_lang": minutes.prompt_lang(),
        # Токен наружу не отдаём: только «задан» и хвостик (правило секретов).
        "todoist_set": bool(str(config.get("todoist_token") or "").strip()),
        "todoist_hint": config._mask(config.get("todoist_token") or ""),
        "prompts": [{"key": k, "title": v["title"], "hint": v["hint"],
                     "default": minutes.DEFAULT_TASKS[k].strip(),
                     "current": minutes.task_text(k).strip(),
                     "overridden": minutes.task_overridden(k)}
                    for k, v in minutes.DOC_KINDS.items()],
    })


@app.post("/api/prompts")
async def api_prompts_post(request: Request) -> JSONResponse:
    """Сохранить имя владельца и свои «задачи и структуры» документов.

    Текст, совпадающий с исходным, не хранится — тогда документ идёт по
    исходной инструкции и получит её будущие исправления.
    """
    from . import minutes

    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Ожидался объект")
    patch: dict[str, Any] = {}
    if "owner_name" in body:
        name = " ".join(str(body.get("owner_name") or "").split()).strip()[:60]
        patch["owner_name"] = name or store.SPEAKER_ME
    over = body.get("overrides")
    if isinstance(over, dict):
        current = dict(config.get("prompt_overrides") or {})
        for key, text in over.items():
            if key not in minutes.DOC_KINDS:
                continue
            text = str(text or "").strip()
            if not text or text == minutes.DEFAULT_TASKS[key].strip():
                current.pop(key, None)
            else:
                current[key] = text[:20000]
        patch["prompt_overrides"] = current
    # Язык инструкций (17.09): всё, кроме «en», считаем русским — молча сохранить
    # неизвестное значение значило бы получить промпт неизвестно на чём.
    # Токен Todoist. Пустая строка — осознанное «убрать токен», поэтому отличаем
    # «поле не прислали» от «прислали пустым». Пустая строка от интерфейса, где
    # поле просто не трогали, до сюда не доходит: там подставляется признак.
    if "todoist_token" in body:
        patch["todoist_token"] = str(body.get("todoist_token") or "").strip()
    if "prompt_lang" in body:
        patch["prompt_lang"] = "en" if str(body.get("prompt_lang") or "").strip().lower() == "en" else "ru"
    if patch:
        config.save(patch)
        hub.publish({"type": "settings", "settings": config.public()})
    return await api_prompts_get()


@app.get("/api/recordings/{rec_id}/minutes")
async def api_minutes_get(rec_id: str) -> JSONResponse:
    """Готовый документ записи: у видео это саммари, у совещания — протокол."""
    from . import minutes

    text = minutes.load_minutes(rec_id)
    if not text:
        raise HTTPException(status_code=404, detail="Документа ещё нет")
    meta = store.get(rec_id) or {}
    return JSONResponse({"markdown": text,
                         "kind": meta.get("last_document") or meta.get("doc_kind") or "minutes"})


@app.get("/api/minutes/templates")
async def api_minutes_templates() -> JSONResponse:
    try:
        from . import minutes

        return JSONResponse({"templates": minutes.TEMPLATES,
                             "engines": minutes.available_engines(),
                             "warning": minutes.cloud_warning(
                                 config.get("minutes_engine") or "claude_cli")})
    except Exception as err:
        raise HTTPException(status_code=500, detail="Модуль протоколов недоступен: %s" % err)


# ====================================================================== хранилище


@app.post("/api/recordings/{rec_id}/save")
async def api_save_note(rec_id: str) -> JSONResponse:
    from . import obsidian

    # Сейф лежит на Яндекс.Диске, запись может ждать занятый файл до 1,5 с —
    # из цикла событий это останавливало бы всю службу. Пишем в фоне.
    loop = asyncio.get_running_loop()
    try:
        res = await loop.run_in_executor(None, obsidian.save_note, rec_id)
    except Exception as err:
        # Раньше неудача оставалась только красной подсказкой на экране — в журнале
        # не было и следа, и потом нельзя было понять, что случилось (15.09).
        log.warning("запись %s: заметка в Obsidian не сохранилась: %s", rec_id, err)
        raise HTTPException(status_code=500, detail="Не удалось сохранить в Obsidian: %s" % err)
    meta = store.get(rec_id)
    hub.publish({"type": "recording", "meta": meta})
    hub.publish({"type": "notice", "level": "ok",
                 "text": "Сохранено в Obsidian: %s" % res.get("relative")})
    return JSONResponse(res)


# Сторожа «нужна скачанная часть» и «идёт запись» переехали в api/deps.py:
# их зовут и маршруты видео, и остальной server.py.
from .api.deps import (no_recording_now as _no_recording_now,  # noqa: E402
                       require_media_parts as _require_media_parts,
                       require_part as _require_part)


@app.get("/api/needs")
async def api_needs() -> JSONResponse:
    """Тяжёлые части: что уже на месте, а что придётся скачать (решение 16.09).

    В сборку они не кладутся: точная модель, английская модель и браузер для
    SharePoint нужны не всем и не сразу, а весят больше гигабайта вместе.
    """
    from . import needs

    return JSONResponse({"parts": needs.state()})


@app.post("/api/needs/{key}")
async def api_needs_install(key: str) -> JSONResponse:
    """Скачать часть. Задачей в очереди: видно ход работы, и можно остановить."""
    from . import needs

    if key not in needs.PARTS:
        raise HTTPException(status_code=404, detail="Неизвестная часть: %s" % key)
    if jobs.busy_with("needs", key):
        raise HTTPException(status_code=409, detail="Эта часть уже скачивается")
    title = needs.PARTS[key]["title"]

    def work(handle) -> dict[str, Any]:
        res = needs.install(key, note=handle.log)
        hub.publish({"type": "needs", "parts": needs.state()})
        hub.publish({"type": "notice", "level": "ok",
                     "text": "%s — на месте." % title})
        return res

    return JSONResponse({"job_id": jobs.submit("needs", work, "Скачиваю: %s" % title,
                                               rec_id=key),
                         "title": title})


@app.get("/api/ytdlp")
async def api_ytdlp() -> JSONResponse:
    """Версия yt-dlp: им качаются ссылки, YouTube и субтитры источников."""
    from . import fetch

    return JSONResponse({"version": fetch.version()})


@app.post("/api/ytdlp/update")
async def api_ytdlp_update() -> JSONResponse:
    """«Обновить yt-dlp»: площадки меняют выдачу, помогает свежая версия (16.09).

    Обновление идёт задачей в очереди: pip зовётся библиотекой в своём процессе
    (Kaspersky не даёт порождать фоновые), и это занимает до минуты.
    """
    from . import fetch

    if jobs.busy_with("ytdlp"):
        raise HTTPException(status_code=409, detail="Обновление уже идёт")

    def work(handle) -> dict[str, Any]:
        res = fetch.update(note=handle.log)
        text = ("yt-dlp обновлён: %s → %s. Ссылки пойдут через новую версию."
                % (res.get("before") or "не было", res.get("after"))
                if res.get("changed") else
                "yt-dlp уже свежий: %s" % (res.get("after") or "версия не определилась"))
        hub.publish({"type": "notice", "level": "ok", "text": text})
        return res

    return JSONResponse({"job_id": jobs.submit("ytdlp", work, "Обновление yt-dlp")})


@app.get("/api/vault")
async def api_vault() -> JSONResponse:
    return JSONResponse({"status": _vault_status(), "categories": _categories()})


@app.post("/api/vault/category")
async def api_vault_category(request: Request) -> JSONResponse:
    from . import obsidian

    body = await request.json()
    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Пустое имя категории")
    cats = obsidian.add_category(name)
    hub.publish({"type": "settings", "settings": config.public()})
    return JSONResponse({"categories": cats})


@app.post("/api/vault/category/remove")
async def api_vault_category_remove(request: Request) -> JSONResponse:
    """Убрать категорию из списка программы. Папка в сейфе и заметки остаются."""
    from . import obsidian

    body = await request.json()
    name = str((body or {}).get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Не указана категория")
    if name not in obsidian.list_categories():
        raise HTTPException(status_code=404, detail="Такой категории в списке нет")
    if len(obsidian.list_categories()) <= 1:
        raise HTTPException(status_code=400, detail="Последнюю категорию убрать нельзя")
    cats = obsidian.remove_category(name)
    hub.publish({"type": "settings", "settings": config.public()})
    return JSONResponse({"categories": cats, "default": config.get("default_category")})


@app.post("/api/vault/init")
async def api_vault_init() -> JSONResponse:
    from . import obsidian

    return JSONResponse(obsidian.ensure_vault())


# ====================================================================== голоса


# ====================================================================== прочее


@app.get("/api/jobs")
async def api_jobs() -> JSONResponse:
    return JSONResponse(jobs.list_all())


@app.post("/api/jobs/{job_id}/cancel")
async def api_job_cancel(job_id: str) -> JSONResponse:
    return JSONResponse({"cancelled": jobs.cancel(job_id)})


@app.get("/api/meeting")
async def api_meeting() -> JSONResponse:
    try:
        desktop = platform.desktop()
        meeting = desktop.current_meeting()
        return JSONResponse({"meeting": meeting,
                             "outlook": desktop.outlook_kind()})
    except Exception as err:
        return JSONResponse({"meeting": None, "error": str(err)})


@app.get("/api/call")
async def api_call() -> JSONResponse:
    state = dict(_call_state)
    if _watcher is not None:
        try:
            state["watcher"] = _watcher.state
        except Exception:
            pass
    return JSONResponse(state)


@app.get("/api/prompt")
async def api_prompt() -> JSONResponse:
    """Текущий вопрос автоматики звонков (или null)."""
    return JSONResponse({"prompt": _calls.prompt if _calls is not None else None})


@app.post("/api/prompt/{prompt_id}")
async def api_prompt_answer(prompt_id: str, request: Request) -> JSONResponse:
    """Ответ на вопрос кнопкой в окне. Уведомление Windows зовёт то же напрямую."""
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    if _calls is None:
        raise HTTPException(status_code=409, detail="Наблюдение за звонками выключено")
    answer = str(body.get("answer") or "").strip()
    loop = asyncio.get_running_loop()
    # Ответ может остановить запись, а это ожидание корутин службы: из потока
    # событий так нельзя — он сам себя бы и ждал.
    res = await loop.run_in_executor(None, _calls.answer, prompt_id, answer, "ui")
    return JSONResponse(res)


@app.post("/api/call/dismiss")
async def api_call_dismiss(request: Request) -> JSONResponse:
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    seconds = float(body.get("seconds") or 900)
    _call_state.update({"asked": False})
    if _watcher is not None:
        try:
            _watcher.snooze(seconds)
        except Exception:
            pass
    return JSONResponse({"ok": True})


@app.api_route("/api/devices", methods=["GET", "POST"])
async def api_devices(request: Request, probe: bool = False) -> JSONResponse:
    """Устройства записи и, по запросу, результат короткой пробы каждого.

    Проба открывает микрофоны и петлю на секунду — это действие, поэтому
    только POST (со своей страницы); простой GET отдаёт лишь список.
    """
    sound = platform.audio()

    probe = bool(probe) and request.method == "POST"
    out: dict[str, Any] = {}
    loop = asyncio.get_running_loop()
    try:
        # Список берём оттуда же, откуда идёт запись, — из WASAPI. Раньше он
        # строился по DirectShow (ffmpeg), а пишет службa через WASAPI: имена в
        # этих двух списках совпадают не всегда, и выбранное в интерфейсе
        # устройство могло не найтись при записи. Заодно «по умолчанию» теперь
        # берётся у Windows, а не приписывается первой строке списка.
        mics = await loop.run_in_executor(None, sound.list_input_devices)
        out["mics"] = [{"index": d["index"], "name": d["name"],
                        "channels": d["channels"],
                        "is_default": bool(d.get("is_default")),
                        "is_communications": bool(d.get("is_communications"))}
                       for d in mics]
        if not out["mics"]:
            legacy = await loop.run_in_executor(None, sound.list_ffmpeg_devices, True)
            out["mics"] = [{"index": i, "name": d["name"], "alt_name": d["alt_name"],
                            "is_default": i == 0}
                           for i, d in enumerate(legacy) if d.get("kind") == "mic"]
        out["loopback"] = await loop.run_in_executor(None, sound.list_loopback_devices)
        out["roles"] = await loop.run_in_executor(None, sound.default_render_devices)
        out["mic_selected"] = config.get("mic_device_name")
        out["far_selected"] = config.get("far_device_index")
        if probe:
            # Пробуем КАЖДЫЙ микрофон, а не только выбранный. На машине легко
            # оказаться с «микрофоном» колонок или виртуальным устройством от
            # Steam: они исправно открываются и отдают ровные нули. Пока не
            # послушаешь каждый, отличить живой вход от пустышки нельзя, и
            # человек узнаёт об этом, когда его уже не услышали на совещании.
            for m in out["mics"]:
                try:
                    res = await loop.run_in_executor(None, sound.probe_input, m["index"], 0.7)
                except Exception as err:
                    res = {"ok": False, "reason": str(err)[:200]}
                peak = float(res.get("peak") or 0.0) if res.get("ok") else 0.0
                m["probe"] = res
                # Ровный ноль — устройство-пустышка, тут сомнений нет. А вот
                # еле слышный шум АЦП (у «микрофона» колонок это 0,002) живым
                # микрофоном называть нельзя: человек поверит и уйдёт на
                # совещание с неработающим входом. Такие помечаем «тихо» —
                # пусть скажет что-нибудь и проверит ещё раз.
                m["silent"] = bool(res.get("ok") and peak <= 0.0)
                m["alive"] = bool(res.get("ok") and peak > 0.002)

            # Пробуем тем же путём, которым пишем: иначе проба может показать
            # «всё хорошо» на устройстве, которое при записи даже не откроется.
            idx = config.get("mic_device_index")
            idx = int(idx) if idx is not None else await loop.run_in_executor(
                None, sound.recommended_input_index)
            out["mic_probe"] = (await loop.run_in_executor(None, sound.probe_input, idx, 1.0)
                                if idx is not None
                                else {"ok": False, "reason": "микрофон не найден"})
            out["far_probe"] = await loop.run_in_executor(
                None, sound.probe_loopback_process, config.get("far_device_index"), 1.2)
    except Exception as err:
        out.setdefault("mics", [])
        out.setdefault("loopback", [])
        out["error"] = str(err)
    return JSONResponse(out)


@app.post("/api/audio-diag")
async def api_audio_diag() -> JSONResponse:
    """Подробно: что видит служба в звуковом стеке и что мешает открыть запись."""
    loop = asyncio.get_running_loop()
    try:
        data = await loop.run_in_executor(None, platform.audio().diagnose)
    except Exception as err:
        raise HTTPException(status_code=500, detail="диагностика не удалась: %s" % err)
    return JSONResponse(data)


@app.post("/api/ffmpeg-diag")
async def api_ffmpeg_diag() -> JSONResponse:
    loop = asyncio.get_running_loop()
    data = await loop.run_in_executor(None, platform.audio().diag_ffmpeg, 0.6)
    return JSONResponse(data)


@app.get("/api/health")
async def api_health() -> JSONResponse:
    active = sessions.active_session()
    return JSONResponse({
        "ok": True,
        "recording": active.rec_id if active else None,
        "jobs_active": len(jobs.active()),
    })


# ====================================================================== WebSocket


@app.websocket("/ws/events")
async def ws_events(ws: WebSocket) -> None:
    await ws.accept()
    await hub.add(ws)
    try:
        await ws.send_text(json.dumps({"type": "hello", "ts": time.time()},
                                      ensure_ascii=False))
        while True:
            msg = await ws.receive_text()
            if msg == "ping":
                await ws.send_text("pong")
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        await hub.remove(ws)


# Роутеры подключаются ПОСЛЕДНЕЙ строкой модуля: к этому моменту в server.py
# уже созданы рассылка, очередь задач и всё, что роутеры берут у общих модулей.
# Так разорван круг «server импортирует роутеры, роутеры импортируют server».
from .api import include_all  # noqa: E402

include_all(app)


def create_app() -> FastAPI:
    return app
