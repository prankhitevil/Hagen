# -*- coding: utf-8 -*-
"""Облачные сервисы: адреса, ключи, списки моделей и единственная точка выхода в сеть.

Один сервис — это адрес (base URL) плюс ключ. Четыре встроенных (Anthropic, OpenAI,
GigaChat, YandexGPT) и любое число добавленных вручную: подходит любой сервис с
API в формате OpenAI — polza.ai, OpenRouter, VseGPT и прочие.

Почему сеть собрана здесь, а не размазана по модулям:
  * бывает включён локальный VPN в режиме системного прокси, и запросы,
    уходящие через него, рвутся на проверке сертификата — поэтому ВСЕ клиенты
    создаются с trust_env=False;
  * Kaspersky подменяет сертификаты сайтов, из-за чего Python не верит никому —
    лечится truststore.inject_into_ssl() в config.py, и он обязан отработать до
    первого обращения, поэтому config импортируется здесь первым;
  * ключ не должен попасть ни в лог, ни в текст ошибки — вычистка в одном месте.

Ключи хранятся в settings.json (api_keys) и наружу отдаются только маской.
"""
from __future__ import annotations

import hashlib
import ipaddress
import logging
import re
import threading
import time
import uuid
from typing import Any
from urllib.parse import urlsplit

from . import config

log = logging.getLogger("hagen.providers")

HTTP_TIMEOUT_S = 300.0          # предел ожидания ответа, кроме скачивания
CONNECT_TIMEOUT_S = 10.0        # соединение либо устанавливается быстро, либо не будет

