# -*- coding: utf-8 -*-
"""Экспорт стенограмм и протоколов в хранилище Obsidian.

Структура, в которую вписываемся (её задал пользователь, ломать нельзя):

    <vault_path>/                     корень хранилища
        _Resources/Templates/Meetings Template.md
        <vault_subfolder>/            корень записей, по умолчанию "Meetings"
            Meetings.md               индексная заметка (создаём здесь)
            Встречи/ Звонки/ Заметки/ подпапки-категории

Одна запись = одна заметка "<ГГГГ-ММ-ДД> <Заголовок>.md" в папке категории.
Frontmatter рассчитан на dataview, ссылки — вики-формат.

Любое чтение и запись — io.open(..., encoding="utf-8"), перевод строки всегда "\\n",
чтобы Obsidian на Windows не получил смесь CRLF/LF.
"""
from __future__ import annotations

import io
import logging
import os
import re
import shutil
import time
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from . import config, store

log = logging.getLogger("hagen.obsidian")

#: Имя индексной заметки в корне записей.
INDEX_NAME = "Meetings"

#: Значение frontmatter `type` у заметок-стенограмм (по нему фильтрует dataview).
NOTE_TYPE = "meeting-transcript"

#: Значение frontmatter `type` у индексной заметки.
INDEX_TYPE = "meetings-index"

#: Базовые теги заметки.
NOTE_TAGS = ["hagen", "стенограмма"]

#: Заголовки разделов заметки.
H_MINUTES = "## Протокол"
H_SUMMARY = "## Саммари"
H_CONSPECT = "## Конспект"
H_DIGEST = "## Выжимка"
#: С 20.09 раздел называется «Свои запросы»: в него попадают и ответы на
#: вопросы, и заказанные документы. Настоящий заголовок берётся из реестра
#: видов (`minutes.DOC_KINDS`), здесь он повторён для разделов заметки.
H_QA = "## Свои запросы"
H_TRANSCRIPT = "## Стенограмма"
#: «Связать с» (17.09): подпись строки со ссылками в блоке «О записи».
H_LINKED = "Связано"
#: «Продолжение предыдущей записи» (17.09): что осталось с прошлого раза.
H_CARRY = "## С прошлого раза осталось"
#: Голосовые заметки автора (20.09): сказаны им отдельно, а не на встрече.
H_NOTES = "## Мои заметки"

#: Разделы документов в заметке — у каждого вида свой (см. minutes.DOC_KINDS).
DOC_HEADINGS = (H_MINUTES, H_SUMMARY, H_CONSPECT, H_DIGEST, H_QA)

#: Максимальная длина имени файла без расширения.
MAX_STEM = 120

#: Символы, недопустимые в именах файлов и папок Windows: \ / : * ? " < > |
_BAD_RE = re.compile(r'[\\/:*?"<>|\x00-\x1f]+')

#: Ведущие markdown-спецсимволы, которые экранируем в тексте реплик.
_LEAD_MD_RE = re.compile(r"^(\s*)([#>\-*+])")


# --------------------------------------------------------------------------- #
# вспомогательные утилиты
# --------------------------------------------------------------------------- #
def _safe_name(name: str, limit: int = MAX_STEM) -> str:
    """Имя, пригодное для файловой системы Windows и для ссылок Obsidian."""
    text = unicodedata.normalize("NFC", str(name or "")).strip()
    text = _BAD_RE.sub("-", text)
    # Obsidian плохо переносит квадратные скобки и решётки в именах файлов
    text = text.replace("[", "(").replace("]", ")").replace("#", "№").replace("^", "-")
    text = re.sub(r"\s+", " ", text).strip(" .-")
    if len(text) > limit:
        text = text[:limit].rstrip(" .-")
    return text or "Без названия"


#: Параметры ссылки, в которых прячется пропуск к файлу. В заметку и в логи
#: они попадать не должны: преавторизованная ссылка SharePoint несёт `tempauth`
#: прямо в запросе, и такая ссылка — это, по сути, ключ от записи.
_SECRET_QUERY = {
    "tempauth", "token", "access_token", "id_token", "refresh_token", "sig",
    "signature", "key", "apikey", "api_key", "password", "pwd", "auth",
    "se", "sp", "sv", "skoid", "sktid", "skt", "ske", "sks", "skv", "srt", "ss",
}


def _clean_url(value: Any) -> str:
    """Ссылка, пригодная для показа: секретные параметры вырезаны.

    Обычные параметры сохраняются: без `?v=...` ссылка на YouTube перестаёт
    открываться, поэтому рубить запрос целиком нельзя.
    """
    raw = str(value or "").strip()
    if not raw or "\n" in raw or "\r" in raw:
        return ""
    try:
        parts = urlsplit(raw)
    except ValueError:
        return ""
    if parts.scheme not in ("http", "https"):
        return ""
    if parts.query:
        kept = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
                if k.lower() not in _SECRET_QUERY]
        parts = parts._replace(query=urlencode(kept))
    return urlunsplit(parts._replace(fragment=""))


def _hms(seconds: Any) -> str:
    """Секунды -> "ЧЧ:ММ:СС" (отрицательные и мусор -> 00:00:00)."""
    try:
        total = int(round(float(seconds)))
    except (TypeError, ValueError):
        total = 0
    if total < 0:
        total = 0
    return f"{total // 3600:02d}:{(total % 3600) // 60:02d}:{total % 60:02d}"


def _parse_dt(value: Any) -> datetime | None:
    """ISO-строка или timestamp -> datetime. Ошибки проглатываем."""
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value))
        except (OverflowError, OSError, ValueError):
            return None
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d", "%Y%m%d-%H%M%S"):
        try:
            return datetime.strptime(text[: len(fmt) + 4], fmt)
        except ValueError:
            continue
    return None


def _meta_dt(meta: dict[str, Any]) -> datetime:
    """Дата и время записи. Если их нет — пробуем вытащить из id, иначе «сейчас»."""
    dt = _parse_dt(meta.get("created_at"))
    if dt is None:
        rec_id = str(meta.get("id") or "")
        if len(rec_id) >= 15:
            dt = _parse_dt(rec_id[:15])
    return dt or datetime.now()


def _yaml_scalar(value: Any) -> str:
    """Скаляр YAML: числа и bool как есть, строки — в кавычках, когда это нужно."""
    if value is None:
        return '""'
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value).replace("\r", " ").replace("\n", " ")
    plain = (
        text
        and text == text.strip()
        and not re.search(r'[:#\[\]{}&*!|>%@`"\',]', text)
        and text.lower() not in {"true", "false", "yes", "no", "null", "~", "on", "off"}
        and not re.fullmatch(r"[-+]?\d+(\.\d+)?", text)
    )
    if plain:
        return text
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _yaml_flow_list(values: Iterable[Any]) -> str:
    items = [_yaml_scalar(v) for v in values]
    return "[" + ", ".join(items) + "]"


