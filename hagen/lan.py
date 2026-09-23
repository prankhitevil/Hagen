# -*- coding: utf-8 -*-
"""Страница для телефона в локальной сети: QR-код в настройках, файл — в обработку.

Телефон и компьютер в одной сети Wi-Fi. В настройках показан QR-код со
ссылкой вида `http://192.168.1.10:8788/?k=…`; телефон открывает её в браузере,
выбирает запись — и она уходит в те же входящие, что и файл из папки
(`inbox.accept`). Приложения на телефоне не нужно.

Отдельная служба, а не второй адрес у основной. Основная слушает только
127.0.0.1 и отвечает только своей странице (проверка Host и Origin в
`server.GuardMiddleware`) — открывать её в сеть целиком нельзя: там все
стенограммы. Здесь узкая дверь на своём порту: страница, проба связи и приём
файла, всё — только с ключом из ссылки. Ключ случайный, живёт в настройках,
наружу не отдаётся; «Новый ключ» делает старую ссылку недействительной.

Без HTTPS: сертификата для адреса в домашней сети взять негде, а ключ в
ссылке защищает от случайного соседа по сети. Кнопка «Поделиться → Hagen»
из диктофона телефона по этой же причине невозможна (браузер требует HTTPS) —
остаётся «открыть страницу и выбрать файл».
"""
from __future__ import annotations

import hmac
import logging
import secrets
import socket
import threading
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response

from . import audio_io, config

log = logging.getLogger("hagen.lan")

PAGE = Path(__file__).resolve().parent / "static" / "phone.html"

#: Сколько ждать, пока служба поднимется на порту.
START_WAIT_S = 6.0


# ---------------------------------------------------------------- ключ и ссылка


def key() -> str:
    """Ключ из ссылки. Нет — завести."""
    cur = str(config.get("lan_key") or "")
    return cur or new_key()


def new_key() -> str:
    """Новый ключ: старая ссылка и старый QR-код перестают работать."""
    fresh = secrets.token_urlsafe(9)
    config.save({"lan_key": fresh})
    return fresh


def key_ok(given: str | None) -> bool:
    cur = str(config.get("lan_key") or "")
    if not cur:
        return False
    # Сравнение байтами: строковое сравнение не принимает не-ASCII, а в
    # адресной строке может оказаться что угодно.
    return hmac.compare_digest(str(given or "").encode("utf-8"), cur.encode("utf-8"))


def _rank(ip: str) -> int:
    """Порядок адресов: домашние Wi-Fi обычно 192.168.x, потом 10.x, потом 172.16–31.x.

    VPN-клиент с туннелем тоже даёт адрес (10.x или 172.x) — он для телефона
    бесполезен, поэтому идёт после «домашнего», а выбрать нужный можно в
    настройках.
    """
    parts = ip.split(".")
    try:
        a, b = int(parts[0]), int(parts[1])
    except (ValueError, IndexError):
        return 9
    if a == 192 and b == 168:
        return 0
    if a == 10:
        return 1
    if a == 172 and 16 <= b <= 31:
        return 2
    if a == 169 and b == 254:
        return 8            # адрес «сеть не выдала» — почти наверняка мимо
    return 3


def addresses() -> list[str]:
    """Адреса IPv4 этого компьютера, без петли, лучшие первыми."""
    found: set[str] = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            found.add(info[4][0])
    except OSError as err:
        log.warning("адреса компьютера не прочитались: %s", err)
    found.discard("0.0.0.0")
    ips = [ip for ip in found if not ip.startswith("127.")]
    return sorted(ips, key=lambda ip: (_rank(ip), ip))


def address(known: list[str] | None = None) -> str:
    """Адрес для ссылки: выбранный в настройках, если он ещё есть, иначе первый."""
    ips = addresses() if known is None else list(known)
    chosen = str(config.get("lan_address") or "").strip()
    if chosen and chosen in ips:
        return chosen
    return ips[0] if ips else ""


def port() -> int:
    try:
        p = int(config.get("lan_port") or 8788)
    except (TypeError, ValueError):
        p = 8788
    return p if 1024 <= p <= 65535 else 8788


def url(addr: str | None = None) -> str:
    addr = address() if addr is None else addr
    if not addr:
        return ""
    return "http://%s:%d/?k=%s" % (addr, port(), key())


def qr_svg(link: str) -> str:
    """QR-код ссылки картинкой SVG для страницы настроек."""
    if not link:
        return ""
    import segno

    return segno.make(link, error="m").svg_inline(scale=6, border=1)


# ---------------------------------------------------------------- приложение

lan_app = FastAPI(title="Hagen — с телефона", docs_url=None, redoc_url=None, openapi_url=None)


@lan_app.middleware("http")
async def _no_store(request: Request, call_next):     # noqa: ANN001
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


