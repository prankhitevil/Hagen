# -*- coding: utf-8 -*-
"""Отправка поручений наружу, в Todoist (решение 17.09).

Почему наружу, а не внутрь. Владелец 17.09: «не заводить задачи у себя, а
отправлять». Своего списка задач в программе нет и не будет — псевдо-Obsidian
внутри приложения он делать отказался.

Это ЕДИНСТВЕННОЕ место программы, где действие уходит за пределы компьютера и
его нельзя отменить кнопкой «отмена». Отсюда три обязательных заслона, и они
живут не в интерфейсе, а здесь, в службе:

  1. **Показ до отправки.** Точка `preview` ничего не отправляет: она только
     говорит, что будет отправлено и что уже отправляли раньше.
  2. **Явное подтверждение.** `send` требует поле confirmed; без него —
     отказ, а не «ну ладно, отправлю».
  3. **Отметка «уже отправлено».** Хранится в записи по нормализованному тексту
     поручения. Повторный разбор той же встречи не создаст вторых копий, даже
     если формулировка слегка изменилась.

Токен — личный, из Todoist (Settings → Integrations → Developer). Он секрет:
наружу отдаётся только «задан/не задан», из сборки для чужого человека
вычищается (`make_portable.SECRET_KEYS`).
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

import httpx

from . import config, store

log = logging.getLogger("hagen.todoist")

API = "https://api.todoist.com/rest/v2"
TIMEOUT = 20.0

#: Сколько поручений разрешаем отправить за один раз. Заслон от «выделил всё
#: подряд на часовой встрече и залил в Todoist сорок строк».
MAX_BATCH = 25


class TodoistError(RuntimeError):
    """Ошибка, которую можно показать человеку по-русски."""


def token() -> str:
    return str(config.get("todoist_token") or "").strip()


def configured() -> bool:
    return bool(token())


def _require_token() -> None:
    """Проверка токена отдельно от слоя запросов.

    Чтобы отсутствие токена ловилось до любой подготовки запроса, а не внутри
    неё: так заслон не зависит от того, как устроен сетевой слой.
    """
    if not configured():
        raise TodoistError(
            "Токен Todoist не задан. Настройки → Документы: возьмите личный токен в "
            "Todoist (Settings → Integrations → Developer) и вставьте его туда."
        )


def _headers() -> dict[str, str]:
    _require_token()
    return {"Authorization": "Bearer %s" % token(), "Content-Type": "application/json"}


def _explain(err: Exception) -> str:
    """Понятная по-русски причина. Токен в текст не попадает никогда."""
    if isinstance(err, httpx.HTTPStatusError):
        code = err.response.status_code
        if code in (401, 403):
            return ("Todoist не принял токен. Проверьте его в «Настройки → Документы»: "
                    "возможно, он отозван или скопирован не полностью.")
        if code == 404:
            return "Todoist не нашёл проект: возможно, его удалили. Выберите другой."
        if code == 429:
            return "Todoist просит подождать: слишком много запросов подряд. Попробуйте позже."
        return "Todoist ответил ошибкой %d." % code
    if isinstance(err, httpx.TimeoutException):
        return "Todoist не ответил вовремя. Проверьте интернет и попробуйте ещё раз."
    if isinstance(err, httpx.HTTPError):
        return ("Не удалось связаться с Todoist. Проверьте интернет; если включён "
                "корпоративный фильтр или VPN, он мог закрыть доступ.")
    return "Не удалось отправить задачу: %s" % err


def _request(method: str, path: str, **kw: Any) -> Any:
    try:
        with httpx.Client(timeout=TIMEOUT) as cli:
            res = cli.request(method, API + path, headers=_headers(), **kw)
            res.raise_for_status()
            return res.json() if res.content else None
    except TodoistError:
        raise
    except Exception as err:                       # noqa: BLE001 — переводим на русский
        log.warning("Todoist %s %s: %s", method, path, err)
        raise TodoistError(_explain(err)) from None


def projects() -> list[dict[str, str]]:
    """Проекты Todoist: куда можно класть задачи."""
    _require_token()
    data = _request("GET", "/projects") or []
    return [{"id": str(p.get("id") or ""), "name": str(p.get("name") or "")}
            for p in data if p.get("id")]


# --------------------------------------------------------------------------- #
# что уже отправляли
# --------------------------------------------------------------------------- #
def sent_map(meta: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Отправленное этой записью: нормализованный текст → что и когда."""
    raw = (meta or {}).get("sent_tasks")
    out: dict[str, dict[str, Any]] = {}
    if isinstance(raw, dict):
        for key, value in raw.items():
            norm = store.norm_line(key)
            if norm and isinstance(value, dict):
                out[norm] = value
    return out