def _vault_base() -> Path:
    """Корень всего хранилища Obsidian (для вычисления относительных путей)."""
    return Path(config.get("vault_path") or config.DEFAULT_VAULT)


def _require_vault_base() -> Path:
    """Убедиться, что корень хранилища Obsidian на месте, и сказать это по-русски.

    Без проверки mkdir(parents=True) молча создал бы «хранилище» по неверному
    пути (например, если Яндекс.Диск не подключён), и заметки ушли бы в пустоту.
    """
    base = _vault_base()
    if not base.is_dir():
        raise ValueError(
            "Хранилище Obsidian не найдено: папки «%s» нет. Проверьте, что "
            "Яндекс.Диск подключён, и путь в настройках («Папка хранилища») указан верно."
            % base
        )
    return base


def _rel_to_vault(path: Path) -> str:
    """Путь относительно корня хранилища, через "/" (как в ссылках Obsidian)."""
    base = _vault_base()
    try:
        rel = Path(path).resolve().relative_to(base.resolve())
    except (ValueError, OSError):
        return Path(path).name
    return rel.as_posix()


def _write_text(path: Path, text: str) -> int:
    """Атомарная запись UTF-8 с "\\n". Возвращает число записанных байт."""
    path.parent.mkdir(parents=True, exist_ok=True)
    data = text if text.endswith("\n") else text + "\n"
    tmp = path.with_name(path.name + ".tmp")
    with io.open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())     # содержимое на диск раньше подмены имени
    # Яндекс.Диск и антивирус иногда держат файл занятым — пробуем ещё раз
    last: OSError | None = None
    for attempt in range(4):
        try:
            os.replace(tmp, path)
            return len(data.encode("utf-8"))
        except OSError as exc:
            last = exc
            time.sleep(0.25 * (attempt + 1))
    try:
        tmp.unlink()
    except OSError:
        pass
    raise OSError(
        "Не удалось записать заметку «%s»: файл занят (синхронизация Яндекс.Диска "
        "или открытый редактор). Подробности: %s" % (path.name, last)
    )


def _read_text(path: Path) -> str:
    with io.open(path, "r", encoding="utf-8-sig", errors="replace") as fh:
        return fh.read().replace("\r\n", "\n").replace("\r", "\n")


def _escape_text(text: str) -> str:
    """Минимальное экранирование markdown: только ведущие #, >, -, *, + в строках."""
    out: list[str] = []
    for line in str(text or "").split("\n"):
        out.append(_LEAD_MD_RE.sub(r"\1\\\2", line))
    return "\n".join(out)


# --------------------------------------------------------------------------- #
# хранилище: папки и категории
# --------------------------------------------------------------------------- #
def _hidden(name: str) -> bool:
    return name.startswith(".") or name.startswith("_")


def list_categories() -> list[str]:
    """Категории: существующие подпапки корня записей + категории из настроек."""
    out: list[str] = []
    root = config.vault_root()
    hidden = {str(h).strip() for h in (config.get("hidden_categories") or [])}
    try:
        for d in sorted(root.iterdir(), key=lambda p: p.name.lower()):
            if d.is_dir() and not _hidden(d.name) and d.name not in out and d.name not in hidden:
                out.append(d.name)
    except OSError:
        pass
    for name in config.get("categories") or []:
        name = str(name).strip()
        if name and name not in out and name not in hidden:
            out.append(name)
    return out


def remove_category(name: str) -> list[str]:
    """Убрать категорию из СПИСКА программы. Папка в сейфе и заметки в ней остаются.

    Папки категорий программа сама видит в сейфе, поэтому одного удаления из
    настроек мало — имя запоминается в hidden_categories. Записи, у которых
    уже стоит эта категория, её сохраняют: их заметки лежат там же, где лежали.
    """
    name = str(name or "").strip()
    if not name:
        raise ValueError("Не указана категория")
    cats = [str(c) for c in (config.get("categories") or []) if str(c) != name]
    hidden = [str(h) for h in (config.get("hidden_categories") or [])]
    if name not in hidden:
        hidden.append(name)
    patch: dict[str, Any] = {"categories": cats, "hidden_categories": hidden}
    left = [c for c in list_categories() if c != name]
    if str(config.get("default_category") or "") == name and left:
        patch["default_category"] = left[0]
    config.save(patch)
    log.info("Категория %s убрана из списка (папка осталась)", name)
    return list_categories()


def add_category(name: str) -> list[str]:
    """Создать папку-категорию и запомнить её в настройках. Возвращает список категорий."""
    safe = _safe_name(name, limit=60)
    if not safe or safe == "Без названия":
        raise ValueError("Пустое или недопустимое имя категории")
    _require_vault_base()
    root = config.vault_root()
    (root / safe).mkdir(parents=True, exist_ok=True)
    cats = [str(c) for c in (config.get("categories") or [])]
    hidden = [str(h) for h in (config.get("hidden_categories") or [])]
    if safe not in cats or safe in hidden:
        if safe not in cats:
            cats.append(safe)
        # Категорию вернули в список — папка та же, заметки в ней на месте.
        config.save({"categories": cats, "hidden_categories": [h for h in hidden if h != safe]})
        log.info("Добавлена категория %s", safe)
    return list_categories()


def _index_path() -> Path:
    return config.vault_root() / f"{INDEX_NAME}.md"


def _dataview_source() -> str:
    """Фрагмент `FROM "..."` для dataview или пустая строка, если записи в корне."""
    rel = _rel_to_vault(config.vault_root())
    rel = "" if rel in {".", ""} else rel
    return f'FROM "{rel}"\n' if rel else ""


