# -*- coding: utf-8 -*-
"""Облачные сервисы: подключения «адрес, ключ, модель» и единственная точка выхода в сеть.

Подключений два, у каждого свои адрес, ключ и модель (решение 21.09):
  docs — документы по ключу (протокол, саммари), когда их делает не Claude CLI;
  asr  — облачное распознавание файлов.
Раздельно — потому что их часто держат у разных сервисов: документы, например,
пишет подписка Claude, а распознаёт агрегатор. Прежде вместо этого был список из
полутора десятков сервисов; каждый из них отличался только адресом, а выбор в
списке молча переключал сервис, — поэтому остались поля.

Как разговаривать с сервисом, программа узнаёт по адресу: почти все говорят на
языке OpenAI, а Anthropic, GigaChat и YandexGPT — каждый по-своему. Для
необычного адреса вид задаётся руками (настройка api_kind).

Почему сеть собрана здесь, а не размазана по модулям:
  * бывает включён локальный VPN в режиме системного прокси, и запросы,
    уходящие через него, рвутся на проверке сертификата — поэтому ВСЕ клиенты
    создаются с trust_env=False;
  * Kaspersky подменяет сертификаты сайтов, из-за чего Python не верит никому —
    лечится truststore.inject_into_ssl() в config.py, и он обязан отработать до
    первого обращения, поэтому config импортируется здесь первым;
  * ключ не должен попасть ни в лог, ни в текст ошибки — вычистка в одном месте.

Ключи хранятся в settings.json (api_keys: docs, asr) и наружу отдаются только маской.
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

#: Подключения и что они обслуживают.
ROLES: dict[str, str] = {
    "docs": "документы",
    "asr": "облачное распознавание",
}

#: Как разговаривает сервис: вид → подпись для окна.
KINDS: dict[str, str] = {
    "openai": "как OpenAI",
    "anthropic": "как Anthropic",
    "gigachat": "как GigaChat",
    "yandexgpt": "как YandexGPT",
}

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
        return ("%s не нашёл указанную модель или адрес (код 404). Проверьте адрес "
                "и модель в настройках. %s" % (service, body))
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


# ---------------------------------------------------------------- подключения


def kind_for(base_url: str) -> str:
    """Как разговаривает сервис по этому адресу. Почти все — как OpenAI."""
    parts = urlsplit(str(base_url or "").strip())
    host = (parts.hostname or "").lower()
    if host == "anthropic.com" or host.endswith(".anthropic.com"):
        return "anthropic"
    if "gigachat" in host:
        return "gigachat"
    # У Яндекса по одному имени сервера два разговора: свой и совместимый с
    # OpenAI. Совместимый живёт под /v1, и в нём имя модели пишется целиком:
    # gpt://<каталог>/<модель>/latest.
    if host == "llm.api.cloud.yandex.net" and not parts.path.startswith("/v1"):
        return "yandexgpt"
    return "openai"


def service_name(base_url: str) -> str:
    """Как назвать сервис в сообщениях: имя сервера из адреса."""
    host = (urlsplit(str(base_url or "").strip()).hostname or "").lower()
    return host or "сервис"


def api_key(role: str) -> str:
    keys = config.get("api_keys") or {}
    if not isinstance(keys, dict):
        return ""
    return str(keys.get(role) or "").strip()


def connection(role: str) -> dict[str, Any]:
    """Подключение целиком: адрес, вид, модель, ключ. Ключ — только внутрь программы."""
    if role == "docs":
        url = str(config.get("api_base_url") or "")
        manual = str(config.get("api_kind") or "").strip()
        model = str(config.get("api_model") or "")
        folder = str(config.get("api_folder") or "")
    elif role == "asr":
        url = str(config.get("asr_base_url") or "")
        manual = "openai"   # распознавание — только по образцу OpenAI (audio/transcriptions)
        model = str(config.get("asr_model") or "")
        folder = ""
    else:
        raise ValueError("Неизвестное подключение: %s" % role)
    url = url.strip().rstrip("/")
    return {
        "role": role,
        "base_url": url,
        "kind": manual if manual in KINDS else kind_for(url),
        "kind_manual": manual in KINDS and role == "docs",
        "model": model.strip(),
        "folder": folder.strip(),
        "key": api_key(role),
        "service": service_name(url),
    }


def problem(role: str) -> str:
    """Чего не хватает подключению, человеческим языком. Пусто — можно работать.

    Модель здесь не проверяется: у документов она может подобраться сама по
    виду сервиса, это решают те, кто подключением пользуется.
    """
    c = connection(role)
    if not c["base_url"]:
        return "Не указан адрес сервиса (%s)." % ROLES[role]
    try:
        validate_base_url(c["base_url"])
    except ValueError as err:
        return "Адрес сервиса не годится: %s" % err
    if not c["key"]:
        return "Не задан ключ для %s." % c["service"]
    if c["kind"] == "yandexgpt" and not c["folder"]:
        return "Для YandexGPT не указан идентификатор каталога."
    return ""


def public_connections() -> dict[str, dict[str, Any]]:
    """Для интерфейса: без ключей, только признак «задан», маска и что не так."""
    out: dict[str, dict[str, Any]] = {}
    for role in ROLES:
        c = connection(role)
        key = c.pop("key")
        c["has_key"] = bool(key)
        c["key_hint"] = mask(key)
        c["kind_title"] = KINDS.get(c["kind"], c["kind"])
        c["problem"] = problem(role)
        out[role] = c
    return out


def validate_base_url(raw: str) -> str:
    """Проверить адрес сервиса. Возвращает очищенный адрес либо бросает ошибку.

    Проверка не формальность: поле адреса — это возможность заставить
    приложение сходить по произвольному адресу с ключом. Без запрета на
    localhost и домашнюю сеть его можно было бы навести на что угодно внутри
    этой машины.
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