def preview(rec_id: str) -> dict[str, Any]:
    """Что можно отправить и что уже отправлено. НИЧЕГО не отправляет."""
    from . import minutes

    meta = store.get(rec_id)
    if meta is None:
        raise TodoistError("Запись не найдена.")
    sent = sent_map(meta)
    items = []
    for text in minutes.open_items(rec_id):
        norm = store.norm_line(text)
        was = sent.get(norm)
        items.append({
            "text": text,
            "clean": clean_task(text),
            "sent": bool(was),
            "sent_at": (was or {}).get("at", ""),
            "url": (was or {}).get("url", ""),
        })
    return {
        "configured": configured(),
        "items": items,
        "project": config.get("todoist_project") or {},
        "max_batch": MAX_BATCH,
    }


#: Строка таблицы протокола: | Задача | Ответственный | Срок |
def clean_task(text: str) -> str:
    """Превратить строку документа в человеческий текст задачи.

    Строка таблицы протокола разбирается на части: задача, ответственный, срок.
    Ответственный и срок дописываются в скобках, потому что в Todoist они уходят
    в чужой проект, где без них непонятно, о чём речь. Срок НЕ превращается в
    дату задачи: в протоколе он записан словами («к четвергу»), и угадывать за
    человека календарную дату — ровно та ошибка, которой мы избегаем в промптах.
    """
    body = str(text or "").strip()
    if body.startswith("|") and body.count("|") >= 3:
        cells = [c.strip() for c in body.strip("|").split("|")]
        task = cells[0] if cells else ""
        who = cells[1] if len(cells) > 1 else ""
        due = cells[2] if len(cells) > 2 else ""
        extra = [p for p in (who, due)
                 if p and p.lower() not in ("не определён", "не указан", "-", "—", "нет")]
        return ("%s (%s)" % (task, ", ".join(extra))) if extra else task
    return body.lstrip("-*+ ").strip()


def send(rec_id: str, texts: list[str], project_id: str = "",
         confirmed: bool = False) -> dict[str, Any]:
    """Отправить выбранные поручения в Todoist.

    Без confirmed=True не отправляет ничего: подтверждение — часть договора с
    человеком, а не украшение интерфейса.
    """
    if not confirmed:
        raise TodoistError("Отправка не подтверждена.")
    _require_token()
    meta = store.get(rec_id)
    if meta is None:
        raise TodoistError("Запись не найдена.")
    chosen = [str(t or "").strip() for t in (texts or []) if str(t or "").strip()]
    if not chosen:
        raise TodoistError("Не выбрано ни одного поручения.")
    if len(chosen) > MAX_BATCH:
        raise TodoistError("За раз отправляем не больше %d поручений." % MAX_BATCH)

    project_id = str(project_id or "").strip()
    sent = sent_map(meta)
    title = str(meta.get("title") or "").strip()
    created: list[dict[str, Any]] = []
    skipped: list[str] = []
    failed: list[dict[str, str]] = []

    for text in chosen:
        norm = store.norm_line(text)
        if norm in sent:
            skipped.append(text)
            continue
        body: dict[str, Any] = {"content": clean_task(text)}
        if project_id:
            body["project_id"] = project_id
        if title:
            body["description"] = "Из записи «%s» (Hagen)." % title
        try:
            task = _request("POST", "/tasks", json=body) or {}
        except TodoistError as err:
            failed.append({"text": text, "error": str(err)})
            break          # сеть или токен: дальше по списку будет то же самое
        sent[norm] = {"id": str(task.get("id") or ""), "url": str(task.get("url") or ""),
                      "at": datetime.now().isoformat(timespec="seconds"), "text": text}
        created.append({"text": text, "url": str(task.get("url") or "")})

    if created:
        store.update(rec_id, {"sent_tasks": {v.get("text", k): v for k, v in sent.items()}})
    return {"created": created, "skipped": skipped, "failed": failed}