def _render_index() -> str:
    """Текст индексной заметки Meetings.md."""
    src = _dataview_source()
    today = datetime.now().strftime("%Y-%m-%d")
    lines = [
        "---",
        f"created: {today}",
        f"type: {INDEX_TYPE}",
        f"tags: {_yaml_flow_list(['hagen'])}",
        "---",
        "",
        f"# {INDEX_NAME}",
        "",
        "Индекс записей совещаний и звонков. Заметки создаёт приложение «Hagen»:",
        "каждая запись — отдельный файл со стенограммой, а при необходимости и с протоколом.",
        "Каждая такая заметка ссылается сюда полем `in`, поэтому список ниже пополняется сам.",
        "",
        "Заметка — выгрузка из приложения: документы, под ними стенограмма. Правьте в",
        "приложении — при каждом обновлении оно переписывает заметку целиком, и ручные",
        "изменения здесь не сохраняются.",
        "",
        "## Записи по категориям",
        "",
        "```dataview",
        'TABLE WITHOUT ID rows.file.link AS "Запись", rows.created AS "Дата"',
        src + f'WHERE type = "{NOTE_TYPE}"',
        "SORT created DESC",
        'GROUP BY category AS "Категория"',
        "```",
        "",
        "## Все записи",
        "",
        "```dataview",
        'TABLE WITHOUT ID file.link AS "Запись", created AS "Дата", category AS "Категория",',
        '  duration AS "Длительность", join(participants, ", ") AS "Участники"',
        src + f'WHERE type = "{NOTE_TYPE}"',
        "SORT created DESC",
        "```",
        "",
        "## Без протокола",
        "",
        "```dataview",
        'LIST WITHOUT ID file.link',
        src + f'WHERE type = "{NOTE_TYPE}" AND has_minutes != true',
        "SORT created DESC",
        "```",
    ]
    return "\n".join(lines) + "\n"


def ensure_vault() -> dict[str, Any]:
    """Создать корень записей, папки категорий и индексную заметку (если их нет)."""
    _require_vault_base()
    root = config.vault_root()
    created: list[str] = []
    if not root.exists():
        root.mkdir(parents=True, exist_ok=True)
        created.append(_rel_to_vault(root) + "/")
    for name in config.get("categories") or []:
        safe = _safe_name(str(name), limit=60)
        folder = root / safe
        if not folder.exists():
            folder.mkdir(parents=True, exist_ok=True)
            created.append(_rel_to_vault(folder) + "/")
    index = _index_path()
    if not index.exists():
        _write_text(index, _render_index())
        created.append(_rel_to_vault(index))
    if created:
        log.info("Хранилище подготовлено, создано: %s", ", ".join(created))
    return {"root": str(root), "created": created, "index": str(index)}


# --------------------------------------------------------------------------- #
# путь заметки
# --------------------------------------------------------------------------- #
def _category_dir(meta: dict[str, Any]) -> Path:
    cat = str(meta.get("category") or config.get("default_category") or "").strip()
    safe = _safe_name(cat, limit=60) if cat else ""
    root = config.vault_root()
    return root / safe if safe and safe != "Без названия" else root


def project_dir(meta: dict[str, Any]) -> Path | None:
    """Папка проекта записи в сейфе или None, если связки нет.

    Путь хранится относительно корня сейфа, поэтому переезд самого сейфа
    (другой диск, другая машина) связку не рвёт. Папку, которая после сборки
    пути оказалась вне сейфа, не признаём: заметки живут в сейфе.
    """
    rel = store.project_folder((meta or {}).get("project"))
    if not rel:
        return None
    base = _vault_base()
    folder = base / Path(rel)
    try:
        if not folder.resolve().is_relative_to(base.resolve()):
            log.warning("Папка проекта %s вне сейфа — связка не применяется", rel)
            return None
    except OSError:
        return None
    return folder


def _note_dir(meta: dict[str, Any]) -> Path:
    """Где лежит заметка записи: папка проекта, если она привязана, иначе категория.

    Решение 20.09: у проекта главное слово. Папку проекта выбирает человек в
    сейфе целиком — она может быть где угодно, не только внутри корня записей.
    """
    return project_dir(meta) or _category_dir(meta)


def _note_recording_id(path: Path) -> str:
    """recording_id из frontmatter готовой заметки («чей это файл»)."""
    try:
        head = _read_text(path)[:4096]
    except OSError:
        return ""
    m = re.search(r"^recording_id:\s*[\"']?([^\"'\s]+)", head, flags=re.MULTILINE)
    return m.group(1) if m else ""


def _unique_path(folder: Path, stem: str, rec_id: str = "") -> Path:
    """Свободный путь "<stem>.md" в папке; при конфликте " (2)", " (3)"...

    Файл, у которого в frontmatter тот же recording_id, считаем своим и занимаем.
    """
    candidate = folder / f"{stem}.md"
    if not candidate.exists():
        return candidate
    if rec_id and _note_recording_id(candidate) == rec_id:
        return candidate
    for n in range(2, 1000):
        suffix = f" ({n})"
        tail = _safe_name(stem, limit=MAX_STEM - len(suffix)) + suffix
        candidate = folder / f"{tail}.md"
        if not candidate.exists():
            return candidate
        if rec_id and _note_recording_id(candidate) == rec_id:
            return candidate
    raise OSError("Не удалось подобрать свободное имя файла заметки")


def note_path(meta: dict[str, Any]) -> Path:
    """Путь заметки: <папка записи>/<ГГГГ-ММ-ДД> <Заголовок>.md.

    Папка записи — папка привязанного проекта, а если её нет — папка категории.
    """
    meta = meta or {}
    date = _meta_dt(meta).strftime("%Y-%m-%d")
    title = str(meta.get("title") or "").strip() or f"Запись {date}"
    stem = _safe_name(f"{date} {title}", limit=MAX_STEM)
    return _unique_path(_note_dir(meta), stem, str(meta.get("id") or ""))


# --------------------------------------------------------------------------- #
# рендер заметки
# --------------------------------------------------------------------------- #
def _participants(meta: dict[str, Any], segments: list[dict[str, Any]]) -> list[str]:
    names: list[str] = []
    for nm in meta.get("participants") or []:
        nm = store.display_speaker(nm)
        if nm and nm not in names:
            names.append(nm)
    if not names:
        for seg in segments:
            nm = store.display_speaker(seg.get("speaker"))
            if nm and nm not in names:
                names.append(nm)
    return names


def _duration_s(meta: dict[str, Any], segments: list[dict[str, Any]]) -> float:
    try:
        dur = float(meta.get("duration_s") or 0.0)
    except (TypeError, ValueError):
        dur = 0.0
    if dur <= 0.0 and segments:
        try:
            dur = max(float(s.get("end") or 0.0) for s in segments)
        except (TypeError, ValueError):
            dur = 0.0
    return max(0.0, dur)


def _speakers_marked(meta: dict[str, Any], segments: list[dict[str, Any]]) -> bool:
    """Реплики разведены по говорящим.

    Достаточно того, что диаризация прошла: имена могут быть ещё безличными
    («Спикер 2»), но разделение уже есть. Подтверждённые пользователем имена
    отмечаются отдельным полем speakers_named.
    """
    if not segments:
        return False
    if bool(meta.get("diarized")):
        return True
    return all(bool(s.get("speaker_locked")) for s in segments)


def _speakers_named(meta: dict[str, Any], segments: list[dict[str, Any]]) -> bool:
    """Все говорящие названы по именам, а не «Спикер N»."""
    if not segments:
        return False
    return all(bool(s.get("speaker_locked")) for s in segments)


