# -*- coding: utf-8 -*-
"""«Отправить»: документы записи — письмом или в Telegram (решение 20.09).

Дверь наружу, как `todoist.py`: ядро говорит «отправь вот это туда-то», а во
что это превращается — почтовое письмо, ссылка `mailto:` или окно Telegram —
знает только этот модуль. Сам он ни в Windows, ни в Outlook не лезет: письмо
открывает розетка `desktop.compose_mail`, ссылку — `system.open_link`.

**Письмо программа не отправляет, а показывает.** Отправка наружу необратима,
поэтому последнее слово за человеком — так же, как с задачами в Todoist.

**Почему вложением** (решение 20.09). Оба системных пути передают
текст внутри самой ссылки, а её длину Windows и программы обрезают на тысячах
знаков: протокол часовой встречи туда не влезет. Классический Outlook доступен
через COM — ему можно отдать вложение, и длина перестаёт быть препятствием.
Нет классического Outlook — честно уходим на `mailto:` с короткой темой и
говорим, где лежат файлы, чтобы приложить их руками.

**Telegram** вложений через ссылку не принимает вовсе. Там путь другой: текст
документа в разметке Telegram кладётся на страницу, а модуль открывает клиент.
"""
from __future__ import annotations

import logging
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import quote

from . import config, platform, store

log = logging.getLogger("hagen.share")

#: Куда можно отправить. Больше двух путей у нас нет и не планируется:
#: остальное — это уже помощник по переписке, а не наш класс задач.
TARGETS = ("mail", "telegram")

#: Сколько знаков документа влезает в ссылку `mailto:`. Windows обрезает её
#: примерно на двух тысячах, поэтому в тело письма-ссылки кладём только начало
#: и говорим, что остальное — в файлах.
MAILTO_LIMIT = 1200

#: То же для Telegram: текст едет внутри ссылки `tg://msg_url`, и длинный
#: документ в неё не влезает. Начало отправляем ссылкой — ради него Telegram и
#: откроет выбор чата, — а целиком документ лежит в буфере обмена.
TG_LIMIT = 1500

#: Имя файла вложения не должно повторять чужие приёмы имён записей: здесь
#: свои правила, потому что файл увидит адресат.
_BAD_NAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')


def _safe_file_name(text: str, limit: int = 80) -> str:
    name = _BAD_NAME.sub(" ", str(text or "")).strip()
    name = re.sub(r"\s+", " ", name).strip(" .")
    return name[:limit] or "Документ"


def available(rec_id: str) -> dict[str, Any]:
    """Что у записи можно отправить: документы и стенограмма. Ничего не шлёт.

    Показ до отправки — тот же порядок, что у задач в Todoist: человек сначала
    видит список и отмечает нужное, и только потом что-то уходит наружу.
    """
    from . import minutes

    meta = store.get(rec_id)
    if meta is None:
        raise ValueError("Запись не найдена")
    items: list[dict[str, Any]] = []
    for doc in minutes.stored_documents(rec_id):
        items.append({"key": doc["key"], "title": doc["title"], "kind": "document"})
    if store.sorted_segments(rec_id):
        items.append({"key": "transcript", "title": "Стенограмма", "kind": "transcript"})
    kind = platform.desktop().outlook_kind()
    return {
        "items": items,
        "title": str(meta.get("title") or "Запись"),
        # Классический Outlook — единственный путь, где вложение доезжает само.
        "mail_attachments": bool(kind.get("com_available")),
        "mail_note": str(kind.get("note") or ""),
    }


def _document_text(rec_id: str, key: str) -> tuple[str, str]:
    """(заголовок, текст) одного отправляемого куска. Пусто — такого нет.

    Вычеркнутые пункты убираются здесь же, как на пути в заметку: раз человек
    вычеркнул их из протокола, адресату они уходить не должны.
    """
    from . import minutes

    meta = store.get(rec_id) or {}
    if key == "transcript":
        text = minutes.build_transcript_text(rec_id)
        return "Стенограмма", text
    for doc in minutes.stored_documents(rec_id):
        if doc["key"] == key:
            text = minutes.strip_dropped(doc["markdown"], store.rec_dropped(meta, key))
            return doc["title"], text
    return "", ""


def prepare_files(rec_id: str, keys: list[str]) -> list[dict[str, str]]:
    """Сложить отмеченное файлами во временную папку. Возвращает [{title, path}].

    Каждый отмеченный кусок — отдельным файлом: так адресат видит по имени, что
    ему прислали, а не разбирает одну простыню. Формат — Markdown, тот самый, в
    котором документ хранится: своей правды у отправки нет.
    """
    meta = store.get(rec_id)
    if meta is None:
        raise ValueError("Запись не найдена")
    if not keys:
        raise ValueError("Не отмечено, что отправлять")
    folder = Path(tempfile.mkdtemp(prefix="hagen-share-"))
    title = _safe_file_name(meta.get("title") or "Запись")
    date = str(meta.get("created_at") or "")[:10]
    out: list[dict[str, str]] = []
    for key in keys:
        heading, text = _document_text(rec_id, str(key))
        if not text.strip():
            continue
        name = _safe_file_name("%s %s — %s" % (date, title, heading))
        path = folder / (name + ".md")
        path.write_text(text.strip() + "\n", encoding="utf-8")
        out.append({"title": heading, "path": str(path)})
    if not out:
        shutil.rmtree(folder, ignore_errors=True)
        raise ValueError("Отмеченное оказалось пустым — отправлять нечего")
    return out