# ---------------------------------------------------------------- перенос со списка

#: Прежний список сервисов (до 21.09) — только чтобы перенести настройки тех,
#: кто выбирал сервис из него. Вид по этим адресам определяется сам.
_OLD_ADDRESSES: dict[str, str] = {
    "anthropic": "https://api.anthropic.com/v1",
    "openai": "https://api.openai.com/v1",
    "gigachat": "https://gigachat.devices.sberbank.ru/api/v1",
    "yandexgpt": "https://llm.api.cloud.yandex.net",
    "polza": "https://polza.ai/api/v1",
    "yandex_ai_studio": "https://llm.api.cloud.yandex.net/v1",
    "provod": "https://api.provod.ai/v1",
    "aitunnel": "https://api.aitunnel.ru/v1",
    "proxyapi": "https://api.proxyapi.ru/openai/v1",
    "gptunnel": "https://gptunnel.ru/v1",
    "llmrouter": "https://llm-router.org/v1",
    "openrouter": "https://openrouter.ai/api/v1",
    "deepseek": "https://api.deepseek.com/v1",
    "zai": "https://api.z.ai/api/paas/v4",
    "moonshot": "https://api.moonshot.ai/v1",
    "groq": "https://api.groq.com/openai/v1",
    "gemini": "https://generativelanguage.googleapis.com/v1beta/openai",
}

#: Подсказка к полю адреса: где взять сервис. Российские — в рублях и без VPN;
#: у Groq и Gemini есть бесплатный тариф; OpenRouter — только с иностранным
#: аккаунтом и картой: с 27.06.2026 он отказывает запросам из России.
SUGGESTED: list[dict[str, str]] = [
    {"title": "polza.ai", "base_url": _OLD_ADDRESSES["polza"],
     "note": "рубли, много моделей, есть распознавание речи"},
    {"title": "RouterAI", "base_url": "https://routerai.ru/api/v1",
     "note": "рубли, карта или СБП, есть распознавание речи"},
    {"title": "LLM Router", "base_url": _OLD_ADDRESSES["llmrouter"], "note": "рубли и СБП"},
    {"title": "provod.ai", "base_url": _OLD_ADDRESSES["provod"],
     "note": "рубли, документы для юрлица"},
    {"title": "AITunnel", "base_url": _OLD_ADDRESSES["aitunnel"], "note": "рубли, 200+ моделей"},
    {"title": "ProxyAPI", "base_url": _OLD_ADDRESSES["proxyapi"], "note": "рубли"},
    {"title": "GPTunnel", "base_url": _OLD_ADDRESSES["gptunnel"], "note": "рубли"},
    {"title": "GigaChat", "base_url": _OLD_ADDRESSES["gigachat"],
     "note": "Сбер: свой ключ и корневой сертификат"},
    {"title": "Yandex AI Studio", "base_url": _OLD_ADDRESSES["yandex_ai_studio"],
     "note": "модель пишется gpt://каталог/модель/latest"},
    {"title": "Groq", "base_url": _OLD_ADDRESSES["groq"], "note": "есть бесплатный тариф"},
    {"title": "Google Gemini", "base_url": _OLD_ADDRESSES["gemini"],
     "note": "есть бесплатный тариф"},
    {"title": "OpenRouter", "base_url": _OLD_ADDRESSES["openrouter"],
     "note": "нужен иностранный аккаунт и карта"},
]