#: Встроенные сервисы. id совпадают со значениями, которые уже лежат в settings.json
#: у нынешних пользователей, — поэтому переезд не требует ни миграции, ни правок
#: руками: старое значение api_provider продолжает указывать на ту же запись.
BUILTIN: list[dict[str, Any]] = [
    {
        "id": "anthropic",
        "title": "Anthropic Claude",
        "kind": "anthropic",
        "base_url": "https://api.anthropic.com/v1",
        "service": "Anthropic (api.anthropic.com)",
        "needs": [],
    },
    {
        "id": "openai",
        "title": "OpenAI",
        "kind": "openai",
        "base_url": "https://api.openai.com/v1",
        "service": "OpenAI (api.openai.com)",
        "needs": [],
    },
    {
        "id": "gigachat",
        "title": "GigaChat (Сбер)",
        "kind": "gigachat",
        "base_url": "https://gigachat.devices.sberbank.ru/api/v1",
        "service": "Сбер GigaChat (gigachat.devices.sberbank.ru)",
        "needs": [],
    },
    {
        "id": "yandexgpt",
        "title": "YandexGPT",
        "kind": "yandexgpt",
        "base_url": "https://llm.api.cloud.yandex.net",
        "service": "Яндекс Облако (llm.api.cloud.yandex.net)",
        "needs": ["yandexgpt_folder"],
    },
    {
        # Российский агрегатор: рубли, без VPN, и самый широкий выбор моделей
        # распознавания речи. Именно он обслуживал саммаризатор.
        "id": "polza",
        "title": "polza.ai (много моделей, рубли)",
        "kind": "openai",
        "base_url": "https://polza.ai/api/v1",
        "service": "polza.ai (polza.ai)",
        "needs": [],
    },
    {
        # Яндекс Облако держит не только YandexGPT: через личный кабинет там
        # доступен целый ряд моделей, и у него есть слой, говорящий на языке
        # OpenAI. Отличие от записи «YandexGPT» выше — в нём имя модели пишется
        # целиком: gpt://<номер каталога>/<модель>/latest. Поэтому отдельная
        # запись, а не поле «каталог» (замечено 17.09).
        "id": "yandex_ai_studio",
        "title": "Yandex Cloud AI Studio (много моделей)",
        "kind": "openai",
        "base_url": "https://llm.api.cloud.yandex.net/v1",
        "service": "Яндекс Облако, совместимый слой (llm.api.cloud.yandex.net)",
        "needs": [],
    },
    # --- российские агрегаторы: рубли, карта РФ, без VPN (собрано 17.09) ---
    # Все они говорят на языке OpenAI, поэтому подключение у них одинаковое:
    # получить ключ в личном кабинете и вставить его. Отличаются только ценой
    # и набором моделей. OpenRouter в списке есть, но с оговоркой в названии:
    # из России он больше не работает (подробности у самой записи ниже).
    {
        "id": "provod",
        "title": "provod.ai (рубли, документы для юрлица)",
        "kind": "openai",
        "base_url": "https://api.provod.ai/v1",
        "service": "provod.ai (api.provod.ai)",
        "needs": [],
    },
    {
        "id": "aitunnel",
        "title": "AITunnel (рубли, 200+ моделей)",
        "kind": "openai",
        "base_url": "https://api.aitunnel.ru/v1",
        "service": "AITunnel (api.aitunnel.ru)",
        "needs": [],
    },
    {
        "id": "proxyapi",
        "title": "ProxyAPI (рубли)",
        "kind": "openai",
        "base_url": "https://api.proxyapi.ru/openai/v1",
        "service": "ProxyAPI (api.proxyapi.ru)",
        "needs": [],
    },
    {
        "id": "gptunnel",
        "title": "GPTunnel (рубли)",
        "kind": "openai",
        "base_url": "https://gptunnel.ru/v1",
        "service": "GPTunnel (gptunnel.ru)",
        "needs": [],
    },
    {
        # Оставлен по решению 17.09 — с оговоркой прямо в названии:
        # с мая 2026 не принимает карты РФ, с 27.06 отвечает отказом на запросы
        # из России, и VPN не помогает (регион определяется и по аккаунту).
        # Годится только тем, у кого иностранный аккаунт и карта.
        "id": "openrouter",
        "title": "OpenRouter (нужен иностранный аккаунт и карта)",
        "kind": "openai",
        "base_url": "https://openrouter.ai/api/v1",
        "service": "OpenRouter (openrouter.ai)",
        "needs": [],
    },
    # --- модели напрямую: дешевле агрегаторов, но платить нужно своей картой ---
    {
        "id": "deepseek",
        "title": "DeepSeek (напрямую, дёшево)",
        "kind": "openai",
        "base_url": "https://api.deepseek.com/v1",
        "service": "DeepSeek (api.deepseek.com)",
        "needs": [],
    },
    {
        "id": "zai",
        "title": "Z.AI GLM (напрямую)",
        "kind": "openai",
        "base_url": "https://api.z.ai/api/paas/v4",
        "service": "Z.AI (api.z.ai)",
        "needs": [],
    },
    {
        "id": "moonshot",
        "title": "Moonshot Kimi (напрямую)",
        "kind": "openai",
        "base_url": "https://api.moonshot.ai/v1",
        "service": "Moonshot (api.moonshot.ai)",
        "needs": [],
    },
    {
        # Бесплатный тариф: несколько тысяч запросов в сутки, карта не нужна.
        "id": "groq",
        "title": "Groq (есть бесплатный тариф)",
        "kind": "openai",
        "base_url": "https://api.groq.com/openai/v1",
        "service": "Groq (api.groq.com)",
        "needs": [],
    },
    {
        # У Gemini есть слой, говорящий на языке OpenAI, — берём его, чтобы не
        # заводить в программе ещё один вид сервиса.
        "id": "gemini",
        "title": "Google Gemini (есть бесплатный тариф)",
        "kind": "openai",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "service": "Google Gemini (generativelanguage.googleapis.com)",
        "needs": [],
    },
]

_BUILTIN_IDS = {p["id"] for p in BUILTIN}

#: Имена моделей, по которым видно, что это распознавание речи, а не чат.
#: Нужно там, где сервис не умеет отдавать список по типу (у polza для этого
#: есть параметр type=stt, у остальных — нет).
_ASR_RE = re.compile(
    r"whisper|transcrib|voxtral|chirp|parakeet|nemo|gigaam|asr|speech[-_]?to[-_]?text|"
    r"\bstt\b",
    re.IGNORECASE,
)


# ---------------------------------------------------------------- секреты


def mask(secret: str | None) -> str:
    """Маска ключа: только она уходит в интерфейс, логи и сообщения об ошибках."""
    if not secret:
        return ""
    s = str(secret)
    if len(s) <= 8:
        return "*" * len(s)
    return f"{s[:4]}{'*' * 6}{s[-2:]}"


def scrub(text: str, secrets: list[str | None]) -> str:
    """Вычистить из текста все секреты — на случай, если сервис вернул ключ эхом."""
    out = str(text or "")
    for sec in secrets:
        if sec and len(str(sec)) >= 8:
            out = out.replace(str(sec), mask(sec))
    return out


def first_lines(text: str, limit: int = 3, max_chars: int = 400) -> str:
    """Первые строки чужого вывода: целиком чужой текст пользователю не показываем."""
    lines = [ln.strip() for ln in str(text or "").splitlines() if ln.strip()]
    return " / ".join(lines[:limit])[:max_chars]