def _mail_body(meta: dict[str, Any], files: list[dict[str, str]],
               attached: bool) -> str:
    """Текст письма: короткое сопроводительное, документы — вложениями."""
    lines = ["Во вложении — %s по записи «%s»."
             % (", ".join(f["title"].lower() for f in files),
                meta.get("title") or "без названия")]
    when = str(meta.get("created_at") or "")[:10]
    if when:
        lines.append("Дата записи: %s." % when)
    if not attached:
        # Файлы приложить нечем: честно говорим, где они лежат.
        lines.append("")
        lines.append("Приложите файлы сами, они лежат здесь:")
        lines.extend(f["path"] for f in files)
    lines.append("")
    lines.append("Подготовлено в Hagen.")
    return "\n".join(lines)


def _mailto(subject: str, body: str) -> str:
    """Ссылка `mailto:` с темой и телом, обрезанным до разумной длины."""
    short = body if len(body) <= MAILTO_LIMIT else body[:MAILTO_LIMIT].rstrip() + "\n…"
    return "mailto:?subject=%s&body=%s" % (quote(subject), quote(short))


def telegram_link(text: str) -> tuple[str, bool]:
    """Ссылка «поделиться в Telegram» и признак «текст обрезан».

    Открывает в клиенте выбор чата с готовым сообщением. Просто `tg://msg`
    этого не делает — он только показывает окно программы, поэтому текст
    обязателен. Длинный документ в ссылку не влезает: отправляем начало, а
    целиком он остаётся в буфере обмена.
    """
    body = str(text or "").strip()
    if not body:
        return "tg://msg", False
    cut = len(body) > TG_LIMIT
    if cut:
        body = body[:TG_LIMIT].rstrip() + "\n…"
    return "tg://msg_url?url=%s" % quote(body, safe=""), cut


def send(rec_id: str, target: str, keys: list[str],
         text: str = "") -> dict[str, Any]:
    """Открыть письмо с отмеченным или окно Telegram. Ничего не отправляет само.

    Возвращает, что получилось: каким путём пошли, какие файлы собраны и что
    сказать человеку. Текст для Telegram здесь не собирается — его готовит
    страница тем же рисовальщиком, что и «Копировать в разметке Telegram», и
    присылает сюда готовым.
    """
    target = str(target or "").strip().lower()
    if target not in TARGETS:
        raise ValueError("Неизвестно, куда отправлять: %s" % target)
    meta = store.get(rec_id)
    if meta is None:
        raise ValueError("Запись не найдена")

    if target == "telegram":
        # Вложение через ссылку Telegram не передать, а вот выбор чата с
        # готовым сообщением — можно: этого и ждут от «Отправить».
        link, cut = telegram_link(text)
        platform.system().open_link(link)
        log.info("Telegram открыт для записи %s, знаков в ссылке %d%s",
                 rec_id, len(text or ""), " (обрезано)" if cut else "")
        if not str(text or "").strip():
            note = ("Telegram открыт. Текст документа — в буфере обмена, "
                    "вставьте его в нужный чат.")
        elif cut:
            note = ("Telegram открыт: выберите чат. В сообщение влезло только "
                    "начало документа — целиком он в буфере обмена, вставьте "
                    "его вместо обрезанного.")
        else:
            note = "Telegram открыт: выберите чат — документ уже в сообщении."
        return {"target": "telegram", "opened": True, "files": [],
                "cut": cut, "note": note}

    files = prepare_files(rec_id, keys)
    subject = "%s — %s" % (meta.get("title") or "Запись",
                           ", ".join(f["title"] for f in files))
    paths = [f["path"] for f in files]
    # Получателей не подставляем (решение 20.09): адресатов человек
    # выбирает сам, и случайно разослать протокол участникам встречи нельзя.
    attached = bool(platform.desktop().compose_mail(
        subject, _mail_body(meta, files, attached=True), paths, []))
    if attached:
        log.info("письмо с вложениями открыто: %s", ", ".join(f["title"] for f in files))
        return {"target": "mail", "opened": True, "attached": True,
                "files": files,
                "note": "Письмо открыто, документы во вложении. Проверьте и отправьте сами."}

    # Классического Outlook нет: открываем письмо ссылкой и оставляем файлы на
    # диске — приложить их придётся руками.
    platform.system().open_link(_mailto(subject, _mail_body(meta, files, attached=False)))
    platform.system().open_path(str(Path(paths[0]).parent))
    log.info("письмо открыто ссылкой mailto, файлы лежат в %s", Path(paths[0]).parent)
    return {"target": "mail", "opened": True, "attached": False, "files": files,
            "note": "Вложение доезжает только через классический Outlook. Письмо "
                    "открыто, а папка с файлами — рядом: приложите их сами."}