#: Модели, которые прежний список подставлял сам, если человек не выбирал.
#: У Anthropic, GigaChat и YandexGPT модель по-прежнему подбирается по виду.
_OLD_MODELS: dict[str, str] = {
    "openai": "gpt-5.6-sol",
    "polza": "anthropic/claude-sonnet-4.6",
}

#: Не сервисы, а соседи по словарю ключей.
_NOT_SERVICES = ("docs", "asr", "yandexgpt_folder")


def adopt_old_services() -> bool:
    """Разовый перенос с прежнего списка сервисов на «адрес, ключ, модель».

    У того, кто обновляется, облачный путь уже настроен, и молча пропасть он
    не должен. Документы берут выбранный тогда сервис; распознавание — свой,
    по умолчанию polza. Прежние ключи в файле остаются как были. Делается один
    раз: отметка `services_adopted` в настройках. Возвращает, был ли перенос.
    """
    if config.get("services_adopted"):
        return False
    keys = config.get("api_keys") or {}
    keys = dict(keys) if isinstance(keys, dict) else {}
    models = config.get("api_models") or {}
    models = dict(models) if isinstance(models, dict) else {}
    custom = {str(p.get("id")): p for p in (config.get("providers") or [])
              if isinstance(p, dict) and p.get("id")}

    def address(pid: str) -> tuple[str, str]:
        if pid in custom:
            return (str(custom[pid].get("base_url") or "").rstrip("/"),
                    str(custom[pid].get("kind") or ""))
        return _OLD_ADDRESSES.get(pid, ""), ""

    patch: dict[str, Any] = {"services_adopted": True}
    new_keys: dict[str, str] = {}

    pid = str(config.get("api_provider") or "").strip()
    with_key = [p for p, v in keys.items() if v and p not in _NOT_SERVICES]
    if pid and not keys.get(pid) and len(with_key) == 1:
        # Выбранный сервис без ключа, а ключ есть ровно у одного: выбор в
        # списке сохранялся сразу, и он остался от просмотра списка.
        pid = with_key[0]
    url, kind = address(pid) if pid else ("", "")
    if url and not str(config.get("api_base_url") or "").strip():
        patch["api_base_url"] = url
        if kind in KINDS and kind != kind_for(url):
            patch["api_kind"] = kind
        patch["api_model"] = str(models.get(pid) or _OLD_MODELS.get(pid, ""))
        if keys.get(pid):
            new_keys["docs"] = str(keys[pid])
        if keys.get("yandexgpt_folder"):
            patch["api_folder"] = str(keys["yandexgpt_folder"])

    apid = str(config.get("asr_provider") or "").strip() or "polza"
    url, _kind = address(apid)
    if url and keys.get(apid) and not keys.get("asr"):
        patch["asr_base_url"] = url
        new_keys["asr"] = str(keys[apid])

    if new_keys:
        patch["api_keys"] = new_keys
    config.save(patch)
    log.info("сервисы перенесены со списка: документы %s, распознавание %s",
             patch.get("api_base_url") or "—", patch.get("asr_base_url") or "как было")
    return True


# ---------------------------------------------------------------- списки моделей

_models_cache: dict[str, dict[str, Any]] = {}
_models_lock = threading.Lock()
CACHE_TTL_S = 3600.0


def _auth_headers(kind: str, key: str) -> dict[str, str]:
    if kind == "anthropic":
        return {"x-api-key": key, "anthropic-version": "2023-06-01"}
    if kind == "gigachat":
        return {"Authorization": "Bearer %s" % gigachat_token(key)}
    return {"Authorization": "Bearer %s" % key} if key else {}