def key_fingerprint(secret: str) -> str:
    """Отпечаток ключа для кэша токена: ни сам ключ, ни даже его маску не храним."""
    return hashlib.sha256(str(secret or "").encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------- сеть


def _client(timeout: float, verify: Any = True):
    """HTTP-клиент. trust_env=False — намеренно, см. докстринг модуля."""
    try:
        import httpx
    except ImportError:
        raise RuntimeError(
            "Для обращения к облачному сервису нужен пакет httpx, а он не установлен."
        ) from None
    transport = httpx.HTTPTransport(retries=1, verify=verify)
    return httpx.Client(
        timeout=httpx.Timeout(timeout, connect=CONNECT_TIMEOUT_S),
        transport=transport,
        trust_env=False,
        follow_redirects=True,
    )


def _looks_like_tls(err: Exception) -> bool:
    blob = str(err).lower()
    return any(k in blob for k in ("certificate", "ssl", "tls", "self signed",
                                   "unable to get local issuer"))


def _tls_hint(service: str, err: Exception) -> str:
    """Объяснение ошибки сертификата. Про Сбер — только когда речь о GigaChat."""
    detail = first_lines(str(err), 1, 160)
    if "gigachat" in service.lower():
        return (
            "%s не принял защищённое соединение: не удалось проверить сертификат "
            "(%s). У Сбера собственный корневой сертификат «Russian Trusted Root "
            "CA»: установите его в хранилище Windows либо укажите путь к файлу "
            "сертификата в настройке gigachat_verify. Отключать проверку "
            "сертификата без нужды нельзя." % (service, detail)
        )
    return (
        "%s не принял защищённое соединение: не удалось проверить сертификат "
        "(%s). Чаще всего так ведёт себя корпоративный прокси или антивирус, "
        "подменяющий сертификаты: установите его корневой сертификат в хранилище "
        "Windows. Молча отключать проверку сертификата нельзя." % (service, detail)
    )


def _explain(status: int, service: str, body: str) -> str | None:
    """Человеческое объяснение кода ответа. None — значит беда временная, повторим."""
    if status in (401, 403):
        return ("%s отклонил ключ доступа (код %d). Проверьте ключ в настройках. %s"
                % (service, status, body))
    if status == 402:
        return ("На счету в %s закончились деньги (код 402). Пополните баланс. %s"
                % (service, body))
    if status == 404:
        return ("%s не нашёл указанную модель или адрес (код 404). Выберите модель "
                "в настройках. %s" % (service, body))
    if status == 413:
        return ("%s отказался принять файл: слишком большой (код 413). %s"
                % (service, body))
    if status == 429 or status >= 500:
        return None
    return "%s отказал (код %d). %s" % (service, status, body)


def _soft(status: int, service: str) -> str:
    if status == 429:
        return ("%s сообщил о превышении лимита обращений (код 429). "
                "Попробуйте позже." % service)
    if status == 503:
        return ("%s сейчас не может обслужить запрос: нет свободных мощностей "
                "(код 503). Попробуйте позже." % service)
    return ("%s отвечает ошибкой на своей стороне (код %d). Попробуйте позже."
            % (service, status))


def _request(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    json_body: Any = None,
    data: Any = None,
    files: Any = None,
    params: Any = None,
    verify: Any = True,
    timeout: float = HTTP_TIMEOUT_S,
    service: str = "сервис",
    secrets: list[str | None] | None = None,
    attempts: int = 2,
) -> Any:
    """Запрос с повтором на временных бедах. Возвращает разобранный JSON."""
    import httpx

    secrets = secrets or []
    last: Exception | None = None
    soft: str | None = None
    for attempt in range(max(1, attempts)):
        try:
            with _client(timeout, verify) as cli:
                resp = cli.request(method, url, headers=headers, json=json_body,
                                   data=data, files=files, params=params)
            if resp.status_code >= 400:
                body = scrub(first_lines(resp.text, limit=2, max_chars=300), secrets)
                hard = _explain(resp.status_code, service, body)
                if hard:
                    raise RuntimeError(hard)
                soft = _soft(resp.status_code, service)
                raise httpx.HTTPStatusError(body, request=resp.request, response=resp)
            if not (resp.content or b"").strip():
                return {}
            try:
                return resp.json()
            except ValueError:
                raise RuntimeError("%s вернул не JSON: %s"
                                   % (service, first_lines(resp.text, 1, 200))) from None
        except RuntimeError:
            raise
        except httpx.HTTPStatusError as err:
            last = err
        except httpx.ConnectError as err:
            last = err
            if _looks_like_tls(err):
                raise RuntimeError(_tls_hint(service, err)) from None
        except httpx.TimeoutException:
            last = RuntimeError("%s не ответил за %d с. Попробуйте позже."
                                % (service, int(timeout)))
        except httpx.HTTPError as err:
            last = err
        if attempt + 1 < max(1, attempts):
            time.sleep(2.0)
    if isinstance(last, RuntimeError):
        raise last
    if soft:
        raise RuntimeError(soft)
    detail = scrub(str(last or ""), secrets)
    raise RuntimeError("Не удалось связаться с %s: %s"
                       % (service, first_lines(detail, 2, 300)))


def http_post(url: str, **kw: Any) -> Any:
    return _request("POST", url, **kw)


def http_get(url: str, **kw: Any) -> Any:
    kw.setdefault("timeout", 60.0)
    return _request("GET", url, **kw)


# ---------------------------------------------------------------- GigaChat: токен

_gc_lock = threading.Lock()
_gc_token: dict[str, Any] = {"value": "", "until": 0.0, "key": ""}


def gigachat_token(key: str) -> str:
    """Обменять authorization key на access_token. Токен живёт около 30 минут."""
    with _gc_lock:
        now = time.time()
        if (_gc_token["value"] and _gc_token["key"] == key_fingerprint(key)
                and _gc_token["until"] > now + 60):
            return str(_gc_token["value"])
        data = http_post(
            "https://ngw.devices.sberbank.ru:9443/api/v2/oauth",
            headers={
                "Authorization": "Basic %s" % key,
                "RqUID": str(uuid.uuid4()),
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            },
            data={"scope": config.get("gigachat_scope") or "GIGACHAT_API_PERS"},
            verify=config.get("gigachat_verify", True),
            timeout=60.0,
            service="GigaChat (выдача токена)",
            secrets=[key],
        )
        token = str((data or {}).get("access_token") or "")
        if not token:
            raise RuntimeError("GigaChat не выдал токен доступа по этому ключу.")
        expires = (data or {}).get("expires_at") or 0
        try:
            # expires_at Сбер присылает в миллисекундах
            until = float(expires) / 1000.0 if float(expires) > 1e11 else now + 1500.0
        except (TypeError, ValueError):
            until = now + 1500.0
        _gc_token.update({"value": token, "until": until, "key": key_fingerprint(key)})
        return token


# ---------------------------------------------------------------- реестр сервисов


def _custom() -> list[dict[str, Any]]:
    raw = config.get("providers")
    return [p for p in (raw or []) if isinstance(p, dict) and p.get("id")]


def all_providers() -> list[dict[str, Any]]:
    """Встроенные плюс добавленные вручную. Одинаковые id не дублируются."""
    out: list[dict[str, Any]] = [dict(p) for p in BUILTIN]
    seen = set(_BUILTIN_IDS)
    for p in _custom():
        pid = str(p.get("id"))
        if pid in seen:
            continue
        seen.add(pid)
        out.append({
            "id": pid,
            "title": str(p.get("title") or pid),
            "kind": str(p.get("kind") or "openai"),
            "base_url": str(p.get("base_url") or "").rstrip("/"),
            "service": str(p.get("title") or pid),
            "needs": [],
            "custom": True,
        })
    return out


def find(pid: str) -> dict[str, Any] | None:
    pid = str(pid or "").strip()
    for p in all_providers():
        if p["id"] == pid:
            return p
    return None


def require(pid: str) -> dict[str, Any]:
    p = find(pid)
    if p is None:
        raise RuntimeError(
            "Сервис «%s» не найден в настройках. Выберите другой в разделе «Модели»." % pid
        )
    return p


def current_id() -> str:
    """Выбранный сервис для документов (протокол и саммари)."""
    pid = str(config.get("api_provider") or "anthropic").strip()
    return pid if find(pid) else "anthropic"


def asr_id() -> str:
    """Выбранный сервис для облачного распознавания речи."""
    pid = str(config.get("asr_provider") or "").strip()
    if pid and find(pid):
        return pid
    return "polza" if find("polza") else current_id()


def api_key(pid: str) -> str:
    keys = config.get("api_keys") or {}
    if not isinstance(keys, dict):
        return ""
    return str(keys.get(pid) or "").strip()


def public_list() -> list[dict[str, Any]]:
    """Для интерфейса: без ключей, только признак «задан» и маска."""
    out = []
    for p in all_providers():
        key = api_key(p["id"])
        out.append({
            "id": p["id"],
            "title": p["title"],
            "kind": p["kind"],
            "base_url": p["base_url"],
            "custom": bool(p.get("custom")),
            "has_key": bool(key),
            "key_hint": mask(key),
        })
    return out


_ID_CLEAN_RE = re.compile(r"[^a-z0-9_-]+")


def _id_from_url(base_url: str, title: str) -> str:
    host = (urlsplit(base_url).hostname or title or "service").lower()
    host = host.replace("www.", "").split(".")[0]
    pid = _ID_CLEAN_RE.sub("-", host).strip("-") or "service"
    taken = {p["id"] for p in all_providers()}
    if pid not in taken:
        return pid
    for n in range(2, 100):
        if f"{pid}-{n}" not in taken:
            return f"{pid}-{n}"
    raise RuntimeError("Слишком много сервисов с похожим именем.")


def validate_base_url(raw: str) -> str:
    """Проверить адрес сервиса. Возвращает очищенный адрес либо бросает ошибку.

    Проверка не формальность: поле «добавить сервис» — это возможность заставить
    приложение сходить по произвольному адресу. Без запрета на localhost и
    домашнюю сеть его можно было бы навести на что угодно внутри этой машины.
    """
    text = str(raw or "").strip().rstrip("/")
    if not text:
        raise ValueError("Не указан адрес сервиса.")
    if "\n" in text or "\r" in text or " " in text:
        raise ValueError("В адресе сервиса не должно быть пробелов и переводов строки.")
    parts = urlsplit(text)
    if parts.scheme != "https":
        raise ValueError("Адрес должен начинаться с https:// — обычный http небезопасен.")
    host = (parts.hostname or "").lower()
    if not host:
        raise ValueError("В адресе не разобрать имя сервера.")
    if host in ("localhost", "localhost.localdomain") or host.endswith(".local"):
        raise ValueError("Адрес на этом же компьютере указывать нельзя.")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None and (ip.is_loopback or ip.is_private or ip.is_link_local
                           or ip.is_reserved):
        raise ValueError("Адрес во внутренней сети указывать нельзя.")
    return text


def add(title: str, base_url: str, kind: str = "openai", key: str = "") -> dict[str, Any]:
    """Добавить свой сервис. Ключ, если передан, кладётся в общее хранилище ключей."""
    url = validate_base_url(base_url)
    kind = str(kind or "openai").strip() or "openai"
    if kind not in ("openai", "anthropic", "gigachat", "yandexgpt"):
        raise ValueError("Неизвестный вид сервиса: %s" % kind)
    name = str(title or "").strip() or (urlsplit(url).hostname or "Свой сервис")
    if "\n" in name or "\r" in name:
        raise ValueError("В названии сервиса не должно быть переводов строки.")
    pid = _id_from_url(url, name)
    entry = {"id": pid, "title": name[:60], "kind": kind, "base_url": url}
    items = _custom() + [entry]
    patch: dict[str, Any] = {"providers": items}
    if (key or "").strip():
        patch["api_keys"] = {pid: key.strip()}
    config.save(patch)
    log.info("добавлен сервис %s (%s)", pid, url)
    return entry


def remove(pid: str) -> bool:
    """Убрать свой сервис. Встроенные не удаляются."""
    pid = str(pid or "").strip()
    if pid in _BUILTIN_IDS:
        raise ValueError("Встроенный сервис удалить нельзя.")
    items = [p for p in _custom() if str(p.get("id")) != pid]
    if len(items) == len(_custom()):
        return False
    patch: dict[str, Any] = {"providers": items, "api_keys": {pid: ""}}
    if str(config.get("api_provider") or "") == pid:
        patch["api_provider"] = "anthropic"
    if str(config.get("asr_provider") or "") == pid:
        patch["asr_provider"] = ""
    config.save(patch)
    _models_cache.pop(pid, None)
    log.info("удалён сервис %s", pid)
    return True


# ---------------------------------------------------------------- списки моделей

_models_cache: dict[str, dict[str, Any]] = {}
_models_lock = threading.Lock()
CACHE_TTL_S = 3600.0


def _auth_headers(p: dict[str, Any], key: str) -> dict[str, str]:
    kind = p["kind"]
    if kind == "anthropic":
        return {"x-api-key": key, "anthropic-version": "2023-06-01"}
    if kind == "gigachat":
        return {"Authorization": "Bearer %s" % gigachat_token(key)}
    return {"Authorization": "Bearer %s" % key} if key else {}


def _price_of(item: dict[str, Any]) -> dict[str, Any]:
    """Цены, если сервис их отдаёт. У polza они приходят прямо в списке моделей."""
    pricing = item.get("pricing")
    if not isinstance(pricing, dict):
        return {}
    out: dict[str, Any] = {}
    for key, dst in (("stt_per_minute", "per_minute"),
                     ("prompt_per_million", "in_per_million"),
                     ("completion_per_million", "out_per_million")):
        val = pricing.get(key)
        if val in (None, ""):
            continue
        try:
            out[dst] = float(val)
        except (TypeError, ValueError):
            continue
    if out:
        out["currency"] = str(pricing.get("currency") or "").strip()
    return out


def _entry(item: Any) -> dict[str, Any] | None:
    if isinstance(item, str):
        return {"id": item, "title": item, "price": {}}
    if not isinstance(item, dict):
        return None
    mid = str(item.get("id") or item.get("model") or item.get("name") or "").strip()
    if not mid:
        return None
    title = str(item.get("name") or item.get("display_name") or mid).strip()
    return {"id": mid, "title": title if title != mid else mid, "price": _price_of(item)}


def _fetch_models(p: dict[str, Any], key: str) -> dict[str, list[dict[str, Any]]]:
    """Спросить у сервиса список моделей. Разделение на чат и распознавание."""
    kind = p["kind"]
    if kind == "yandexgpt":
        # У Яндекса списка моделей нет вовсе: имена задокументированы и вписаны руками.
        return {
            "chat": [{"id": "yandexgpt-5.1", "title": "yandexgpt-5.1", "price": {}},
                     {"id": "yandexgpt-5-lite", "title": "yandexgpt-5-lite", "price": {}}],
            "stt": [],
        }
    base = p["base_url"].rstrip("/")
    service = p.get("service") or p["title"]
    headers = _auth_headers(p, key)

    chat: list[dict[str, Any]] = []
    stt: list[dict[str, Any]] = []

    def collect(payload: Any) -> list[dict[str, Any]]:
        items = payload.get("data") if isinstance(payload, dict) else payload
        if not isinstance(items, list):
            return []
        out = []
        for it in items:
            e = _entry(it)
            if e is not None:
                out.append(e)
        return out

    # У polza (и у совместимых) есть фильтр по типу: отдельный список моделей
    # распознавания. Значение type=stt рабочее, а описанное в документации
    # type=audio возвращает пустой список — проверено живым запросом.
    if kind == "openai":
        try:
            stt = collect(http_get("%s/models" % base, headers=headers,
                                   params={"type": "stt"}, service=service,
                                   secrets=[key], attempts=1))
        except RuntimeError as err:
            log.info("список моделей распознавания не получен (%s)", first_lines(str(err), 1, 120))
    every = collect(http_get("%s/models" % base, headers=headers, service=service,
                             secrets=[key]))
    known = {m["id"] for m in stt}
    for m in every:
        if m["id"] in known:
            continue
        (stt if _ASR_RE.search(m["id"]) else chat).append(m)
    chat.sort(key=lambda m: m["id"])
    stt.sort(key=lambda m: m["id"])
    return {"chat": chat, "stt": stt}


def list_models(pid: str, refresh: bool = False) -> dict[str, Any]:
    """Список моделей сервиса. Ходит в сеть только по требованию и кэшируется.

    Автоматически при каждом открытии настроек не обновляем намеренно: каждый
    выход в сеть — лишний повод для окна антивируса, а список меняется редко.
    """
    p = require(pid)
    with _models_lock:
        hit = _models_cache.get(pid)
        if hit and not refresh and (time.time() - hit["at"]) < CACHE_TTL_S:
            return dict(hit, cached=True)
    key = api_key(pid)
    if not key and p["kind"] != "openai":
        raise RuntimeError("Для «%s» не задан ключ — список моделей не спросить."
                           % p["title"])
    data = _fetch_models(p, key)
    payload = {"provider": pid, "at": time.time(), "chat": data["chat"], "stt": data["stt"]}
    with _models_lock:
        _models_cache[pid] = payload
    log.info("сервис %s: моделей чата %d, распознавания %d",
             pid, len(data["chat"]), len(data["stt"]))
    return dict(payload, cached=False)


def cached_models(pid: str) -> dict[str, Any] | None:
    with _models_lock:
        hit = _models_cache.get(pid)
        return dict(hit, cached=True) if hit else None