_DENIED = ("<!doctype html><meta charset='utf-8'><meta name='viewport' content='width=device-width'>"
           "<title>Hagen</title><p style='font:18px system-ui;margin:2em'>Нет доступа. Откройте "
           "страницу по QR-коду из настроек Hagen (раздел «С телефона»).</p>")


def _require_key(k: str | None) -> None:
    if not key_ok(k):
        raise HTTPException(status_code=403, detail="Нет доступа: ссылка без ключа или ключ сменился.")


@lan_app.get("/")
async def page(k: str = "") -> Response:
    if not key_ok(k):
        return HTMLResponse(_DENIED, status_code=403)
    if not PAGE.exists():
        return HTMLResponse("<h1>Страница не собрана</h1>", status_code=500)
    return FileResponse(str(PAGE), media_type="text/html; charset=utf-8")


@lan_app.get("/ping")
async def ping(k: str = "") -> JSONResponse:
    """Проба связи со страницы: ключ подходит, компьютер отвечает."""
    _require_key(k)
    return JSONResponse({"ok": True, "name": "Hagen"})


@lan_app.post("/upload")
async def upload(file: UploadFile, k: str = Form("")) -> JSONResponse:
    """Файл с телефона → входящие: распознать и разметить, без документа."""
    from . import inbox
    from .api.media import save_upload

    _require_key(k)
    name = Path(file.filename or "запись").name
    if not audio_io.is_media(name):
        raise HTTPException(status_code=400,
                            detail="Такой формат не поддерживается: %s" % (Path(name).suffix or "без расширения"))
    tmp = await save_upload(file, name)
    try:
        res = inbox.accept(tmp, name, "phone")
    except Exception as err:      # noqa: BLE001 — телефону нужен ответ, какой бы ни была причина
        tmp.unlink(missing_ok=True)
        log.warning("файл с телефона не принят: %s", err)
        raise HTTPException(status_code=500, detail="Не удалось поставить в обработку: %s" % err) from None
    return JSONResponse({"ok": True, "name": name, "rec_id": res.get("rec_id")})


# ---------------------------------------------------------------- служба на порту

_lock = threading.Lock()
_server: Any = None
_thread: threading.Thread | None = None
_port: int | None = None
_error = ""


def _run(server: Any) -> None:
    global _error
    try:
        server.run()
    except BaseException as err:      # noqa: BLE001 — порт занят: uvicorn выходит через SystemExit
        _error = "служба не поднялась: %s" % (err or "порт занят")
        log.warning("страница для телефона: %s", _error)


def running() -> bool:
    t = _thread
    return bool(t is not None and t.is_alive() and _server is not None and getattr(_server, "started", False))


def wanted() -> bool:
    return bool(config.feature("phone") and config.get("lan_enabled"))


def start(on_port: int | None = None) -> bool:
    """Поднять службу на порту. True — слушает."""
    global _server, _thread, _port, _error
    import uvicorn

    with _lock:
        if running():
            return True
        _error = ""
        p = int(on_port or port())
        cfg = uvicorn.Config(lan_app, host="0.0.0.0", port=p, log_level="warning",
                             access_log=False, timeout_graceful_shutdown=5)
        _server = uvicorn.Server(cfg)
        _port = p
        _thread = threading.Thread(target=_run, args=(_server,), name="hagen-lan", daemon=True)
        _thread.start()
        deadline = time.time() + START_WAIT_S
        while time.time() < deadline:
            if _server.started:
                log.info("страница для телефона слушает порт %d", p)
                return True
            if not _thread.is_alive():
                break
            time.sleep(0.05)
        if not _error:
            _error = "служба не поднялась на порту %d (занят другой программой?)" % p
        _server = None
        return False


def stop() -> None:
    global _server, _thread, _port
    with _lock:
        srv, t = _server, _thread
        _server, _thread, _port = None, None, None
        if srv is None:
            return
        srv.should_exit = True
        if t is not None and t.is_alive():
            t.join(timeout=START_WAIT_S)


def apply() -> bool:
    """Привести службу в согласие с настройками: поднять, остановить, сменить порт."""
    want = wanted()
    if running() and (not want or _port != port()):
        stop()
    if want and not running():
        return start()
    return running()


def state() -> dict[str, Any]:
    """Что показать в настройках: адреса, ссылка, QR-код, слушает ли порт."""
    ips = addresses()
    addr = address(ips)
    link = url(addr) if wanted() else ""
    return {
        "enabled": bool(config.get("lan_enabled")),
        "feature": config.feature("phone"),
        "running": running(),
        "port": port(),
        "addresses": ips,
        "address": addr,
        "url": link,
        "qr_svg": qr_svg(link),
        "error": _error if wanted() and not running() else "",
        "key_hint": config.mask(key()) if wanted() else "",
    }
