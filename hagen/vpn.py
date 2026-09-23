# -*- coding: utf-8 -*-
"""Локальный VPN-клиент как выход в сеть: порты, выбор прокси, проба выхода.

Зачем это ядру. Документы пишет Claude CLI, а серверы Anthropic отказывают
запросам из части стран. Запрос, ушедший с «неправильного» адреса, всё равно
несёт токен аккаунта и текст стенограммы, и на той стороне аккаунт оказывается
связан с этим адресом. Поэтому перед запуском CLI программа решает, каким
путём он пойдёт, и без уверенности не запускает его вовсе.

Что здесь знает программа:
  * типовые локальные порты VPN-клиентов (Hiddify, Happ, v2rayN, Nekoray) и
    какой из них какой протокол принимает — CLI понимает только HTTP-прокси;
  * слушает ли порт — проверка без единого пакета наружу;
  * проба выхода: с какого адреса и из какой страны уходят запросы и пускает
    ли Anthropic с него. Проба идёт БЕЗ токена: на той стороне виден только
    адрес без аккаунта.

Сюда же смотрит скачивание видео по ссылке (fetch.py) за списком портов, чтобы
он был один.
"""
from __future__ import annotations

import logging
import re
import socket
from dataclasses import dataclass
from typing import Any

log = logging.getLogger("hagen.vpn")


@dataclass(frozen=True)
class Port:
    """Локальный порт VPN-клиента: номер, чей он и что принимает."""
    number: int
    client: str
    http: bool          # принимает HTTP CONNECT — годится для Claude CLI
    socks: bool         # принимает SOCKS5 — годится для yt-dlp
    note: str = ""


#: Типовые порты. Порядок — порядок перебора: сначала клиенты, у которых
#: смешанный порт (HTTP и SOCKS вместе), затем раздельные.
PORTS: tuple[Port, ...] = (
    Port(12334, "Hiddify", http=True, socks=True, note="смешанный порт"),
    Port(10809, "Happ, v2rayN", http=True, socks=False, note="порт HTTP-прокси"),
    Port(2080, "Nekoray", http=True, socks=True, note="смешанный порт"),
    Port(10808, "Happ, v2rayN", http=False, socks=True, note="порт SOCKS5"),
)

#: Сколько ждать соединения с локальным портом. Localhost отвечает мгновенно:
#: либо слушает, либо сразу «отказано».
LISTEN_TIMEOUT_S = 0.3
#: Сколько ждать пробы выхода в сеть — одного запроса.
PROBE_TIMEOUT_S = 8.0

_ADDR_RE = re.compile(r"^(?:http://)?(?P<host>[A-Za-z0-9.\-]+):(?P<port>\d{2,5})/?$")


def listening(port: int, host: str = "127.0.0.1") -> bool:
    """Слушает ли кто-нибудь локальный порт. Наружу ничего не уходит."""
    try:
        with socket.create_connection((host, int(port)), timeout=LISTEN_TIMEOUT_S):
            return True
    except OSError:
        return False


def parse_proxy(text: str) -> tuple[str, int] | None:
    """Разобрать адрес прокси из настройки: «127.0.0.1:12334», можно с http://."""
    m = _ADDR_RE.match((text or "").strip())
    if not m:
        return None
    port = int(m.group("port"))
    if not 1 <= port <= 65535:
        return None
    return m.group("host"), port


def scan_ports() -> list[dict[str, Any]]:
    """Все известные порты и слушает ли их кто-нибудь сейчас — для показа."""
    return [{"port": p.number, "client": p.client, "note": p.note, "http": p.http,
             "socks": p.socks, "listening": listening(p.number)} for p in PORTS]


def find_cli_proxy(setting: str) -> dict[str, Any]:
    """Через какой локальный порт пускать Claude CLI.

    setting — «auto» (первый слушающий из известных HTTP-портов) либо адрес
    «хост:порт». Возвращает url (пусто, если пускать не через что), название
    клиента и listening — слушает ли порт. Наружу ничего не уходит.
    """
    text = (setting or "auto").strip()
    if text.lower() != "auto":
        parsed = parse_proxy(text)
        if not parsed:
            return {"url": "", "client": "", "listening": False,
                    "error": "Адрес прокси не разобран: нужен вид 127.0.0.1:12334."}
        host, port = parsed
        known = next((p for p in PORTS if p.number == port), None)
        return {"url": "http://%s:%d" % (host, port),
                "client": known.client if known else "прокси из настройки",
                "listening": listening(port, host)}
    for p in PORTS:
        if p.http and listening(p.number):
            return {"url": "http://127.0.0.1:%d" % p.number, "client": p.client,
                    "listening": True}
    return {"url": "", "client": "", "listening": False}


