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
import contextlib
import json
import logging
import threading
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from . import asr, config, dictation, diarize_jobs, jobs, platform, recordings, reread, store
from .api import deps as api_deps

log = logging.getLogger("hagen.server")

STATIC_DIR = Path(__file__).resolve().parent / "static"
UPLOAD_DIR = config.DATA_DIR / "_uploads"

SAFE_HOSTS = {"127.0.0.1", "localhost", "[::1]", "::1"}


# ====================================================================== события


# Рассылка событий переехала в hagen/events.py, чтобы её могли брать и
# роутеры. Имена здесь сохранены: остальной код
# и проверки продолжают обращаться к server.hub и server.EventHub.
from .events import EventHub, hub  # noqa: E402  (после импортов модуля)


def _live_yield_reason() -> str:
    """Пока идёт запись, разметка и распознавание файлов стоят (решение 22.09).

    Процессор — звонку: иначе у живой записи запаздывал текст, а запись
    собеседников не поднималась по минуте. Настройка читается при каждом
    вопросе: переключили посреди паузы — задача пойдёт через секунду.
    Разметка в помощнике на паузу не встаёт, но по этому же ответу помощник
    переходит в режим эффективности.
    """
    if config.get("processing_during_recording") == "run":
        return ""
    return "идёт запись" if recordings.sessions.active_session() is not None else ""


jobs.set_yield_rule(_live_yield_reason)