def _asr_label(meta: dict[str, Any]) -> str:
    """Чем получен текст: распознаванием (и каким) или готовыми субтитрами."""
    kind = str(meta.get("transcript_source") or "").strip()
    if kind == "subs":
        name = str(meta.get("subs_name") or "").strip()
        tail = f" ({name})" if name else ""
        return f"готовые субтитры источника{tail} — распознавание не запускалось"
    name = str(meta.get("asr_model") or meta.get("model") or config.get("offline_model") or "v3_e2e_rnnt")
    if kind == "cloud":
        return f"{name} (в облаке)"
    if kind == "local_en":
        return f"{name} (на этом компьютере, английский)"
    return name if "/" in name else f"GigaAM {name}"


def _about_block(meta: dict[str, Any]) -> list[str]:
    """«# О записи» — только то, чего нет в свойствах заметки (формат 15.09).

    Дата, длительность, категория, источник и участники уже в YAML — здесь их
    нет. Остаются: чем получен текст, чем разделены говорящие, встреча в
    календаре.
    """
    items = [f"- **Текст получен:** {_asr_label(meta)}"]
    if meta.get("diarized"):
        model = meta.get("diarize_model") or config.get("diarize_model")
        items.append(f"- **Разделение говорящих:** {model}")
    meeting = meta.get("meeting")
    if isinstance(meeting, dict):
        subject = str(meeting.get("subject") or meeting.get("title") or "").strip()
        if subject:
            items.append(f"- **Встреча в календаре:** {subject}")
    # «Связать с» (17.09) — строкой здесь, а не отдельным разделом: главных
    # разделов в заметке ровно столько, сколько документов, плюс стенограмма
    # (формат 15.09), и лишний заголовок первого уровня сломал бы это.
    links = [_wikilink(link) for link in store.rec_links(meta)]
    links = [text for text in links if text]
    if links:
        items.append("- **%s:** %s" % (H_LINKED, " · ".join(links)))
    return ["# О записи", ""] + items + [""]


def _suggestion_info(raw: Any) -> tuple[str, float | None]:
    """Из подсказки о говорящем достаём имя и долю совпадения 0..1."""
    if not raw:
        return "", None
    if isinstance(raw, str):
        return raw.strip(), None
    if not isinstance(raw, dict):
        return str(raw), None
    name = ""
    for key in ("name", "speaker", "label", "title"):
        val = raw.get(key)
        if val:
            name = str(val).strip()
            break
    score: float | None = None
    for key in ("score", "similarity", "confidence", "match", "value", "probability"):
        if raw.get(key) is not None:
            try:
                score = float(raw[key])
            except (TypeError, ValueError):
                score = None
            break
    if score is not None and score > 1.0:
        score = score / 100.0
    return name, score


def _frontmatter(meta: dict[str, Any], segments: list[dict[str, Any]], has_minutes: bool) -> list[str]:
    dt = _meta_dt(meta)
    names = _participants(meta, segments)
    cat = str(meta.get("category") or config.get("default_category") or "")
    source = str(meta.get("source") or "live")
    if source not in ("live", "file", "link"):
        source = "live"
    out = [
        "---",
        f'in: [{_yaml_scalar("[[" + INDEX_NAME + "]]")}]',
        f"created: {dt.strftime('%Y-%m-%d')}",
        f"type: {NOTE_TYPE}",
        f"category: {_yaml_scalar(cat)}",
        f"duration: {_yaml_scalar(_hms(_duration_s(meta, segments)))}",
        f"participants: {_yaml_flow_list(names)}",
        f"speakers_marked: {'true' if _speakers_marked(meta, segments) else 'false'}",
        f"speakers_named: {'true' if _speakers_named(meta, segments) else 'false'}",
        f"source: {_yaml_scalar(source)}",
    ]
    # Проект записи (17.09): признак для Obsidian, по нему он фильтрует и
    # собирает подборки. Пустой проект в шапку не пишем — пустое поле мешает
    # dataview сильнее, чем отсутствующее.
    project = store.rec_project(meta)
    if project:
        out.append(f"project: {_yaml_scalar(project)}")
    url = _clean_url(meta.get("url"))
    if url:
        out.append(f"url: {_yaml_scalar(url)}")
    # Свои теги записи идут ПОСЛЕ служебных: «hagen» и «стенограмма» остаются
    # на месте, иначе развалятся готовые подборки в сейфе.
    tags = list(NOTE_TAGS)
    for tag in store.rec_tags(meta):
        if tag.lower() not in {t.lower() for t in tags}:
            tags.append(tag)
    out += [
        f"recording_id: {_yaml_scalar(meta.get('id') or '')}",
        f"has_minutes: {'true' if has_minutes else 'false'}",
        f"tags: {_yaml_flow_list(tags)}",
        "---",
    ]
    return out


def _audio_paths(meta: dict[str, Any]) -> list[str]:
    """Пути к исходному аудио, чтобы запись можно было перераспознать."""
    rec_id = str(meta.get("id") or "")
    if not rec_id:
        return []
    try:
        tracks = store.existing_tracks(rec_id)
    except OSError:
        tracks = []
    if not tracks:
        tracks = [t for t in (meta.get("tracks") or []) if t]
    out: list[str] = []
    for track in tracks:
        try:
            path = store.track_path(rec_id, str(track))
        except Exception:  # защита от неожиданного имени дорожки
            continue
        text = str(path)
        if text not in out:
            out.append(text)
    video = str(meta.get("video_path") or "").strip()
    if video and video not in out:
        out.append(video)
    return out


def _shot_lines(shot: dict[str, Any]) -> list[str]:
    """Снимок экрана внутри стенограммы: время и сама картинка.

    Ссылка — вики-ссылкой по имени файла: Obsidian находит картинку, где бы она
    ни лежала в сейфе, поэтому перенос заметки в другую категорию её не рвёт.
    Имя файла уникально (дата, время, хвост номера записи).
    """
    name = str(shot.get("file") or "").strip()
    if not name:
        return []
    return [f"> 🖼 **[{_hms(shot.get('at_s'))}] Снимок экрана**", f"> ![[{name}|700]]"]