def env_through(env: dict[str, str], url: str) -> dict[str, str]:
    """Окружение дочерней программы, в котором наружу ведёт только этот прокси.

    Унаследованные переменные прокси снимаются: иначе клиент в режиме
    системного прокси мог бы увести запрос в обход выбранного порта.
    Свой адрес (localhost) остаётся прямым — на всякий случай, CLI туда не ходит.
    """
    out = dict(env)
    for key in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY",
                "ALL_PROXY", "all_proxy"):
        out.pop(key, None)
    out["HTTPS_PROXY"] = out["HTTP_PROXY"] = url
    out["NO_PROXY"] = out["no_proxy"] = "127.0.0.1,localhost,::1"
    return out


def yt_proxies() -> list[str]:
    """Адреса для yt-dlp в порядке перебора: SOCKS там, где он есть, иначе HTTP."""
    out: list[str] = []
    for p in PORTS:
        if p.socks:
            out.append("socks5://127.0.0.1:%d" % p.number)
        elif p.http:
            out.append("http://127.0.0.1:%d" % p.number)
    return out


# ---------------------------------------------------------------- проба выхода

#: Cloudflare отвечает текстом «ключ=значение»: адрес и страна, без регистрации.
#: Anthropic сам стоит за Cloudflare, так что путь до него тот же.
TRACE_URL = "https://www.cloudflare.com/cdn-cgi/trace"
#: Вопрос к Anthropic без ключа. Из разрешённой страны ответ — 401 «нет
#: ключа», из запрещённой — 403 ещё до проверки ключа. Аккаунт в запросе не
#: участвует: на той стороне только адрес.
ANTHROPIC_URL = "https://api.anthropic.com/v1/models"

# Проба намеренно не через providers._client: тот нарочно не знает прокси
# (trust_env=False, без proxy), а здесь весь смысл — пойти ровно тем путём,
# каким пойдёт CLI: через заданный порт либо «как есть» с переменными
# окружения.


def _client(proxy: str | None, as_is: bool):
    import httpx

    kw: dict[str, Any] = {
        "timeout": httpx.Timeout(PROBE_TIMEOUT_S, connect=PROBE_TIMEOUT_S),
        "follow_redirects": False,
        "trust_env": bool(as_is),
    }
    if proxy:
        kw["proxy"] = proxy
    return httpx.Client(**kw)


def parse_trace(text: str) -> dict[str, str]:
    """Разобрать ответ Cloudflare: ip=…, loc=… — в словарь."""
    out: dict[str, str] = {}
    for line in (text or "").splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def anthropic_verdict(status: int | None) -> str:
    """«open» — пускает, «blocked» — отказ по региону, «unknown» — не понять."""
    if status == 401:
        return "open"
    if status == 403:
        return "blocked"
    return "unknown"


def probe_egress(proxy: str | None = None, as_is: bool = False) -> dict[str, Any]:
    """С какого адреса уходят запросы и пускает ли Anthropic.

    proxy — через этот адрес; as_is — как есть, с переменными окружения (так
    пойдёт CLI без нашего вмешательства). Токен не участвует.
    """
    out: dict[str, Any] = {"ip": "", "country": "", "anthropic": "unknown",
                           "status": None, "error": ""}
    try:
        with _client(proxy, as_is) as cli:
            try:
                r = cli.get(TRACE_URL)
                trace = parse_trace(r.text)
                out["ip"] = trace.get("ip", "")
                out["country"] = trace.get("loc", "")
            except Exception as err:
                out["error"] = "Cloudflare не ответил: %s" % str(err)[:160]
            r = cli.get(ANTHROPIC_URL)
            out["status"] = r.status_code
            out["anthropic"] = anthropic_verdict(r.status_code)
    except Exception as err:
        # Порт есть, а туннеля за ним нет; либо сети нет вовсе. Наружу при
        # этом ничего не ушло — соединение не состоялось.
        out["error"] = (out["error"] + "; " if out["error"] else "") + str(err)[:160]
    return out