def _price_of(item: dict[str, Any]) -> dict[str, Any]:
    """Цены, если сервис их отдаёт — только тогда: таблиц цен в программе нет.

    Два известных вида записи в списке моделей:
      * polza — за миллион токенов, с валютой (prompt_per_million,
        completion_per_million, stt_per_minute, currency). У моделей чата цена
        лежит не в самой модели, а в top_provider.pricing — проверено живым
        запросом 23.09;
      * OpenRouter — за ОДИН токен строкой в долларах (prompt, completion);
        «-1» там значит «цена плавающая», такую не показываем.
    На выходе всегда одно: in_per_million, out_per_million, per_minute, currency.
    """
    pricing = item.get("pricing")
    if not isinstance(pricing, dict):
        top = item.get("top_provider")
        pricing = top.get("pricing") if isinstance(top, dict) else None
    if not isinstance(pricing, dict):
        return {}
    out: dict[str, Any] = {}
    fields = (("stt_per_minute", "per_minute", 1.0),
              ("prompt_per_million", "in_per_million", 1.0),
              ("completion_per_million", "out_per_million", 1.0),
              ("prompt", "in_per_million", 1e6),
              ("completion", "out_per_million", 1e6))
    for key, dst, scale in fields:
        val = pricing.get(key)
        if val in (None, "") or dst in out:
            continue
        try:
            num = float(val)
        except (TypeError, ValueError):
            continue
        if num < 0:
            continue
        out[dst] = num * scale
    if out:
        currency = str(pricing.get("currency") or "").strip()
        if not currency and ("prompt" in pricing or "completion" in pricing):
            currency = "USD"
        out["currency"] = currency
    return out


def model_price(role: str, model_id: str) -> dict[str, Any]:
    """Цена модели по уже полученному списку. Пусто — список не спрашивали,
    модели в нём нет или сервис цен не сообщает."""
    cached = cached_models(role) or {}
    want = str(model_id or "").strip()
    if not want:
        return {}
    for group in ("chat", "stt"):
        for item in cached.get(group) or []:
            if item.get("id") == want:
                return dict(item.get("price") or {})
    return {}


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


def _fetch_models(c: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """Спросить у сервиса список моделей. Разделение на чат и распознавание."""
    kind = c["kind"]
    if kind == "yandexgpt":
        # У Яндекса списка моделей нет вовсе: имена задокументированы и вписаны руками.
        return {
            "chat": [{"id": "yandexgpt-5.1", "title": "yandexgpt-5.1", "price": {}},
                     {"id": "yandexgpt-5-lite", "title": "yandexgpt-5-lite", "price": {}}],
            "stt": [],
        }
    base = c["base_url"]
    service = c["service"]
    key = c["key"]
    headers = _auth_headers(kind, key)

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


def list_models(role: str, refresh: bool = False) -> dict[str, Any]:
    """Список моделей сервиса этого подключения. В сеть — только по требованию.

    Автоматически при каждом открытии настроек не обновляем намеренно: каждый
    выход в сеть — лишний повод для окна антивируса, а список меняется редко.
    Кэш привязан к адресу: сменили адрес — прежний список не показываем.
    """
    c = connection(role)
    if not c["base_url"]:
        raise RuntimeError("Сначала укажите адрес сервиса.")
    try:
        validate_base_url(c["base_url"])
    except ValueError as err:
        raise RuntimeError("Адрес сервиса не годится: %s" % err) from None
    with _models_lock:
        hit = _models_cache.get(role)
        if (hit and not refresh and hit["base_url"] == c["base_url"]
                and (time.time() - hit["at"]) < CACHE_TTL_S):
            return dict(hit, cached=True)
    if not c["key"] and c["kind"] != "openai":
        raise RuntimeError("Для %s не задан ключ — список моделей не спросить." % c["service"])
    data = _fetch_models(c)
    payload = {"role": role, "base_url": c["base_url"], "at": time.time(),
               "chat": data["chat"], "stt": data["stt"]}
    with _models_lock:
        _models_cache[role] = payload
    log.info("%s: моделей чата %d, распознавания %d",
             c["service"], len(data["chat"]), len(data["stt"]))
    return dict(payload, cached=False)


def cached_models(role: str) -> dict[str, Any] | None:
    """Уже полученный список — если он о нынешнем адресе подключения."""
    url = connection(role)["base_url"]
    with _models_lock:
        hit = _models_cache.get(role)
        if hit and hit["base_url"] == url:
            return dict(hit, cached=True)
    return None