def _transcript_block(segments: list[dict[str, Any]],
                      shots: list[dict[str, Any]] | None = None) -> tuple[list[str], list[str]]:
    """Реплики и сноски-подсказки. Возвращает (строки раздела, строки сносок).

    Снимки экрана встают между репликами — перед первой репликой, начатой после
    снимка.
    """
    body: list[str] = []
    notes: list[str] = []
    note_ids: dict[tuple[str, str], str] = {}
    prev_key: Any = object()
    pending = sorted([s for s in (shots or []) if isinstance(s, dict) and s.get("file")],
                     key=lambda s: float(s.get("at_s") or 0.0))

    def flush(until: float | None) -> None:
        nonlocal prev_key
        while pending and (until is None or float(pending[0].get("at_s") or 0.0) <= until):
            lines = _shot_lines(pending.pop(0))
            if not lines:
                continue
            if body:
                body.append("")
            body.extend(lines)
            prev_key = object()      # следующая реплика — новым абзацем с именем

    for seg in segments:
        flush(float(seg.get("start") or 0.0))
        name = store.display_speaker(seg.get("speaker")) or store.SPEAKER_FAR
        key = seg.get("speaker_key") or name
        mark = ""      # пометка «имя не подтверждено», идёт сразу за именем
        ref_note = ""  # ссылка на сноску с процентом совпадения
        if not seg.get("speaker_locked"):
            sug_name, score = _suggestion_info(seg.get("suggestion"))
            if sug_name or score is not None:
                pct = f"{round((score or 0.0) * 100)} %" if score is not None else "неизвестно"
                cache_key = (sug_name, pct)
                ref = note_ids.get(cache_key)
                if ref is None:
                    ref = f"guess{len(note_ids) + 1}"
                    note_ids[cache_key] = ref
                    who = sug_name or "имя не определено"
                    notes.append(
                        f"[^{ref}]: Имя предложено автоматически: **{who}**, "
                        f"совпадение голоса {pct}. Подтвердите или исправьте в «Hagen»."
                    )
                mark = " (?)"
                ref_note = f"[^{ref}]"
        if body and key != prev_key:
            body.append("")
        elif body:
            # подряд идущие реплики одного человека остаются одним абзацем,
            # поэтому разделяем их жёстким переводом строки (два пробела)
            body[-1] = body[-1] + "  "
        prev_key = key
        # реплика — ровно одна строка, внутренние переводы строк склеиваем
        text = _escape_text(re.sub(r"\s+", " ", str(seg.get("text") or "")).strip())
        body.append(f"**[{_hms(seg.get('start'))}] {name}{mark}:**{ref_note} {text}".rstrip())
    if not segments:
        body = ["_Реплик нет: запись пустая или ещё не распознана._"]
    flush(None)
    return body, notes


#: Строка-заголовок markdown: решётки и текст.
_MD_HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(.*?)[ \t]*$")
#: Начало и конец блока кода: заголовки внутри кода не трогаем.
_FENCE_RE = re.compile(r"^[ \t]*(```|~~~)")


def _main_heading(heading: str) -> str:
    """Главный раздел заметки — заголовок первого уровня: «## Протокол» → «# Протокол»."""
    return "# " + str(heading or "").lstrip("#").strip()


def _nest_document(md: str) -> str:
    """Документ внутрь своего главного раздела (формат 15.09).

    Собственное название документа — заголовок первого уровня в самом начале
    («# Протокол совещания») — убирается: его место занимает главный раздел.
    Остальные заголовки сдвигаются так, чтобы самый крупный стал вторым
    уровнем: «## Участники», «## Задачи» — подразделы, а не соседи главных
    разделов. Порядок уровней внутри документа сохраняется.
    """
    lines = str(md or "").strip("\n").split("\n")
    fence = False
    heads: list[tuple[int, int]] = []
    for i, line in enumerate(lines):
        if _FENCE_RE.match(line):
            fence = not fence
            continue
        m = None if fence else _MD_HEADING_RE.match(line)
        if m:
            heads.append((i, len(m.group(1))))
    first = next((i for i, line in enumerate(lines) if line.strip()), None)
    drop = heads[0][0] if heads and heads[0][0] == first and heads[0][1] == 1 else None
    rest = [level for i, level in heads if i != drop]
    shift = 2 - min(rest) if rest else 0
    out: list[str] = []
    fence = False
    for i, line in enumerate(lines):
        if i == drop:
            continue
        if _FENCE_RE.match(line):
            fence = not fence
            out.append(line)
            continue
        m = None if fence else _MD_HEADING_RE.match(line)
        if m and shift:
            level = min(6, max(2, len(m.group(1)) + shift))
            line = "#" * level + " " + m.group(2)
        out.append(line)
    if drop is not None:
        # Строка «Дата: …, начало в …, длительность …» под названием документа —
        # то же, что в свойствах заметки (формат 15.09).
        first_left = next((i for i, line in enumerate(out) if line.strip()), None)
        if first_left is not None and _DATE_LINE_RE.match(out[first_left]):
            del out[first_left]
    return "\n".join(_drop_sections(out, _YAML_SECTIONS)).strip("\n")


#: Первая строка документа с датой и длительностью — дублирует свойства заметки.
_DATE_LINE_RE = re.compile(r"^[ \t]*[*_]*Дата[*_]*[ \t]*:", re.IGNORECASE)
#: Подразделы документа, которые повторяют свойства заметки: участники уже в YAML.
_YAML_SECTIONS = ("участники",)


def _drop_sections(lines: list[str], names: tuple[str, ...]) -> list[str]:
    """Убрать подразделы с такими названиями — вместе с текстом до следующего заголовка того же уровня."""
    out: list[str] = []
    skip_level = 0
    fence = False
    for line in lines:
        if _FENCE_RE.match(line):
            fence = not fence
        m = None if fence else _MD_HEADING_RE.match(line)
        if m:
            level = len(m.group(1))
            if skip_level and level <= skip_level:
                skip_level = 0
            if not skip_level and m.group(2).strip(" *_:").lower() in names:
                skip_level = level
                continue
        if not skip_level:
            out.append(line)
    return out


def _heading_for(meta: dict[str, Any]) -> str:
    """Заголовок раздела с документом: у видео «Саммари», у записи «Протокол».

    Смотрим на ЯВНЫЙ признак doc_kind, а не на источник записи. Значение
    source='link' появилось раньше режима «Видео» и стоит у обычных записей,
    полученных по ссылке: если решать по нему, у старых записей в заметке
    возник бы второй раздел, а прежний «## Протокол» остался бы сиротой.
    """
    return H_SUMMARY if str(meta.get("doc_kind") or "") == "summary" else H_MINUTES


