# -*- coding: utf-8 -*-
"""SharePoint и OneDrive: записи встреч Teams и готовые транскрипты к ним.

Что здесь происходит и почему именно так.

ВХОД. Обычного «логина с паролем» у Microsoft для такой программы нет, а секрет
приложения в открытом коде хранить нельзя. Поэтому вход идёт по коду устройства
(device code): служба просит у Microsoft короткий код, показывает его человеку
вместе со ссылкой, человек подтверждает вход в браузере под своей учётной
записью — служба получает токен. Ничего секретного в программе не лежит:
идентификаторы приложения и тенанта ниже — это не пароли, они публичные.

ДВА ТОКЕНА. Поиск и скачивание идут через Graph (graph.microsoft.com), а вот
транскрипт Teams-записи живёт в Stream и достаётся только через SharePoint REST
v2.1, у которого своя «аудитория» токена — сам хост SharePoint, а не Graph.
Второй токен берём молча, по refresh token того же входа: заново подтверждать
код не нужно.

ГДЕ ЖИВУТ ТОКЕНЫ. Только в памяти процесса. На диск (в settings.json и куда бы
то ни было ещё) не пишем: refresh token — это полный доступ к почте и файлам
человека. Цена решения — вход живёт до перезапуска службы, после чего надо
войти заново; так и говорим человеку.

ВСТРЕЧА БЕЗ ЗАПИСИ. Если встречу не записывали, но расшифровку включали, Teams
всё равно создаёт mp4-контейнер нулевого размера — просто чтобы прицепить к нему
транскрипт. Такую «запись» узнаём по размеру меньше мегабайта: видео не качаем,
работаем по одному транскрипту.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from .. import audio_io, config, platform
from ..obsidian import _clean_url

log = logging.getLogger("hagen.sources.sharepoint")

# Страховка на случай запуска не через run.py: библиотека httpx на уровне INFO
# пишет в журнал полный адрес запроса, а в ссылках скачивания лежит tempauth.
for _noisy in ("httpx", "httpcore"):
    if logging.getLogger(_noisy).level < logging.WARNING:
        logging.getLogger(_noisy).setLevel(logging.WARNING)

#: Короткое имя источника для интерфейса.
SOURCE_NAME = "sharepoint"

# Идентификаторы приложения и тенанта перенесены из саммаризатора как есть.
# Это НЕ секреты: client_id публичен по устройству протокола device code, а
# секрета приложения у нас нет вовсе — вход подтверждает человек в браузере.
SP_CLIENT_ID = "4c56dd18-6ce6-4a72-9243-f316d91f8da4"
SP_TENANT_ID = "28764fed-263a-40a4-97e5-a2d82f7a0049"

_AUTH = "https://login.microsoftonline.com/%s/oauth2/v2.0" % SP_TENANT_ID
_GRAPH = "https://graph.microsoft.com/v1.0"

#: Скоуп входа. offline_access нужен ради refresh token: без него второй токен
#: (к хосту SharePoint) не получить, а значит не забрать транскрипт из Stream.
_SCOPE = "https://graph.microsoft.com/.default offline_access"

UA = "Mozilla/5.0"


# Таймауты раздельные. Соединение должно устанавливаться быстро — если за десять
# секунд не вышло, дальше ждать бессмысленно. А чтение длинное: запись встречи
# на пару часов весит под гигабайт и качается не минуту.
CONNECT_TIMEOUT_S = 10.0
READ_TIMEOUT_S = 60.0
LONG_READ_TIMEOUT_S = 900.0

#: Сколько раз переспрашивать Graph Search при его же временных отказах.
SEARCH_RETRIES = 5
_RETRY_CODES = (429, 500, 502, 503, 504)

#: Размер страницы выдачи и общий потолок. Раньше бралась одна страница на 200
#: находок, и всё, что дальше, молча терялось; теперь листаем, пока Search
#: говорит «есть ещё» (moreResultsAvailable).
SEARCH_PAGE = 200
MAX_RESULTS = 1000

#: Запись встречи Teams: так Teams называет файл, либо файл лежит в папке
#: «Recordings». В старом проекте из 349 найденных видео встреч было около 9 —
#: остальное футбол, камеры и учебные ролики, поэтому по умолчанию фильтруем.
MEETING_NAME_RE = re.compile(r"meeting[ _-]*recording|запись[ _-]*(собрания|встречи)", re.I)
#: Настоящая дата встречи в имени файла Teams: «…-20260515_150245-…».
#: Дата изменения файла от неё отличается: расшифровку дописывают позже.
_MEETING_DATE_RE = re.compile(r"(20\d{2})(\d{2})(\d{2})_(\d{2})(\d{2})(\d{2})")

#: Меньше мегабайта у mp4 — это пустой контейнер Teams, а не видео.
EMPTY_CONTAINER_BYTES = 1_048_576

TRANSCRIPT_EXTS = (".vtt", ".srt")
VIDEO_EXT_KQL = ["mp4", "m4v", "mov", "mkv", "webm"]
TRANSCRIPT_EXT_KQL = ["vtt", "srt"]
_HIT_RE = re.compile(r"\.(mp4|m4v|mov|mkv|webm|vtt|srt)$", re.I)

__all__ = [
    "begin_login", "wait_login", "logged_in", "logout",
    "search", "fetch", "resolve_link", "item_label",
]


# --------------------------------------------------------------------------- мелочи

class _Cancelled(RuntimeError):
    """Отмена пользователем.

    Отдельный класс нужен ровно для одного: отмену нельзя спутать с сетевой
    ошибкой. Иначе прерванное скачивание уходило бы в запасной путь и качало
    файл заново — вместо того чтобы остановиться.
    """


def _progress(handle: Any, value: float, note: str = "") -> None:
    if handle is None:
        return
    try:
        handle.progress(max(0.0, min(1.0, float(value))), note)
    except Exception:
        pass


def _note(handle: Any, msg: str) -> None:
    log.info("%s", msg)
    if handle is None:
        return
    try:
        handle.log(msg)
    except Exception:
        pass


def _cancelled(handle: Any) -> bool:
    if handle is None:
        return False
    try:
        return bool(handle.cancelled)
    except Exception:
        return False


def _check(handle: Any) -> None:
    if _cancelled(handle):
        raise _Cancelled("отменено")


def _sleep(seconds: float, handle: Any = None) -> None:
    """Пауза, которая слышит отмену: спим короткими кусками."""
    left = float(seconds)
    while left > 0:
        _check(handle)
        time.sleep(min(0.5, left))
        left -= 0.5


_URL_IN_TEXT = re.compile(r"https?://\S+")
# Токены Microsoft — это JWT: три части через точку, первая начинается с eyJ.
_JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-.]+")


def _safe(value: Any, limit: int = 200) -> str:
    """Текст, пригодный для лога и для показа человеку.

    Наружу уходит и в интерфейс, и в лог-файл, поэтому вычищаем всё секретное:
    преавторизованные ссылки SharePoint несут в запросе параметр tempauth (это
    полноценный пропуск к файлу), а тела ответов Microsoft — сами токены.
    Ссылки не рубим целиком, а чистим тем же способом, что и заметки Obsidian.
    """
    text = str(value or "")
    text = _URL_IN_TEXT.sub(lambda m: _clean_url(m.group(0)) or "<ссылка скрыта>", text)
    text = _JWT_RE.sub("<токен скрыт>", text)
    return " ".join(text.split())[:limit]


def direct_env() -> dict[str, str]:
    """Окружение для дочернего ffmpeg/ffprobe — БЕЗ системного прокси.

    Перенесено из саммаризатора и выстрадано там же. Локальный VPN-клиент
    выставляет http_proxy/https_proxy на свой порт; дочерний процесс их читает и
    уходит через VPN. Дальше две беды сразу: рвётся TLS, а преавторизованная
    ссылка SharePoint перестаёт работать — её выписали на прямой IP, и с другого
    адреса SharePoint отдаёт отказ. Наши запросы по той же причине идут с
    trust_env=False.
    """
    env = dict(os.environ)
    for key in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY",
                "ALL_PROXY", "all_proxy"):
        env.pop(key, None)
    env["NO_PROXY"] = env["no_proxy"] = "*"
    return env


_BAD_CHARS = re.compile(r'[\\/:*?"<>|]')


def _slug(text: str, maxlen: int = 60) -> str:
    """Имя файла, которое переживёт Windows."""
    s = _BAD_CHARS.sub("_", str(text or ""))
    s = re.sub(r"_+", "_", s).strip(" ._")
    return s[:maxlen].strip(" ._") or "meeting"


def _is_transcript_name(name: str) -> bool:
    return str(name or "").lower().endswith(TRANSCRIPT_EXTS)


# Teams называет транскрипт по-разному: «Имя.vtt», «Имя-transcript.vtt»,
# «Имя.ru-ru.vtt», «Имя.mp4.vtt». Сводим имя к «ядру», чтобы находить пару
# к видео. Языки перечислены списком не от лени: шаблон «любые две буквы»
# съедал у «Standup-hr» кусок названия.
_TAIL_NOISE = re.compile(
    r"(?:[ _.\-]*(?:transcript|транскрипт|субтитры|captions?)"
    r"|[ _.\-](?:ru|en|uk|kk|de|fr|es|zh)(?:[-_][a-z]{2})?"
    r"|\.(?:mp4|m4v|mov|mkv|webm|vtt|srt))$", re.I)


def _match_key(name: str) -> str:
    """Имя файла -> ключ сопоставления видео и транскрипта."""
    s = str(name or "").strip()
    prev = None
    while s and s != prev:          # хвосты снимаются по одному: «a.mp4.ru-ru.vtt» -> «a»
        prev = s
        s = _TAIL_NOISE.sub("", s)
    return s.strip(" ._-").lower()


# --------------------------------------------------------------------------- сеть

def _client(read_timeout: float = READ_TIMEOUT_S):
    """Клиент httpx для Microsoft.

    trust_env=False принципиально: бывает включён локальный VPN,
    который прописывает системный прокси, и запросы через него ломаются —
    вплоть до того, что выписанный токен перестаёт подходить к ссылке.
    """
    try:
        import httpx
    except ImportError:
        raise RuntimeError(
            "Для работы с SharePoint нужен пакет httpx, а он не установлен."
        ) from None

    timeout = httpx.Timeout(
        connect=CONNECT_TIMEOUT_S, read=read_timeout,
        write=read_timeout, pool=CONNECT_TIMEOUT_S,
    )
    return httpx.Client(
        timeout=timeout, trust_env=False, follow_redirects=True,
        headers={"User-Agent": UA},
    )


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": "Bearer %s" % token}


#: Грабли старого проекта: 403 при скачивании чаще всего не «нет прав», а
#: политика сайта или метка конфиденциальности — смотреть можно, скачивать нет.
FORBIDDEN_HINT = ("Нет доступа к файлу. Чаще всего запись лежит в OneDrive организатора "
                  "встречи — Teams разрешает скачивать видео только ему, — либо у файла "
                  "метка конфиденциальности: смотреть можно, скачивать нельзя. "
                  "Проверьте в браузере, работает ли у записи кнопка «Скачать».")


def _forbidden(err: Exception) -> bool:
    text = str(err)
    return text.startswith("Нет доступа к файлу") or "(код 403)" in text or "403" in text[:80]


def _graph_error(what: str, resp: Any) -> str:
    """Отказ Graph -> объяснение по-русски.

    Без кода причину не отличить, а человеку она важна: истёкший вход лечится
    повторным входом, отсутствие прав — ничем.
    """
    code = getattr(resp, "status_code", 0)
    hint = {
        401: " Вход истёк — войдите в Microsoft заново.",
        403: " " + FORBIDDEN_HINT,
        404: " Файл не найден: удалён или ещё обрабатывается.",
        429: " Microsoft просит подождать — попробуйте через минуту.",
    }.get(code, "")
    body = ""
    try:
        body = _safe(resp.text, 160)
    except Exception:
        body = ""
    return "%s (ответ Microsoft %s).%s %s" % (what, code or "без кода", hint, body)


# --------------------------------------------------------------------------- вход

_lock = threading.RLock()

# Всё про вход — только в памяти процесса. На диск не попадает ничего:
# refresh token равносилен паролю от почты и файлов человека.
_state: dict[str, Any] = {
    "access": "",           # токен к Graph
    "access_until": 0.0,    # время, после которого его надо обновить
    "refresh": "",          # им получаем и Graph-токен, и токен к хосту SharePoint
    "pending": None,        # начатый, но не подтверждённый вход по коду
}

#: Токены к хостам SharePoint: {host: (token, годен_до)}.
_resource: dict[str, tuple[str, float]] = {}


def _remember(body: dict[str, Any]) -> str:
    """Запомнить выданные токены. Возвращает access token."""
    access = str(body.get("access_token") or "")
    with _lock:
        if access:
            _state["access"] = access
            # Минуту отнимаем на дорогу: токен не должен протухнуть посреди запроса.
            _state["access_until"] = time.time() + float(body.get("expires_in") or 3600) - 60.0
        if body.get("refresh_token"):
            _state["refresh"] = str(body["refresh_token"])
    return access


def _token_post(payload: dict[str, str]) -> tuple[int, dict[str, Any]]:
    with _client() as cli:
        resp = cli.post(_AUTH + "/token", data=payload)
    try:
        body = resp.json()
    except ValueError:
        body = {}
    return resp.status_code, body


def begin_login() -> dict[str, Any]:
    """Шаг 1 входа: получить ссылку и код, которые надо показать человеку.

    Возвращает {"url", "code", "expires_in"}. Сам device_code наружу не отдаём и
    не логируем: тот, кто его знает, может забрать токен вместо человека.
    """
    with _client() as cli:
        resp = cli.post(_AUTH + "/devicecode",
                        data={"client_id": SP_CLIENT_ID, "scope": _SCOPE})
    try:
        data = resp.json()
    except ValueError:
        data = {}
    device_code = str(data.get("device_code") or "")
    if not device_code:
        raise RuntimeError("Microsoft не выдал код для входа. %s" % _safe(resp.text, 160))

    expires_in = int(data.get("expires_in") or 900)
    url = str(data.get("verification_uri") or data.get("verification_url")
              or "https://microsoft.com/devicelogin")
    code = str(data.get("user_code") or "")
    with _lock:
        _state["pending"] = {
            "device_code": device_code,
            # Интервал опроса задаёт Microsoft; чаще спрашивать нельзя — забанит.
            "interval": max(2, int(data.get("interval") or 5)),
            "deadline": time.time() + expires_in,
            # Ссылку и короткий код человек и так видит на экране — их можно
            # показать заново после перезагрузки страницы. device_code — нет.
            "url": url, "code": code,
        }
    log.info("вход в Microsoft начат, код действует %d мин", expires_in // 60)
    return {"url": url, "code": code, "expires_in": expires_in}


def pending_login() -> dict[str, Any] | None:
    """Начатый, но не подтверждённый вход: ссылка, код, сколько осталось."""
    with _lock:
        p = _state.get("pending")
        if not p:
            return None
        left = int(p["deadline"] - time.time())
        if left <= 0:
            return None
        return {"url": p.get("url"), "code": p.get("code"), "expires_in": left}


def wait_login(handle: Any = None) -> bool:
    """Шаг 2 входа: ждать подтверждения в браузере.

    True — вошли. False — человек не успел подтвердить, надо запрашивать код
    заново. Явный отказ и поломка на стороне Microsoft — исключение с понятным
    текстом.
    """
    with _lock:
        pending = _state.get("pending")
    if not pending:
        raise RuntimeError("Вход не начат: сначала запросите код.")

    interval = float(pending["interval"])
    while time.time() < pending["deadline"]:
        _sleep(interval, handle)
        status, body = _token_post({
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            "client_id": SP_CLIENT_ID,
            "device_code": pending["device_code"],
        })
        if status == 200 and body.get("access_token"):
            _remember(body)
            with _lock:
                _state["pending"] = None
            _note(handle, "Вход в Microsoft выполнен.")
            return True

        err = str(body.get("error") or "")
        if err == "authorization_pending":
            continue
        if err == "slow_down":
            interval += 5                  # Microsoft просит спрашивать реже — слушаемся
            continue
        if err in ("expired_token", "code_expired"):
            break
        with _lock:
            _state["pending"] = None
        if err in ("authorization_declined", "access_denied"):
            raise RuntimeError("Вход отклонён в браузере.")
        raise RuntimeError("Microsoft отказал во входе: %s"
                           % (_safe(body.get("error_description") or err, 160) or "причина не указана"))

    with _lock:
        _state["pending"] = None
    _note(handle, "Вход не подтверждён вовремя — запросите код заново.")
    return False


def logged_in() -> bool:
    """Есть ли действующий вход. В сеть не ходит."""
    with _lock:
        if _state["refresh"]:
            return True
        return bool(_state["access"] and time.time() < _state["access_until"])


def logout() -> None:
    """Забыть вход. Стирать с диска нечего — там ничего и не было."""
    with _lock:
        _state.update({"access": "", "access_until": 0.0, "refresh": "", "pending": None})
        _resource.clear()
    log.info("вход в Microsoft забыт")


def _access_token() -> str:
    """Действующий токен к Graph. При нужде молча обновляет его по refresh."""
    with _lock:
        access, until, refresh = _state["access"], _state["access_until"], _state["refresh"]
    if access and time.time() < until:
        return access
    if not refresh:
        raise RuntimeError("Вход в Microsoft не выполнен — войдите и повторите.")

    status, body = _token_post({
        "grant_type": "refresh_token", "client_id": SP_CLIENT_ID,
        "refresh_token": refresh, "scope": _SCOPE,
    })
    token = _remember(body) if status == 200 else ""
    if not token:
        # Refresh не вечен: тенант может отозвать его сменой пароля или политикой.
        logout()
        raise RuntimeError("Вход в Microsoft истёк — нужно войти заново.")
    return token


def sp_resource_token(host: str) -> str:
    """Токен к самому хосту SharePoint (а не к Graph) — по refresh того же входа.

    Нужен потому, что у SharePoint REST v2.1 своя аудитория: токен, выписанный
    на graph.microsoft.com, он не принимает. Повторно подтверждать код при этом
    не требуется. Пусто, если refresh нет или согласия на скоупы SharePoint не
    выдано, — вызывающий просто обходится без транскрипта из Stream.
    """
    host = str(host or "").strip()
    if not host:
        return ""
    with _lock:
        cached = _resource.get(host)
        refresh = _state["refresh"]
    if cached and time.time() < cached[1]:
        return cached[0]
    if not refresh:
        return ""

    status, body = _token_post({
        "grant_type": "refresh_token", "client_id": SP_CLIENT_ID,
        "refresh_token": refresh, "scope": "https://%s/.default" % host,
    })
    if status != 200:
        log.info("токен к %s не выдан (код %s) — транскрипт из Stream пропускаем", host, status)
        return ""
    # Microsoft может выдать новый refresh взамен использованного — запоминаем.
    token = str(body.get("access_token") or "")
    with _lock:
        if body.get("refresh_token"):
            _state["refresh"] = str(body["refresh_token"])
        if token:
            _resource[host] = (token, time.time() + float(body.get("expires_in") or 3600) - 60.0)
    return token


# --------------------------------------------------------------------------- поиск

def pair_items(items: list[dict]) -> list[dict]:
    """Сводит найденное в один список:

    видео + одноимённый транскрипт -> ОДНА строка (распознавание не понадобится);
    транскрипт без видео -> отдельная строка (встречу не записывали).
    Пара ищется по «ядру» имени в пределах одного диска.
    """
    vids = [i for i in items if i["kind"] == "video"]
    trs = [i for i in items if i["kind"] == "transcript"]
    by_key: dict[tuple[str, str], dict] = {}
    for t in trs:
        by_key.setdefault((t["driveId"], _match_key(t["name"])), t)
    paired = set()
    for v in vids:
        t = by_key.get((v["driveId"], _match_key(v["name"])))
        if t:
            v["transcript_item"] = {"id": t["id"], "driveId": t["driveId"], "name": t["name"]}
            paired.add(t["id"])
    out = vids + [t for t in trs if t["id"] not in paired]
    out.sort(key=lambda x: x["modified"], reverse=True)
    return out


def item_label(it: dict) -> str:
    """Подпись в списке находок: тип виден сразу.

    Нулевой размер у видео — встречу не записывали: Teams создаёт пустой
    mp4-контейнер, чтобы прицепить к нему транскрипт.
    """
    icon = ("🎬" if it.get("has_video") else "") + ("📝" if it.get("has_transcript") else "")
    return "%s · %sMB · %s %s" % (it.get("date") or it.get("modified", ""), it.get("sizeMB", 0),
                                  icon or "·", it.get("name", ""))


def meeting_date(name: str, modified: str = "") -> str:
    """Дата встречи: из имени файла Teams, иначе дата изменения файла."""
    m = _MEETING_DATE_RE.search(str(name or ""))
    if m:
        y, mo, d = m.group(1), m.group(2), m.group(3)
        if 1 <= int(mo) <= 12 and 1 <= int(d) <= 31:
            return "%s-%s-%s" % (y, mo, d)
    return str(modified or "")[:10]


def is_meeting(it: dict) -> bool:
    """Похоже ли на запись встречи Teams: по имени или по папке «Recordings»."""
    if it.get("kind") == "transcript":
        return True
    if MEETING_NAME_RE.search(str(it.get("name") or "")):
        return True
    return "/recordings/" in str(it.get("web_url") or "").lower()


def _months_set(months: Any) -> set[int]:
    out = set()
    for m in (months or []):
        try:
            v = int(m)
        except (TypeError, ValueError):
            continue
        if 1 <= v <= 12:
            out.add(v)
    return out


def _kql(query: str, year: int | None, months: set[int]) -> str:
    """Строка запроса для Graph Search.

    queryString — это KQL: слова через пробел = И (все слова, порядок неважен),
    OR — «или», "фраза" — точное совпадение. Сужаем типами файлов и диапазоном
    дат изменения: так и быстрее, и меньше капризов у самого Search.
    """
    base = (query or "").strip()
    ftypes = " OR ".join("filetype:%s" % e for e in VIDEO_EXT_KQL + TRANSCRIPT_EXT_KQL)
    parts = (["(%s)" % base] if base else []) + ["(%s)" % ftypes]
    if year:
        first = min(months) if months else 1
        last = max(months) if months else 12
        start = "%d-%02d-01" % (year, first)
        # Верхнюю границу даём с запасом в месяц: фильтр идёт по дате ИЗМЕНЕНИЯ,
        # а расшифровку к записи Teams дописывает позже. Точный отбор по дате
        # встречи — на нашей стороне.
        ey, em = (year, last + 2) if last <= 10 else (year + 1, last + 2 - 12)
        end = "%d-%02d-01" % (ey, em)
        parts.append("LastModifiedTime>=%s AND LastModifiedTime<%s" % (start, end))
    return " AND ".join(parts)


def _retry_pause(resp: Any, attempt: int) -> float:
    """Сколько ждать перед повтором: Microsoft сам говорит через Retry-After."""
    raw = ""
    try:
        raw = str(resp.headers.get("Retry-After") or "")
    except Exception:
        raw = ""
    if raw.strip().isdigit():
        return float(min(int(raw.strip()), 60))
    return float(min(2 ** attempt, 16))


def sp_search(token: str, query: str, year: int | None = None, months: Any = None,
              max_results: int = MAX_RESULTS, handle: Any = None) -> list[dict]:
    """Поиск записей встреч (driveItem) через Graph Search.

    Возвращает список {id, driveId, name, modified, sizeMB, kind, web_url,
    duration_s, label}, где kind = video|transcript, а у видео с найденным
    транскриптом дополнительно transcript_item.

    Graph Search периодически отдаёт временный отказ 500 («The call failed,
    please try again») — это его нормальное поведение, а не наша ошибка, поэтому
    повторяем на 5xx и 429 с растущей паузой.
    """
    months_set = _months_set(months)
    kql = _kql(query, year, months_set)

    out: list[dict] = []
    seen: set[str] = set()
    seen_files: set[tuple[str, int]] = set()
    start = 0
    with _client(120.0) as cli:
        while start < int(max_results):
            body = {"requests": [{"entityTypes": ["driveItem"],
                                  "query": {"queryString": kql},
                                  "from": start, "size": SEARCH_PAGE}]}
            resp = None
            for attempt in range(SEARCH_RETRIES):
                _check(handle)
                _progress(handle, 0.1, "ищу записи в SharePoint")
                resp = cli.post("%s/search/query" % _GRAPH, headers=_bearer(token), json=body)
                if resp.status_code < 400:
                    break
                if resp.status_code in _RETRY_CODES:
                    pause = _retry_pause(resp, attempt)
                    log.info("поиск отвечает %s, повтор через %.0f c", resp.status_code, pause)
                    _sleep(pause, handle)
                    continue
                break
            if resp is None or resp.status_code >= 400:
                raise RuntimeError(_graph_error("Поиск в SharePoint не удался", resp))
            try:
                data = resp.json()
            except ValueError:
                raise RuntimeError("Поиск вернул не JSON — попробуйте ещё раз.") from None

            more = False
            got = 0
            for grp in data.get("value", []):
                for cont in grp.get("hitsContainers", []):
                    more = more or bool(cont.get("moreResultsAvailable"))
                    for hit in cont.get("hits", []):
                        got += 1
                        res = hit.get("resource", {}) or {}
                        name = str(res.get("name") or "")
                        if not _HIT_RE.search(name):
                            continue
                        modified = str(res.get("lastModifiedDateTime") or "")
                        date = meeting_date(name, modified)
                        if year:
                            # Точный отбор — по дате ВСТРЕЧИ, а не изменения файла.
                            if date[:4] != str(int(year)):
                                continue
                            mm = int(date[5:7]) if date[5:7].isdigit() else 0
                            if months_set and mm and mm not in months_set:
                                continue
                        drive = str((res.get("parentReference") or {}).get("driveId") or "")
                        iid = str(res.get("id") or "")
                        size = int(res.get("size") or 0)
                        if not drive or not iid or iid in seen:
                            continue
                        # Search возвращает один и тот же файл по нескольку раз, в
                        # том числе под разными id (копии в разных библиотеках).
                        fkey = (name.lower(), size)
                        if fkey in seen_files:
                            continue
                        seen.add(iid)
                        seen_files.add(fkey)
                        kind = "transcript" if _is_transcript_name(name) else "video"
                        out.append({
                            "id": iid, "driveId": drive, "name": name,
                            "modified": modified[:10], "date": date,
                            "size": size, "sizeMB": round(size / 1048576),
                            "kind": kind,
                            "has_video": kind == "video" and size >= EMPTY_CONTAINER_BYTES,
                            "web_url": _clean_url(res.get("webUrl")),
                            "duration_s": _facet_duration(res),
                            "source_name": SOURCE_NAME,
                        })
            start += SEARCH_PAGE
            if not more or got == 0:
                break

    items = pair_items(out)
    for it in items:
        # Расшифровка известна, только если её нашёл поиск. Привязанную к записи
        # расшифровку Stream поиск не видит — её проверяет check_transcripts().
        it["has_transcript"] = True if (it["kind"] == "transcript" or it.get("transcript_item")) else None
        it["is_meeting"] = is_meeting(it)
        it["label"] = item_label(it)
    items.sort(key=lambda x: (x.get("date") or "", x.get("modified") or ""), reverse=True)
    return items


def search_ex(query: str, year: int | None = None, months: list[int] | None = None,
              meetings_only: bool = True, handle: Any = None) -> dict[str, Any]:
    """Поиск для окна: находки и сколько отброшено фильтром «только встречи»."""
    token = _access_token()
    _progress(handle, 0.05, "ищу записи в SharePoint")
    items = sp_search(token, query, year, months, handle=handle)
    hidden = 0
    if meetings_only:
        kept = [i for i in items if i.get("is_meeting")]
        hidden = len(items) - len(kept)
        items = kept
    with_tr = sum(1 for i in items if i.get("has_transcript"))
    _note(handle, "Найдено записей: %d (с готовым транскриптом: %d, скрыто не встреч: %d)"
          % (len(items), with_tr, hidden))
    _progress(handle, 1.0, "поиск закончен")
    return {"items": items, "hidden": hidden}


def has_stream_transcript(token: str, drive_id: str, item_id: str, web_url: str,
                          cli: Any = None) -> bool | None:
    """Есть ли у записи расшифровка Teams в Stream. None — узнать не удалось.

    cli — уже открытое соединение: при проверке шестидесяти записей подряд
    новое TLS-рукопожатие на каждую занимало больше времени, чем сам вопрос.
    """
    host, base = _stream_api_base(web_url)
    if not base:
        return None
    sp_token = sp_resource_token(host)
    if not sp_token:
        return None
    try:
        url = "%s/drives/%s/items/%s/media/transcripts" % (base, drive_id, item_id)
        if cli is not None:
            r = cli.get(url, headers=_bearer(sp_token))
        else:
            with _client() as own:
                r = own.get(url, headers=_bearer(sp_token))
        if r.status_code >= 400:
            return None
        data = r.json()
        return bool(data.get("value") or data.get("transcripts"))
    except Exception as err:
        log.debug("проверка расшифровки не удалась: %s", _safe(err, 120))
        return None


def check_transcripts(items: list[dict], handle: Any = None) -> dict[str, bool | None]:
    """Пометка 📝 для найденных видео: у кого есть расшифровка Teams.

    Поиск её не видит — Teams привязывает расшифровку к самой записи, — поэтому
    спрашиваем про каждую запись отдельно. {id: True/False/None}.
    """
    token = _access_token()
    out: dict[str, bool | None] = {}
    with _client() as cli:
        for it in items[:60]:
            _check(handle)
            iid, drive = str(it.get("id") or ""), str(it.get("driveId") or "")
            if not iid or not drive:
                continue
            out[iid] = has_stream_transcript(token, drive, iid, str(it.get("web_url") or ""),
                                             cli=cli)
    return out


# --------------------------------------------------------------------------- элементы и файлы

def _graph_item(token: str, drive_id: str, item_id: str) -> dict[str, Any]:
    """Свойства элемента: преавторизованная ссылка, размер, длительность, webUrl."""
    with _client() as cli:
        resp = cli.get("%s/drives/%s/items/%s" % (_GRAPH, drive_id, item_id),
                       headers=_bearer(token))
    if resp.status_code >= 400:
        raise RuntimeError(_graph_error("Не удалось открыть запись", resp))
    try:
        return resp.json()
    except ValueError:
        raise RuntimeError("Microsoft вернул не JSON вместо описания записи.") from None


def sp_resolve_item(token: str, drive_id: str, item_id: str) -> str:
    """Свежая преавторизованная ссылка на файл.

    Ссылка секретная: в её запросе лежит tempauth — пропуск к файлу без всякого
    токена. Ни в лог, ни в текст ошибки она не попадает.
    """
    url = _graph_item(token, drive_id, item_id).get("@microsoft.graph.downloadUrl")
    if not url:
        raise RuntimeError("Microsoft не дал ссылку на файл — возможно, запись ещё "
                           "обрабатывается или к ней нет доступа.")
    return str(url)


def _download_url(url: str, dest: Path, headers: dict[str, str] | None = None,
                  expect: int = 0, handle: Any = None, what: str = "запись") -> Path:
    """Скачать по ссылке в файл, с прогрессом и отменой."""
    import httpx

    dest.parent.mkdir(parents=True, exist_ok=True)
    # Пишем во временный файл: оборванное скачивание не должно выглядеть готовым.
    tmp = Path(str(dest) + ".part")
    done = 0
    try:
        with _client(LONG_READ_TIMEOUT_S) as cli:
            with cli.stream("GET", url, headers=headers or None) as resp:
                if resp.status_code >= 400:
                    resp.read()
                    if resp.status_code == 403:
                        raise RuntimeError(FORBIDDEN_HINT)
                    raise RuntimeError("Microsoft не отдал файл (код %d)." % resp.status_code)
                total = int(resp.headers.get("Content-Length") or expect or 0)
                shown = -1
                with open(tmp, "wb") as fh:
                    for chunk in resp.iter_bytes(1 << 20):
                        _check(handle)
                        if not chunk:
                            continue
                        fh.write(chunk)
                        done += len(chunk)
                        pct = int(done * 100 / total) if total else -1
                        if pct != shown:
                            shown = pct
                            _progress(handle, (done / total) if total else 0.0,
                                      "скачиваю %s: %d%%" % (what, pct) if pct >= 0
                                      else "скачано %d МБ" % (done >> 20))
    except _Cancelled:
        tmp.unlink(missing_ok=True)
        raise
    except httpx.HTTPError as err:
        tmp.unlink(missing_ok=True)
        # В тексте ошибок httpx лежит полный адрес запроса вместе с tempauth.
        raise RuntimeError("Скачивание оборвалось: %s" % _safe(err, 160)) from None
    except OSError as err:
        tmp.unlink(missing_ok=True)
        raise RuntimeError("Не удалось записать файл на диск: %s" % _safe(err, 160)) from None

    if done == 0:
        tmp.unlink(missing_ok=True)
        raise RuntimeError("Файл скачался пустым.")
    os.replace(tmp, dest)
    return dest


def sp_download_file(token: str, drive_id: str, item_id: str, dest: Path,
                     handle: Any = None) -> Path:
    """Скачать элемент целиком — для .vtt/.srt, они маленькие."""
    return _download_url(sp_resolve_item(token, drive_id, item_id), Path(dest),
                         handle=handle, what="транскрипт")


def sp_download_content(token: str, drive_id: str, item_id: str, dest: Path,
                        handle: Any = None) -> Path:
    """Скачивание через Graph /content с токеном — запасной путь.

    Нужен, когда преавторизованная ссылка отвечает отказом 403: часть тенантов
    режет чтение потока по такой ссылке (особенно кусками, range-запросами).
    Через Graph файл отдаётся нормально, просто тянуть приходится целиком.
    """
    dest = Path(dest)
    _download_url("%s/drives/%s/items/%s/content" % (_GRAPH, drive_id, item_id),
                  dest, headers=_bearer(token), handle=handle)
    if dest.stat().st_size < 1_000_000:
        # Меньше мегабайта для записи встречи — это не видео, а обрывок.
        raise RuntimeError("Файл скачался пустым.")
    return dest


def sp_find_sibling_transcript(token: str, drive_id: str, item_id: str, name: str):
    """Транскрипт рядом с записью, в той же папке SharePoint.

    Поиск индексирует .vtt не всегда, а сосед по папке находится надёжно.
    Возвращает {'id','driveId','name'} либо None; ошибки гасим — это подсказка,
    а не обязательный шаг.
    """
    try:
        with _client() as cli:
            hdr = _bearer(token)
            par = (cli.get("%s/drives/%s/items/%s" % (_GRAPH, drive_id, item_id),
                           headers=hdr).json().get("parentReference") or {})
            pid, pdrive = par.get("id"), par.get("driveId") or drive_id
            if not pid:
                return None
            kids = cli.get("%s/drives/%s/items/%s/children?$top=400&$select=id,name"
                           % (_GRAPH, pdrive, pid), headers=hdr).json().get("value", [])
        key = _match_key(name)
        for ch in kids:
            n = str(ch.get("name") or "")
            if _is_transcript_name(n) and _match_key(n) == key:
                return {"id": str(ch.get("id") or ""), "driveId": pdrive, "name": n}
    except Exception as err:
        log.debug("сосед-транскрипт не найден: %s", _safe(err, 120))
        return None
    return None


# --------------------------------------------------------------------------- транскрипт Teams

def _stream_api_base(web_url: str) -> tuple[str, str]:
    """webUrl элемента -> (хост, база REST v2.1).

    Записи лежат либо на сайте команды (/sites/, /teams/), либо в личном
    OneDrive (/personal/) — префикс нужен обоим, иначе SharePoint отвечает 404.
    """
    m = re.match(r"https://([^/]+)(/(?:sites|teams|personal)/[^/]+)?/", str(web_url or ""))
    if not m:
        return "", ""
    host, site = m.group(1), (m.group(2) or "")
    return host, "https://%s%s/_api/v2.1" % (host, site)


def _ticks_to_tc(value: Any) -> str:
    """'00:00:05.1234567' (тики .NET) -> '00:00:05.123'."""
    s = str(value or "0:00:00").strip()
    h, m, rest = (s.split(":") + ["0", "0"])[:3]
    sec, _, frac = rest.partition(".")
    try:
        return "%02d:%02d:%02d.%s" % (int(h), int(m), int(float(sec)), (frac + "000")[:3])
    except ValueError:
        return "00:00:00.000"


def _entries_to_vtt(entries: list) -> str:
    """JSON-транскрипт Stream -> WebVTT со спикерами (дальше его читает общий разбор)."""
    out = ["WEBVTT", ""]
    for e in entries or []:
        text = str((e or {}).get("text") or "").strip()
        if not text:
            continue
        who = str(e.get("speakerDisplayName") or "").strip()
        out += ["%s --> %s" % (_ticks_to_tc(e.get("startOffset")), _ticks_to_tc(e.get("endOffset"))),
                ("<v %s>%s</v>" % (who, text)) if who else text,
                ""]
    return "\n".join(out)


def sp_stream_transcript(token: str, drive_id: str, item_id: str, dest: Path,
                         web_url: str = "", handle: Any = None):
    """Транскрипт Teams-записи из Stream (SharePoint REST v2.1, media/transcripts).

    Отдельным файлом в папке он НЕ лежит: Teams привязывает его к самой записи,
    поэтому ни поиск по filetype:vtt, ни перебор соседей его не находят.
    Возвращает путь или None — отсутствие транскрипта не ошибка.
    """
    try:
        with _client() as cli:
            hdr = _bearer(token)
            if not web_url:
                web_url = cli.get("%s/drives/%s/items/%s?$select=webUrl" % (_GRAPH, drive_id, item_id),
                                  headers=hdr).json().get("webUrl", "")
            host, base = _stream_api_base(web_url)
            sp_token = sp_resource_token(host)
            if not sp_token:
                return None
            sph = _bearer(sp_token)
            lst = cli.get("%s/drives/%s/items/%s/media/transcripts" % (base, drive_id, item_id),
                          headers=sph)
            if lst.status_code >= 400:
                _note(handle, "Транскрипт из Stream недоступен (код %d)" % lst.status_code)
                return None
            data = lst.json()
            items = data.get("value") or data.get("transcripts") or []
            if not items:
                return None
            tid = items[0].get("id") or items[0].get("transcriptId")

            raw = None
            # json — рабочий формат Stream (schemas/transcript.json); webvtt отдают
            # не все тенанты (на корпоративном тенанте проверено: format=webvtt
            # отвечает 400), поэтому он идёт вторым.
            for fmt in ("json", "webvtt"):
                r = cli.get("%s/drives/%s/items/%s/media/transcripts/%s/streamContent?format=%s"
                            % (base, drive_id, item_id, tid, fmt), headers=sph, timeout=120.0)
                if r.status_code < 400 and r.content:
                    raw = r.text
                    break
        if not raw:
            return None
        if not raw.lstrip().upper().startswith("WEBVTT"):
            parsed = json.loads(raw)
            raw = _entries_to_vtt(parsed.get("entries") or parsed.get("value") or [])
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(raw, encoding="utf-8")
        _note(handle, "Транскрипт получен из Stream — распознавание не нужно")
        return dest
    except _Cancelled:
        raise
    except Exception as err:
        _note(handle, "Транскрипт из Stream не забрался: %s" % _safe(err, 120))
        return None


def _attach_sp_transcript(token: str, dst_dir: Path, drive_id: str, item_id: str,
                          name: str, transcript_item: dict | None = None,
                          web_url: str = "", handle: Any = None,
                          prefer: bool | None = None) -> str:
    """Найти к записи готовый транскрипт. Возвращает путь к файлу или "".

    Три источника по убыванию дешевизны: найденный поиском файл, файл-сосед по
    папке, привязанный к записи транскрипт Stream (у встреч Teams он именно
    там, отдельным файлом не лежит).

    prefer — галочка «брать готовые субтитры» из формы задания. Раньше здесь
    читалась только общая настройка, и галочка в форме на SharePoint не влияла.
    """
    if prefer is None:
        prefer = bool(config.get("prefer_transcript", True))
    if not prefer:
        return ""
    _check(handle)
    stem = _slug(Path(name).stem, 60)

    tr = transcript_item or sp_find_sibling_transcript(token, drive_id, item_id, name)
    if tr and tr.get("id"):
        dest = Path(dst_dir) / (stem + (Path(tr["name"]).suffix.lower() or ".vtt"))
        try:
            sp_download_file(token, tr["driveId"], tr["id"], dest, handle)
            _note(handle, "Найден готовый транскрипт «%s» — распознавание не нужно" % tr["name"])
            return str(dest)
        except _Cancelled:
            raise
        except Exception as err:
            _note(handle, "Транскрипт «%s» не скачался: %s" % (tr["name"], _safe(err, 100)))

    got = sp_stream_transcript(token, drive_id, item_id, Path(dst_dir) / (stem + ".vtt"),
                               web_url, handle)
    return str(got) if got else ""


# --------------------------------------------------------------------------- получение записи

def _facet_duration(meta: dict) -> float:
    """Длительность из описания файла (Graph отдаёт её в миллисекундах)."""
    for facet in ("video", "audio"):
        try:
            value = float((meta.get(facet) or {}).get("duration") or 0) / 1000.0
        except (TypeError, ValueError):
            value = 0.0
        if value > 0:
            return round(value, 1)
    return 0.0


def _probe_duration(path: Path) -> float:
    """Длительность файла через свой ffprobe (ни одного стороннего .exe в проекте)."""
    cmd = [audio_io.ffprobe_exe(), "-hide_banner", "-v", "error", "-show_entries",
           "format=duration", "-of", "default=nw=1:nk=1", str(path)]
    try:
        out = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=120, creationflags=platform.system().hidden_process_flags(), env=direct_env(),
        )
        return round(float((out.stdout or "0").strip() or 0.0), 1)
    except Exception:
        return 0.0


def _storage_kind(web_url: str) -> str:
    """Личный OneDrive или библиотека сайта — видно по адресу."""
    return "onedrive" if "/personal/" in str(web_url or "").lower() else "sharepoint"


def resolve_link(url: str, token: str | None = None) -> dict[str, Any]:
    """Ссылка SharePoint/OneDrive -> элемент такого же вида, как из поиска.

    Ссылку Microsoft принимает не как есть, а закодированной в base64 с
    префиксом «u!» — таков формат Graph /shares.
    """
    link = str(url or "").strip()
    if not link.lower().startswith(("http://", "https://")):
        raise RuntimeError("Это не ссылка на SharePoint или OneDrive.")
    token = token or _access_token()
    enc = "u!" + base64.urlsafe_b64encode(link.encode("utf-8")).decode("ascii").rstrip("=")
    with _client() as cli:
        resp = cli.get("%s/shares/%s/driveItem" % (_GRAPH, enc), headers=_bearer(token))
    if resp.status_code >= 400:
        raise RuntimeError(_graph_error("Не удалось открыть ссылку", resp))
    item = resp.json()
    name = str(item.get("name") or "meeting")
    return {
        "id": str(item.get("id") or ""),
        "driveId": str((item.get("parentReference") or {}).get("driveId") or ""),
        "name": name,
        "modified": str(item.get("lastModifiedDateTime") or "")[:10],
        "sizeMB": round(int(item.get("size") or 0) / 1048576),
        "kind": "transcript" if _is_transcript_name(name) else "video",
        "web_url": _clean_url(item.get("webUrl")) or _clean_url(link),
        "duration_s": _facet_duration(item),
        "source_name": SOURCE_NAME,
    }


def fetch(item: dict, dst_dir: Path, handle: Any = None,
          opts: dict[str, Any] | None = None) -> dict[str, Any]:
    """Забрать запись: видео и/или готовый транскрипт в dst_dir.

    item — строка из search() либо {"url": "<ссылка SharePoint>"}. Результат —
    общий договор источников. Пустое "media" означает, что видео нет вовсе:
    встречу не записывали, есть только расшифровка.

    opts — настройки задания из формы: «брать готовый текст» (prefer_transcript)
    и «что хранить» (store_media). Если текст готов, а хранить видео и звук не
    просили — гигабайт не качаем вовсе.
    """
    opts = dict(opts or {})
    prefer = opts.get("prefer_transcript")
    prefer = bool(config.get("prefer_transcript", True)) if prefer is None else bool(prefer)
    store_mode = str(opts.get("store_media") or "").strip().lower()
    if store_mode not in ("none", "audio", "video"):
        store_mode = "video" if config.get("keep_video", True) else "none"
    token = _access_token()
    dst_dir = Path(dst_dir)
    dst_dir.mkdir(parents=True, exist_ok=True)

    it = dict(item or {})
    if not (it.get("id") and it.get("driveId")):
        link = it.get("url") or it.get("web_url") or it.get("link") or ""
        if not link:
            raise RuntimeError("Нечего забирать: нужна ссылка или запись из поиска.")
        _progress(handle, 0.02, "открываю ссылку")
        it = resolve_link(str(link), token)
        if not (it.get("id") and it.get("driveId")):
            raise RuntimeError("По ссылке не нашлось файла.")

    name = str(it.get("name") or "meeting")
    title = Path(name).stem
    drive_id, item_id = str(it["driveId"]), str(it["id"])
    kind = it.get("kind") or ("transcript" if _is_transcript_name(name) else "video")
    stem = _slug(title, 60)
    _check(handle)

    extra: dict[str, Any] = {
        "drive_id": drive_id, "item_id": item_id, "file_name": name,
        "modified": it.get("modified", ""),
        "storage": _storage_kind(it.get("web_url", "")),
    }

    # Только транскрипт: встречу не записывали либо выбран сам файл .vtt/.srt.
    if kind == "transcript":
        dest = dst_dir / (stem + (Path(name).suffix.lower() or ".vtt"))
        _note(handle, "Скачиваю готовый транскрипт (видео не записывалось)…")
        sp_download_file(token, drive_id, item_id, dest, handle)
        _progress(handle, 1.0, "транскрипт получен")
        return {
            "media": "", "transcript": str(dest), "title": title,
            "url": it.get("web_url", ""), "source_name": SOURCE_NAME,
            "duration_s": 0.0, "extra": dict(extra, has_video=False),
        }

    _progress(handle, 0.05, "смотрю запись")
    meta = _graph_item(token, drive_id, item_id)
    size = int(meta.get("size") or 0)
    web_url_raw = str(meta.get("webUrl") or "")
    source_url = _clean_url(web_url_raw) or it.get("web_url", "")
    duration = _facet_duration(meta) or float(it.get("duration_s") or 0.0)
    extra["storage"] = _storage_kind(web_url_raw)
    extra["size_mb"] = round(size / 1048576)

    transcript = _attach_sp_transcript(token, dst_dir, drive_id, item_id, name,
                                       it.get("transcript_item"), web_url_raw, handle,
                                       prefer=prefer)
    _check(handle)

    media = ""
    if size < EMPTY_CONTAINER_BYTES:
        # Пустой контейнер: встречу не записывали, качать нечего.
        _note(handle, "Видео у записи нет (пустой контейнер) — работаю только по транскрипту")
        if not transcript:
            raise RuntimeError("У этой записи нет ни видео, ни транскрипта.")
    elif transcript and store_mode == "none":
        # Транскрипт готов, распознавание не нужно, а хранить видео и звук не
        # просили — значит, и качать гигабайт незачем.
        _note(handle, "Транскрипт готов, а видео и звук не храним — скачивание пропускаю")
    else:
        dest = dst_dir / (stem + (Path(name).suffix.lower() or ".mp4"))
        _note(handle, "Скачиваю запись (%d МБ)…" % (size >> 20))
        try:
            media = str(_download_media(token, drive_id, item_id, meta, dest, size, handle))
        except _Cancelled:
            raise
        except RuntimeError as err:
            # Живой случай 13.09: запись в OneDrive организатора — видео 403, а
            # расшифровку Stream отдаёт. Документ по расшифровке сделать можно,
            # падать из-за видео незачем.
            if not (transcript and _forbidden(err)):
                raise
            _note(handle, "Видео скачать нельзя (скачивание разрешено только владельцу "
                          "записи). Расшифровка получена — работаю по ней.")
            extra["video_forbidden"] = True
            media = ""
        if media and not duration:
            duration = _probe_duration(Path(media))

    _progress(handle, 1.0, "запись получена")
    extra["has_video"] = bool(media)
    return {
        "media": media, "transcript": transcript, "title": title,
        "url": source_url, "source_name": SOURCE_NAME,
        "duration_s": float(duration or 0.0), "extra": extra,
    }


def _download_media(token: str, drive_id: str, item_id: str, meta: dict,
                    dest: Path, size: int, handle: Any = None) -> Path:
    """Скачать саму запись: сначала по преавторизованной ссылке, потом через Graph.

    Ссылка быстрее (идёт мимо Graph, прямо в хранилище), но часть тенантов
    отдаёт по ней отказ 403. Тогда тянем через Graph /content с токеном — путь
    медленнее, зато работает всегда.
    """
    url = str(meta.get("@microsoft.graph.downloadUrl") or "")
    if url:
        try:
            return _download_url(url, dest, expect=size, handle=handle)
        except _Cancelled:
            raise
        except Exception as err:
            _note(handle, "Прямая ссылка файл не отдала (%s) — качаю через Graph"
                  % _safe(err, 100))
    _check(handle)
    return sp_download_content(token, drive_id, item_id, dest, handle)