def _job_changed(job: dict[str, Any]) -> None:
    public = {
        k: job.get(k) for k in
        ("id", "kind", "title", "rec_id", "status", "progress", "eta_s", "note",
         "error", "cancel_requested", "error_kind", "resets_at", "retry",
         "created_at", "finished_at", "paused")
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
            diarize_jobs.drop_media_if_done(rec_id)
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

@contextlib.asynccontextmanager
async def _lifespan(_app: FastAPI):
    """Запуск и остановка службы — одним контекстом, как просит FastAPI.

    Остановка — в `finally`: при жёстком выходе сервер бросает отмену прямо в
    это место, и без него остались бы занятыми звуковые устройства и горячая
    клавиша диктовки.
    """
    await _startup()
    try:
        yield
    finally:
        await _shutdown()


app = FastAPI(title="Hagen", docs_url=None, redoc_url=None, openapi_url=None,
              lifespan=_lifespan)
app.add_middleware(GuardMiddleware)


@app.middleware("http")
async def no_store(request: Request, call_next):
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


async def _startup() -> None:
    config.ensure_dirs()
    # Разовый перенос возможностей с прежней установки: у того, кто обновился,
    # функции уже настроены, и молча пропасть они не должны (п. 9.2).
    try:
        config.adopt_used_features()
    except Exception as err:
        log.warning("возможности не перенеслись: %s", err)
    # Так же разово: прежний список сервисов → адрес, ключ и модель.
    try:
        from . import providers

        providers.adopt_old_services()
    except Exception as err:
        log.warning("сервисы не перенеслись: %s", err)
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    _sweep_uploads()
    hub.bind_loop(asyncio.get_running_loop())
    recordings.recover_orphans()
    log.info("служба запущена на 127.0.0.1:%s", config.get("port"))
    # Служба поднялась — если это первая работа новой версии, откатывать её
    # не надо (hagen/updates.py). Итог прошлого обновления — в журнал.
    try:
        from . import updates

        updates.confirm_started()
        last = updates.status().get("last") or {}
        if last and not last.get("seen"):
            log.info("обновление %s → %s: %s%s", last.get("from"), last.get("to"),
                     last.get("phase"), (" (%s)" % last["error"]) if last.get("error") else "")
    except Exception as err:
        log.warning("состояние обновления не прочиталось: %s", err)
    # Модели, которые прежняя программа держала, а выбор в настройках их уже не
    # просит, — удалить до прогрева: сейчас их никто не держит.
    try:
        from . import needs

        done = needs.cleanup_pending()
        if done:
            log.info("отложенное удаление моделей: %s", ", ".join(done))
    except Exception as err:
        log.warning("отложенное удаление моделей не удалось: %s", err)
    asyncio.get_running_loop().run_in_executor(None, _warmup_async)
    _start_call_watcher()
    _start_idle_watch()
    _start_retention()
    dictation.start()


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


def _run_retention() -> dict[str, Any]:
    """Удалить звук звонков старше срока из настроек (если срок задан)."""
    from . import storage

    res = storage.purge(busy=recordings.busy)
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
    """Где лежит звук записей и видео и сколько занимает (для «Настройки → Общие»)."""
    from . import storage

    loop = asyncio.get_running_loop()
    audio = await loop.run_in_executor(None, storage.audio_usage)
    assets = await loop.run_in_executor(None, storage.assets_usage)
    days = storage.retention_days()
    due = storage.expired(days, busy=recordings.busy) if days else []
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


@app.post("/api/pick-folder")
async def api_pick_folder(request: Request) -> JSONResponse:
    """Кнопка «Выбрать…» у поля с папкой: окно выбора, как в Проводнике.

    Путь только возвращается — в поле его кладёт и сохраняет страница, так же
    как вписанный руками. None — человек закрыл окно, ничего не выбрав.
    """
    body = await request.json()
    title = str((body or {}).get("title") or "Выбор папки").strip()[:120]
    start = str((body or {}).get("start") or "").strip()[:1000] or None
    loop = asyncio.get_running_loop()
    try:
        path = await loop.run_in_executor(None, platform.shell().pick_folder, title, start)
    except Exception as err:
        raise HTTPException(status_code=500, detail="Окно выбора папки не открылось (%s). "
                                                    "Путь можно вписать в поле." % err) from None
    return JSONResponse({"path": path})


@app.post("/api/storage/purge")
async def api_storage_purge() -> JSONResponse:
    """«Удалить сейчас» — звук звонков старше срока, не дожидаясь проверки по расписанию."""
    loop = asyncio.get_running_loop()
    res = await loop.run_in_executor(None, _run_retention)
    return JSONResponse(res)


def _warmup_async() -> None:
    try:
        # Точную модель на старте не греем — решение 14.09:
        # она вместе с torch занимает около 1,3 ГБ, а нужна не всегда. Грузится
        # при первом обращении (файл, «Перечитать точнее», диктовка) и
        # выгружается после простоя (asr.release_idle, «precise_idle_min»).
        # Диктовка начинает загрузку сама, как только человек начал говорить.
        # Исключение — режим одной модели (18.09): там точная и есть модель
        # эфира, её греет прогрев эфира, и сторож простоя её не трогает.
        info = asr.warmup(live=True, precise=False)
        log.info("модель эфира прогрета: %s", info)
        hub.publish({"type": "ready", "asr": asr.live_state()})
    except Exception as err:
        log.error("прогрев модели не удался: %s", err)
        hub.publish({"type": "ready",
                     "asr": {"state": "error", "error": str(err)[:300], "seconds": None}})


async def _shutdown() -> None:
    recordings.shutdown()
    _stop_call_watcher()
    dictation.stop()


# ====================================================================== детект звонка

_watcher = None
_calls = None      # calls.CallAutomation — звонок → запись


def _call_start_recording(meeting: dict[str, Any] | None) -> str:
    """Звонок начался — запись как кнопкой «Старт», с именем по встрече."""
    body: dict[str, Any] = {"category": config.get("default_category")}
    if meeting:
        body["meeting"] = meeting
    return str(recordings.start(body)["meta"]["id"])


def _call_public() -> dict[str, Any]:
    """Состояние звонка для страницы — то, что видит автоматика."""
    state = dict(_calls.call) if _calls is not None else {"active": False}
    state.setdefault("asked", False)
    return state


def shell_hooks() -> platform.base.ShellHooks:
    """Что значку у часов и уведомлениям можно просить у службы (розетка «Оболочка»)."""
    def add_listener(fn: Any) -> None:
        autom = _ensure_calls()
        if autom is not None:
            autom.add_listener(fn)

    def answer(prompt_id: str, button: str) -> None:
        autom = _ensure_calls()
        if autom is not None:
            autom.answer(prompt_id, button, source="toast")

    return platform.base.ShellHooks(
        active_recording=recordings.active_id,
        start_recording=lambda: str(recordings.start()["meta"]["id"]),
        stop_recording=recordings.stop_and_save,
        add_prompt_listener=add_listener,
        answer_prompt=answer,
    )


def _ensure_calls() -> Any:
    """Автоматика вопросов. Нужна и без наблюдения за звонками (20.09).

    Через неё же спрашивается про забытую запись, а она бывает начата кнопкой —
    когда звонки программа вообще не слушает.
    """
    global _calls
    if _calls is not None:
        return _calls
    try:
        from . import calls
    except Exception as err:
        log.info("автоматика вопросов недоступна: %s", err)
        return None
    _calls = calls.CallAutomation(calls.Hooks(
        active_recording=recordings.active_id,
        start_recording=_call_start_recording,
        stop_recording=recordings.stop_and_save,
        discard_recording=recordings.discard,
        merge_and_resume=recordings.merge_and_resume,
        publish=hub.publish,
    ))
    recordings.on_stopped(_calls.recording_stopped)
    return _calls


def _start_idle_watch() -> None:
    """Сторож забытой записи (20.09): разговор кончился, а запись идёт."""
    if float(config.get("idle_stop_min") or 0) <= 0:
        return
    autom = _ensure_calls()
    if autom is None:
        return
    try:
        autom.start_idle_watch()
        log.info("сторож забытой записи включён")
    except Exception as err:
        log.info("сторож забытой записи не включился: %s", err)


def _start_call_watcher() -> None:
    global _watcher
    if not config.get("call_watch_enabled"):
        return
    if _ensure_calls() is None:
        return

    def on_start(info: dict[str, Any]) -> None:
        meeting = None
        try:
            if config.get("outlook_enabled"):
                # Вместе с другими встречами того же времени: в уведомлении о
                # звонке будет кнопка переключиться на них (решение 22.09).
                meeting = platform.desktop().current_meeting(with_alternatives=True)
        except Exception as err:
            log.debug("встречу Outlook прочитать не вышло: %s", err)
        _calls.on_call_start(info, meeting)
        hub.publish({"type": "call", "call": _call_public()})

    def on_end(info: dict[str, Any]) -> None:
        _calls.on_call_end(info)
        hub.publish({"type": "call", "call": _call_public()})

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
    if _calls is not None:
        try:
            _calls.stop_idle_watch()
        except Exception:
            pass


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


@app.get("/api/state")
async def api_state() -> JSONResponse:
    # Сбор состояния ходит в звуковой поток, к сейфу на Яндекс.Диске и ищет
    # Claude CLI — из цикла событий это замораживало всю службу на секунды
    # при каждом «Старте» и «Стопе». Считаем в фоне.
    loop = asyncio.get_running_loop()
    payload = await loop.run_in_executor(None, _state_payload)
    return JSONResponse(payload)


def _state_payload() -> dict[str, Any]:
    active = recordings.sessions.active_session()
    payload = {
        "recordings": store.list_all(),
        "settings": config.public(),
        "jobs": jobs.list_all(20),
        "active_recording": active.rec_id if active is not None else None,
        "session": active.status() if active is not None else None,
        "call": _call_public(),
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
        caps["diarize_note"] = "модуль разметки голосов недоступен: %s" % err
    try:
        from . import minutes

        from . import providers as _prov

        caps["claude_cli"] = bool(minutes.resolve_claude_cli())
        # Для сводки «Готов к записи»: слушает ли порт VPN. Сети не касается.
        caps["claude_cli_net"] = minutes.cli_network_state() if caps["claude_cli"] else ""
        caps["engines"] = minutes.available_engines()
        caps["minutes_models"] = minutes.models_hint()
        caps["connections"] = _prov.public_connections()
        caps["models_cached"] = {role: _prov.cached_models(role) or {} for role in _prov.ROLES}
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
    global _caps_cache
    for derived in ("hf_token_set", "hf_token_hint", "api_keys_set", "api_keys_hint",
                    "diarize_engine", "diarize_needs_token"):
        patch.pop(derived, None)
    config.save(patch)
    # Готовность движков и подключений зависит от настроек: после сохранения
    # окно должно видеть новую, а не ту, что держится в кэше ещё 20 с.
    with _caps_lock:
        _caps_cache = None
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
        recordings.sync_mic_pill(recordings.active_id() is not None)
    if any(k.startswith("dictate_") for k in patch):
        # Сочетание клавиш занимается и освобождается сразу, а не при следующем
        # запуске: иначе человек поменял бы клавишу и решил, что она не работает.
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, dictation.start)
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


# ====================================================================== записи


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
    loop = asyncio.get_running_loop()
    try:
        return JSONResponse(await loop.run_in_executor(None, recordings.start, body))
    except (LookupError, ValueError, jobs.Busy) as err:
        raise api_deps.as_http(err) from None


@app.get("/api/recordings/{rec_id}")
async def api_recording_get(rec_id: str) -> JSONResponse:
    try:
        return JSONResponse(recordings.payload(rec_id))
    except LookupError as err:
        raise api_deps.as_http(err) from None


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
    # Роли участников В ЭТОЙ записи (20.09): постоянная роль живёт у человека в
    # базе голосов, здесь — только то, что в этот раз иначе.
    if "roles" in body:
        patch["roles"] = store.clean_roles(body.get("roles"))
    # Голосовые заметки (20.09): правятся и убираются из карточки записи.
    if "notes" in body:
        patch["notes"] = store.clean_notes(body.get("notes"))
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
    if ({"project", "tags", "links", "dropped", "continues", "notes"} & set(patch)) \
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
    """Что подсказывать в полях «Проект» и «Теги»: то, что уже вводили.

    Вместе с ними — папки проектов в сейфе: карточке записи надо показать, куда
    уедет заметка, когда у проекта есть связанная папка (20.09).
    """
    return JSONResponse({"projects": store.known_projects(),
                         "tags": store.known_tags(),
                         "folders": store.known_project_folders()})


@app.post("/api/projects/folder")
async def api_project_folder(request: Request) -> JSONResponse:
    """Привязать папку сейфа к проекту или снять связку (пустая папка).

    Связка живёт у проекта, а не у записи: проект один на все свои записи.
    Заметки переезжают не сразу, а когда каждая запись сохраняется заново, —
    файлы в сейфе программа двигает только по ходу своей обычной работы.
    """
    body = await request.json()
    try:
        folders = store.set_project_folder(body.get("project"), body.get("folder"))
    except ValueError as err:
        raise HTTPException(status_code=400, detail=str(err)) from None
    return JSONResponse({"folders": folders})


@app.get("/api/recordings/{rec_id}/share")
async def api_share_preview(rec_id: str) -> JSONResponse:
    """Что у записи можно отправить. Ничего не отправляет и не открывает."""
    from . import share

    try:
        return JSONResponse(await asyncio.to_thread(share.available, rec_id))
    except ValueError as err:
        raise HTTPException(status_code=404, detail=str(err)) from None


@app.post("/api/recordings/{rec_id}/share")
async def api_share(rec_id: str, request: Request) -> JSONResponse:
    """Открыть письмо с отмеченным или окно Telegram (20.09).

    Письмо программа только ПОКАЗЫВАЕТ: отправка наружу необратима, и
    последнее слово за человеком — как и с задачами в Todoist.
    """
    from . import share

    body = await request.json()
    keys = [str(k) for k in (body.get("keys") or [])]
    # Текст для Telegram собирает страница — там же, где живёт его разметка.
    text = str(body.get("text") or "")
    try:
        res = await asyncio.to_thread(share.send, rec_id, body.get("target"), keys, text)
    except ValueError as err:
        raise HTTPException(status_code=400, detail=str(err)) from None
    return JSONResponse(res)


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


@app.get("/api/vault/folders")
async def api_vault_folders(q: str = "") -> JSONResponse:
    """Папки сейфа — для выбора папки проекта (20.09).

    В отличие от заметок, пустой запрос отдаёт список: папок немного, и папку
    проекта выбирают глазами.
    """
    from . import obsidian

    try:
        found = await asyncio.to_thread(obsidian.find_folders, q)
    except OSError as err:
        raise HTTPException(status_code=400,
                            detail="Сейф Obsidian не читается: %s" % err) from None
    return JSONResponse({"folders": found})


@app.delete("/api/recordings/{rec_id}")
async def api_recording_delete(rec_id: str, scope: str = "all") -> JSONResponse:
    """Удалить запись: всё, только звук и видео или только из программы (recordings.delete)."""
    # Удаление файлов и заметки в сейфе — в фоне, чтобы не держать цикл событий.
    loop = asyncio.get_running_loop()
    try:
        out = await loop.run_in_executor(None, recordings.delete, rec_id, scope)
    except (LookupError, ValueError, jobs.Busy, recordings.Locked) as err:
        raise api_deps.as_http(err) from None
    return JSONResponse(out)


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
    for item in recordings.rec_paths(rec_id, meta):
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
    loop = asyncio.get_running_loop()
    try:
        return JSONResponse(await loop.run_in_executor(None, recordings.stop, rec_id))
    except LookupError as err:
        raise api_deps.as_http(err) from None


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


@app.post("/api/recordings/{rec_id}/retranscribe")
async def api_retranscribe(rec_id: str, request: Request) -> JSONResponse:
    """Перечитать точной моделью (reread.submit).

    ``speakers`` в теле — сколько голосов у собеседников назвал человек.
    """
    from .api.speakers import voices_asked

    body: dict[str, Any] = {}
    try:
        body = await request.json()
    except Exception:
        pass
    api_deps.require_part(asr.precise_part())  # перечитываем точной моделью — она нужна на месте
    try:
        job_id = reread.submit(rec_id, voices_asked(body))
    except (LookupError, ValueError, jobs.Busy) as err:
        raise api_deps.as_http(err) from None
    return JSONResponse({"job_id": job_id})


# Старые точки /api/upload и /api/fetch удалены намеренно: и файл, и ссылка
# обрабатываются одним конвейером в media.py через /api/media/*. Две дороги к
# одному делу означали бы два разных поведения и двойную починку.


# ====================================================================== документы


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
                       lane=_minutes.document_lane(engine),
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
    """Инструкции документов для «Настройки → Документы»."""
    from . import minutes

    from . import voices

    return JSONResponse({
        "owner_name": store.owner_name(),
        # Своя роль (20.09): сторона и должность владельца записи. Список
        # сторон один на всю программу и живёт в базе голосов.
        "owner_side": voices.clean_side(config.get("owner_side")),
        "owner_position": voices.clean_position(config.get("owner_position")),
        "sides": [{"key": k, "title": v[0]} for k, v in voices.SIDES.items()],
        "prompt_lang": minutes.prompt_lang(),
        # Токен наружу не отдаём: только «задан» и хвостик (правило секретов).
        "todoist_set": bool(str(config.get("todoist_token") or "").strip()),
        "todoist_hint": config.mask(config.get("todoist_token") or ""),
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
    if "owner_side" in body or "owner_position" in body:
        from . import voices

        patch["owner_side"] = voices.clean_side(body.get("owner_side"))
        patch["owner_position"] = voices.clean_position(body.get("owner_position"))
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
    state = _call_public()
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
        if out["mics"]:
            # Какой микрофон подставится, если в списке выбрано «авто»: страница
            # называет его по имени, а решает это тот же выбор, что и при записи.
            try:
                auto = await loop.run_in_executor(None, sound.recommended_input_index)
            except Exception:
                auto = None
            for m in out["mics"]:
                m["recommended"] = auto is not None and m["index"] == auto
        else:
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
    return JSONResponse({
        "ok": True,
        "recording": recordings.active_id(),
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