def render_markdown(
    meta: dict[str, Any],
    segments: list[dict[str, Any]],
    minutes_md: str | None = None,
    with_transcript: bool = True,
    minutes_heading: str | None = None,
    documents: list[tuple[str, str]] | None = None,
) -> str:
    """Собрать полный markdown заметки: свойства, документы, стенограмма.

    Формат 15.09: главных разделов (заголовки первого уровня) ровно
    столько, сколько видов документов сделано, плюс «Стенограмма». Всё, что
    внутри документа («Участники», «Решения», «Задачи»), — подразделы своего
    главного раздела. Названия записи и «О записи» в тексте нет: имя заметки
    Obsidian показывает сам, дата, длительность и участники — в свойствах.

    documents — все готовые документы записи [(заголовок раздела, текст)]: у
    каждого вида свой раздел. Если не заданы, берётся один minutes_md.

    with_transcript=False собирает заметку БЕЗ раздела стенограммы. Нужно для
    кнопки «Сделать протокол»: она отправляет в хранилище только протокол, а
    стенограмма попадает туда отдельной кнопкой и только по желанию человека.

    minutes_heading задаёт заголовок раздела с документом: для совещания это
    «## Протокол», для видео — «## Саммари». Место в заметке одно и то же.
    """
    meta = dict(meta or {})
    segments = sorted(
        [s for s in (segments or []) if isinstance(s, dict)],
        key=lambda s: (float(s.get("start") or 0.0), str(s.get("track") or "")),
    )
    minutes = (minutes_md or "").strip()
    docs = [(h, (md or "").strip()) for h, md in (documents or []) if (md or "").strip()]
    if not docs and minutes:
        docs = [(minutes_heading or _heading_for(meta), minutes)]

    lines: list[str] = []
    lines += _frontmatter(meta, segments, bool(docs))
    lines.append("")
    lines += _about_block(meta)
    lines += _notes_block(meta)
    lines += _carry_block(meta)

    for heading, body in docs:
        lines += [_main_heading(heading), "", _nest_document(body), ""]

    if with_transcript:
        body, notes = _transcript_block(segments, meta.get("screenshots"))
        lines += [_main_heading(H_TRANSCRIPT), ""]
        lines += body
        lines.append("")
        if notes:
            lines += notes
            lines.append("")

    lines.append("---")
    lines.append("")
    lines.append("*Файл создан приложением «Hagen» автоматически; правки вносите в приложении.*")
    audio = _audio_paths(meta)
    if audio:
        lines.append("")
        lines.append("*Исходные файлы (для повторного распознавания):*")
        for path in audio:
            lines.append(f"- `{path}`")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
# запись заметки
# --------------------------------------------------------------------------- #
def _require_meta(rec_id: str) -> dict[str, Any]:
    meta = store.get(rec_id)
    if not meta:
        raise ValueError(f"Запись {rec_id} не найдена")
    return meta


def _resolve_target(meta: dict[str, Any]) -> tuple[Path, Path | None]:
    """Куда писать заметку. Возвращает (целевой путь, путь, который надо удалить).

    Если заметка уже была и место не изменилось — пишем в тот же файл. Если
    изменилось — переносим файл туда, где ему теперь место. Место задаёт
    категория, а с 20.09 ещё и папка привязанного проекта: сменили у записи
    проект — заметка уезжает в папку нового проекта тем же переносом.
    """
    old_raw = str(meta.get("vault_path") or "").strip()
    old = Path(old_raw) if old_raw else None
    target_dir = _note_dir(meta)
    target_dir.mkdir(parents=True, exist_ok=True)

    if old is not None and old.exists() and old.is_file():
        try:
            same_dir = old.parent.resolve() == target_dir.resolve()
        except OSError:
            same_dir = str(old.parent) == str(target_dir)
        if same_dir:
            return old, None
        moved = _unique_path(target_dir, _safe_name(old.stem), str(meta.get("id") or ""))
        try:
            if moved.exists():
                moved.unlink()
            shutil.move(str(old), str(moved))
            log.info("Заметка перенесена в %s", _rel_to_vault(target_dir))
            return moved, None
        except OSError as exc:
            log.warning("Не удалось перенести заметку (%s), пишу заново", exc)
            return _unique_path(target_dir, _safe_name(old.stem), str(meta.get("id") or "")), old

    return note_path(meta), None


def _stored_documents(rec_id: str) -> list[tuple[str, str]]:
    """Все готовые документы записи [(заголовок раздела, текст)] — из файлов записи.

    Вычеркнутые пункты (17.09) убираются ЗДЕСЬ, на пути в заметку: в самом файле
    документа они остаются, и вычёркивание всегда можно отменить. Если после
    вычёркивания от документа не осталось содержания, раздела в заметке не будет.
    """
    from . import minutes as _minutes

    meta = store.get(rec_id) or {}
    out: list[tuple[str, str]] = []
    for d in _minutes.stored_documents(rec_id):
        text = _minutes.strip_dropped(d["markdown"], store.rec_dropped(meta, d["key"]))
        # Пустым считаем документ, от которого остался один каркас: заголовки,
        # разделители и подпись «Сформировано». Раздел из голых заголовков в
        # заметке хуже, чем его отсутствие.
        if _minutes.droppable_lines(text):
            out.append((d["heading"], text))
    return out


def _note_has_transcript(meta: dict[str, Any], path: Path) -> bool:
    """Есть ли в заметке стенограмма.

    Заметка, созданная «Сделать документ» до нажатия «Сохранить стенограмму»,
    стенограммы не содержит — и пересохранение её туда не добавляет. Признак
    хранится в записи; у заметок, записанных раньше, смотрим в сам файл.
    """
    if "vault_transcript" in meta:
        return bool(meta.get("vault_transcript"))
    try:
        # «# Стенограмма» — с 15.09, «## Стенограмма» — у заметок до того
        name = re.escape(H_TRANSCRIPT.lstrip("#").strip())
        return re.search(r"^#{1,2}[ \t]+" + name + r"[ \t]*$", _read_text(path),
                         flags=re.MULTILINE) is not None
    except OSError:
        return True


def refresh_note(rec_id: str) -> dict[str, Any]:
    """Обновить заметку, если она уже есть в Obsidian (15.09).

    Зовётся после разметки голосов, подписи имён, «Перечитать точнее» и
    разделения голосов: раньше заметка оставалась такой, какой её записал
    «Стоп» — с «Участник» вместо имён. Заметка — выгрузка из программы, в
    Obsidian её не правят (решение 15.09), поэтому переписывается
    целиком. Заметки нет — {"skipped": "no_note"}: без нажатия в хранилище
    ничего не кладём.
    """
    meta = store.get(rec_id) or {}
    raw = str(meta.get("vault_path") or "").strip()
    path = Path(raw) if raw else None
    if path is None or not path.exists():
        return {"skipped": "no_note"}
    return save_note(rec_id, with_transcript=_note_has_transcript(meta, path))


def save_note(rec_id: str, minutes_md: str | None = None,
              with_transcript: bool = True,
              minutes_heading: str | None = None,
              documents: list[tuple[str, str]] | None = None) -> dict[str, Any]:
    """Отрендерить и записать заметку записи целиком. Возвращает {"path", "bytes", "relative"}.

    Заметка — выгрузка из программы: документы и стенограмма берутся только из
    данных записи, правки в Obsidian не переносятся (решение 15.09;
    отменяет прежнее решение, где правки документов из заметки сохранялись).
    """
    meta = _require_meta(rec_id)
    ensure_vault()
    segments = store.sorted_segments(rec_id)
    if documents is not None:
        minutes_md = "\n".join(md for _h, md in documents) or None
    elif minutes_md is None:
        # Все документы записи, каждый в своём разделе, над стенограммой.
        documents = _stored_documents(rec_id)
        minutes_md = "\n".join(md for _h, md in documents) or None
    text = render_markdown(meta, segments, None if documents is not None else minutes_md,
                           with_transcript=with_transcript,
                           minutes_heading=minutes_heading, documents=documents)

    target, stale = _resolve_target(meta)
    size = _write_text(target, text)
    if stale is not None and stale.exists():
        try:
            stale.unlink()
        except OSError:
            log.warning("Старая заметка осталась на диске: %s", stale)

    store.update(rec_id, {"vault_path": str(target), "vault_transcript": bool(with_transcript),
                          "has_minutes": bool((minutes_md or "").strip())})
    log.info("Заметка сохранена: %s (%d байт)", target, size)
    return {"path": str(target), "bytes": size, "relative": _rel_to_vault(target)}


def append_minutes(rec_id: str, minutes_md: str,
                   heading: str | None = None) -> dict[str, Any]:
    """Документ готов — переписать заметку: все документы записи, под ними стенограмма.

    Этот документ берётся с переданным текстом (он мог ещё не лечь в файл
    записи); пустой текст — «убрать раздел». Участники и «О записи» — свежие.

    Заметки ещё нет — создаётся ТОЛЬКО с документами: стенограмму в хранилище
    кладёт «Сохранить стенограмму» (раньше кнопка документа неожиданно выкладывала
    всю стенограмму). Заметка есть — стенограмма в ней остаётся и тоже
    переписывается, если она там была (решение 15.09).
    """
    meta = _require_meta(rec_id)
    minutes = (minutes_md or "").strip()
    head = heading or _heading_for(meta)
    docs = [(h, md) for h, md in _stored_documents(rec_id) if h != head]
    if minutes:
        # на месте прежнего раздела этого вида, иначе — последним
        order = [h for h, _md in _stored_documents(rec_id)]
        docs.insert(order.index(head) if head in order else len(docs), (head, minutes))
    raw = str(meta.get("vault_path") or "").strip()
    path = Path(raw) if raw else None
    with_transcript = bool(path is not None and path.exists() and _note_has_transcript(meta, path))
    res = save_note(rec_id, with_transcript=with_transcript, documents=docs)
    log.info("%s вписан в заметку: %s", head.lstrip("# "), res.get("path"))
    return res


def delete_note(rec_id: str) -> dict[str, Any]:
    """Убрать заметку записи из хранилища.

    Только тот файл, который сама программа и создала — путь берётся из
    `vault_path` в meta. Ничего не ищем по имени и не удаляем по догадке:
    в хранилище лежат заметки, написанные человеком, и промахнуться нельзя.
    """
    meta = store.get(rec_id) or {}
    raw = str(meta.get("vault_path") or "").strip()
    if not raw:
        return {"deleted": False, "why": "заметки не было"}
    path = Path(raw)

    # Границы проверяем обязательно: хранилище лежит на Яндекс.Диске, и промах
    # уехал бы в облако и на все машины разом. Удаляем только файл .md внутри
    # корня хранилища — и ничего другого, чем бы ни было записано в meta.
    try:
        inside = path.resolve().is_relative_to(config.vault_root().resolve())
    except (OSError, ValueError):
        inside = False
    if not inside or path.suffix.lower() != ".md":
        log.warning("отказ удалять заметку не из хранилища: %s", path)
        return {"deleted": False, "why": "путь вне хранилища"}

    if not path.exists():
        store.update(rec_id, {"vault_path": None})
        return {"deleted": False, "why": "заметка уже удалена"}
    try:
        path.unlink()
    except OSError as err:
        log.warning("заметку не удалось удалить (%s): %s", path, err)
        return {"deleted": False, "why": str(err)}
    store.update(rec_id, {"vault_path": None, "has_minutes": False})
    log.info("заметка удалена: %s", path)
    return {"deleted": True, "path": str(path), "relative": _rel_to_vault(path)}


# --------------------------------------------------------------------------- #
# состояние хранилища
# --------------------------------------------------------------------------- #
def _is_writable(root: Path) -> bool:
    """Проверка записи: создаём и удаляем временный файл."""
    folder = root
    while not folder.is_dir() and folder.parent != folder:
        folder = folder.parent
    probe = folder / ".hagen-write-test.tmp"
    try:
        with io.open(probe, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("проверка записи\n")
        return True
    except OSError:
        return False
    finally:
        try:
            if probe.exists():
                probe.unlink()
        except OSError:
            pass


def _count_notes(root: Path) -> int:
    index = _index_path()
    total = 0
    try:
        for path in root.rglob("*.md"):
            if path.is_file() and path != index and not path.name.startswith("."):
                total += 1
    except OSError:
        pass
    return total


def vault_status() -> dict[str, Any]:
    """Короткая сводка о хранилище для интерфейса настроек."""
    root = config.vault_root()
    exists = root.is_dir()
    return {
        "root": str(root),
        "exists": exists,
        "writable": _is_writable(root),
        "categories": list_categories(),
        "notes": _count_notes(root) if exists else 0,
    }


# --------------------------------------------------------------------------- #
# «Связать с»: ссылки из заметки записи на другие заметки сейфа (17.09)
# --------------------------------------------------------------------------- #
# Решение 17.09: ссылка ставится на ЗАМЕТКИ, а не на папки. У папки нет
# вики-ссылки: в заметке остался бы просто текст пути, по которому Obsidian не
# перейдёт и графа не построит. Заметка же связывается по-настоящему — и запись
# появляется в обратных ссылках каждой связанной заметки.
#
# Сами связи живут в данных записи, а не в файле: заметка целиком
# перерисовывается из программы (решение 15.09), и всё, что дописано
# руками в Obsidian, пропало бы при следующем сохранении.

#: Папки, в которые за заметками не ходим: служебное Obsidian, корзина, наши же
#: резервные копии. Иначе в подсказках всплывут сотни старых версий.
_SKIP_DIRS = {".obsidian", ".trash", ".git", ".vscode", "node_modules",
              "__pycache__", ".versions", ".stfolder"}

_NOTES_TTL_S = 60.0
_NOTES_MAX = 30000
_notes_cache: dict[str, Any] = {"root": "", "stamp": 0.0, "items": []}


def _scan_vault_notes() -> list[dict[str, str]]:
    """Все .md сейфа: {title, path}. Кэш на минуту — сейф бывает большой.

    Читаются только имена файлов, не содержимое: обход должен быть быстрым даже
    на сейфе в тысячи заметок, а заголовок у Obsidian и так равен имени файла.
    """
    base = _vault_base()
    root = str(base)
    now = time.time()
    if (_notes_cache["root"] == root
            and now - float(_notes_cache["stamp"]) < _NOTES_TTL_S):
        return _notes_cache["items"]        # type: ignore[return-value]

    items: list[dict[str, str]] = []
    if base.is_dir():
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames
                           if not d.startswith(".") and d not in _SKIP_DIRS]
            for name in filenames:
                if not name.lower().endswith(".md"):
                    continue
                full = Path(dirpath) / name
                items.append({"title": name[:-3], "path": _rel_to_vault(full)})
                if len(items) >= _NOTES_MAX:
                    break
            if len(items) >= _NOTES_MAX:
                break
    items.sort(key=lambda it: it["title"].lower())
    _notes_cache.update({"root": root, "stamp": now, "items": items})
    return items


def find_notes(query: str, limit: int = 40) -> list[dict[str, str]]:
    """Заметки сейфа по куску названия. Пустой запрос — ничего, не весь сейф.

    Сначала те, чьё имя НАЧИНАЕТСЯ с запроса, потом остальные совпадения: иначе
    точное «Смета» тонет среди «Пересмотр сметы за третий квартал».
    """
    q = str(query or "").strip().lower()
    if len(q) < 2:
        return []
    starts: list[dict[str, str]] = []
    inside: list[dict[str, str]] = []
    for item in _scan_vault_notes():
        low = item["title"].lower()
        if low.startswith(q):
            starts.append(item)
        elif q in low or q in item["path"].lower():
            inside.append(item)
        if len(starts) >= limit:
            break
    return (starts + inside)[:limit]


# --------------------------------------------------------------------------- #
# папки сейфа: выбор папки проекта (20.09)
# --------------------------------------------------------------------------- #
# Папок в сейфе на порядок меньше, чем заметок, и пустой запрос здесь не
# страшен: список папок можно показать целиком, чтобы папку проекта выбирали
# глазами, а не угадывали название.
_FOLDERS_MAX = 5000
_folders_cache: dict[str, Any] = {"root": "", "stamp": 0.0, "items": []}


def _scan_vault_folders() -> list[dict[str, str]]:
    """Все папки сейфа: {title, path}. Кэш на минуту, как у заметок."""
    base = _vault_base()
    root = str(base)
    now = time.time()
    if (_folders_cache["root"] == root
            and now - float(_folders_cache["stamp"]) < _NOTES_TTL_S):
        return _folders_cache["items"]       # type: ignore[return-value]

    items: list[dict[str, str]] = []
    if base.is_dir():
        for dirpath, dirnames, _files in os.walk(base):
            dirnames[:] = [d for d in dirnames
                           if not d.startswith(".") and d not in _SKIP_DIRS]
            for name in dirnames:
                full = Path(dirpath) / name
                items.append({"title": name, "path": _rel_to_vault(full)})
                if len(items) >= _FOLDERS_MAX:
                    break
            if len(items) >= _FOLDERS_MAX:
                break
    items.sort(key=lambda it: it["path"].lower())
    _folders_cache.update({"root": root, "stamp": now, "items": items})
    return items


def find_folders(query: str = "", limit: int = 200) -> list[dict[str, str]]:
    """Папки сейфа по куску имени или пути. Пустой запрос отдаёт начало списка."""
    q = str(query or "").strip().lower()
    items = _scan_vault_folders()
    if not q:
        return items[:limit]
    starts = [it for it in items if it["title"].lower().startswith(q)]
    inside = [it for it in items
              if not it["title"].lower().startswith(q)
              and (q in it["title"].lower() or q in it["path"].lower())]
    return (starts + inside)[:limit]


def _wikilink(link: dict[str, str]) -> str:
    """Вики-ссылка на заметку. Тёзки различаем путём, иначе хватает имени.

    Obsidian находит заметку по имени, где бы она ни лежала, поэтому короткая
    ссылка переживает перенос файла. Но если в сейфе две заметки с одинаковым
    именем, короткая ссылка ведёт наугад: там пишем путь.
    """
    title = str(link.get("title") or "").strip()
    path = str(link.get("path") or "").strip()
    if not title:
        return ""
    same = [it for it in _scan_vault_notes() if it["title"].lower() == title.lower()]
    if len(same) > 1 and path:
        stem = path[:-3] if path.lower().endswith(".md") else path
        return f"[[{stem}|{title}]]"
    return f"[[{title}]]"




def _notes_block(meta: dict[str, Any]) -> list[str]:
    """«Мои заметки» — надиктованное автором (20.09).

    Это не часть разговора, поэтому в стенограмму заметки не попадают и стоят
    своим разделом. Раздел второго уровня внутри «О записи»: главных разделов в
    заметке ровно столько, сколько документов, плюс стенограмма (формат 15.09).
    """
    notes = store.rec_notes(meta or {})
    if not notes:
        return []
    out = [H_NOTES, ""]
    for note in notes:
        mark = "**Поручение.** " if note.get("kind") == "task" else ""
        out.append("- %s%s" % (mark, _escape_text(str(note.get("text") or ""))))
    out.append("")
    return out


def _carry_block(meta: dict[str, Any]) -> list[str]:
    """«С прошлого раза осталось» — поручения предыдущей записи серии (17.09).

    Решение 17.09: в новую встречу тянем НЕ весь прошлый протокол, а
    только незакрытые пункты. Связь выбирает человек, сама программа серию не
    угадывает: у еженедельной планёрки и у случайной встречи с тем же названием
    разная судьба, и ошибиться тут дороже, чем не подсказать.

    Раздел — второго уровня внутри «О записи», а не главный: главных разделов в
    заметке ровно столько, сколько документов, плюс стенограмма (формат 15.09).
    """
    from . import minutes as _minutes

    prev_id = str((meta or {}).get("continues") or "").strip()
    if not prev_id:
        return []
    prev = store.get(prev_id)
    if not prev:
        return []
    items = _minutes.open_items(prev_id)
    title = str(prev.get("title") or "").strip() or "предыдущая запись"
    dt = _meta_dt(prev).strftime("%d.%m.%Y")
    out = [H_CARRY, "", f"Продолжение записи «{title}» от {dt}."]
    if items:
        out.append("")
        out += ["- " + _escape_text(item) for item in items]
    else:
        out.append("")
        out.append("Незакрытых пунктов с прошлого раза не осталось.")
    out.append("")
    return out
