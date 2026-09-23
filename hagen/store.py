# -*- coding: utf-8 -*-
"""Хранилище записей на диске. Одна папка = одна запись.

data/<rec_id>/
    meta.json          описание записи
    mic.wav            дорожка «Я» (микрофон)
    far.wav            дорожка «участники звонка» (петля вывода)
    source.wav         единая дорожка для импортированных файлов
    transcript.json    реплики
    diarization.json   сырой результат pyannote и эмбеддинги
"""
from __future__ import annotations

import io
import json
import logging
import os
import re
import shutil
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from . import config

log = logging.getLogger("hagen.store")

_locks: dict[str, threading.RLock] = {}
_locks_guard = threading.Lock()

TRACK_MIC = "mic"
TRACK_FAR = "far"
TRACK_FILE = "file"

SPEAKER_ME = "Я"
SPEAKER_FAR = "Участник"


def owner_name() -> str:
    """Как называть владельца записи в стенограмме и документах.

    В данных реплики микрофона всегда подписаны «Я» — так их узнают разметка,
    база голосов и эхо-фильтр. Показывается и уходит в модель имя из
    настройки owner_name (решение 13.09: «или я, или Иван П.»).
    """
    name = " ".join(str(config.get("owner_name") or "").split()).strip()
    return name[:60] or SPEAKER_ME


def display_speaker(name: str | None) -> str:
    """Имя говорящего для показа: «Я» заменяется именем владельца."""
    name = str(name or "").strip()
    return owner_name() if name == SPEAKER_ME else name

_ID_RE = re.compile(r"[0-9]{8}-[0-9]{6}-[0-9a-f]{4}")


def _lock_for(rec_id: str) -> threading.RLock:
    with _locks_guard:
        lk = _locks.get(rec_id)
        if lk is None:
            lk = threading.RLock()
            _locks[rec_id] = lk
        return lk


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def new_id() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:4]


def rec_dir(rec_id: str) -> Path:
    return config.DATA_DIR / rec_id


def paths(rec_id: str) -> dict[str, Path]:
    # Номер записи — единственная часть пути, которая приходит извне: чужой
    # вид номера («..» и т. п.) дальше папки data не пускаем (находка 18).
    if not _valid_id(rec_id):
        raise ValueError("Неверный номер записи: %r" % (rec_id,))
    d = rec_dir(rec_id)
    return {
        "dir": d,
        "meta": d / "meta.json",
        "mic": d / "mic.wav",
        "far": d / "far.wav",
        "file": d / "source.wav",
        "subs": d / "source.vtt",     # готовые субтитры источника, если они были
        "transcript": d / "transcript.json",
        "diarization": d / "diarization.json",
        "minutes": d / "minutes.md",
    }


def assets_root() -> Path:
    """Папка для тяжёлого: скачанных видео и текстовых копий транскрипта.

    Держится ОТДЕЛЬНО от сейфа Obsidian намеренно: сейф лежит на Яндекс.Диске,
    и каждый скачанный ролик уезжал бы в облако и съедал место там.
    """
    raw = str(config.get("assets_dir") or "").strip()
    if raw:
        return Path(os.path.expandvars(raw)).expanduser()
    return Path.home() / "Videos" / "PK_Summarizer"


def assets_dir(rec_id: str, folder: str | None = None) -> Path:
    """Папка одной записи внутри хранилища тяжёлых файлов."""
    name = (folder or "").strip() or rec_id
    return assets_root() / name


def _write_json(path: Path, payload: Any, create_dir: bool = False) -> None:
    """Атомарная запись. Папку заводим, только когда об этом просят явно.

    Иначе запись воскрешала бы удалённую запись: отменённая задача договаривает
    свой шаг уже после удаления, mkdir поднимает папку заново, и на диске
    остаётся невидимый мусор — папка без meta.json, которой нет в списке.
    """
    if not path.parent.exists():
        if not create_dir:
            log.info("запись отменена, папки уже нет: %s", path.parent.name)
            return
        path.parent.mkdir(parents=True, exist_ok=True)
    config.atomic_json(path, payload)


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        with io.open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (json.JSONDecodeError, OSError):
        return default


def _valid_id(rec_id: str) -> bool:
    return bool(rec_id) and _ID_RE.fullmatch(str(rec_id)) is not None


def create(
    title: str | None = None,
    mode: str = "online",
    category: str | None = None,
    source: str = "live",
    source_name: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Завести запись. extra — дополнительные поля meta.json.

    Через extra режим «Видео» кладёт в запись ссылку, язык, выбранный движок
    распознавания и карту этапов обработки, не заводя второго хранилища.
    """
    rec_id = new_id()
    now = datetime.now()
    meta = {
        "id": rec_id,
        "title": (title or "").strip() or ("Запись " + now.strftime("%d.%m %H:%M")),
        "created_at": _now_iso(),
        "mode": mode,
        "source": source,
        "source_name": source_name,
        "category": category or config.get("default_category"),
        "status": "recording" if source == "live" else "queued",
        "duration_s": 0.0,
        "tracks": [],
        "diarized": False,
        "diarize_status": "none",
        "diarize_progress": 0.0,
        "diarize_eta_s": None,
        "speakers": {},
        "participants": [],
        "meeting": None,
        "vault_path": None,
        "has_minutes": False,
        "error": None,
    }
    for key, value in (extra or {}).items():
        if key not in ("id", "created_at"):     # эти поля задаёт только хранилище
            meta[key] = value
    rec_dir(rec_id).mkdir(parents=True, exist_ok=True)
    _write_json(paths(rec_id)["meta"], meta)
    _write_json(paths(rec_id)["transcript"], {"segments": []})
    return _with_flags(meta)


#: Запись короче этого считается несостоявшейся: звука в ней нет (22.09).
EMPTY_MIN_S = 1.0


def is_empty(meta: dict[str, Any]) -> bool:
    """Запись не состоялась: живая, остановлена, а звука меньше секунды.

    Один признак на список и на открытие записи. Видео сюда не попадают: у
    файла с субтитрами звука может не быть вовсе, а текст есть. Запись, у
    которой звук удалили руками, тоже не пустая — стенограмма осталась.
    """
    return (meta.get("source", "live") == "live" and meta.get("status") == "recorded"
            and not meta.get("media_removed")
            and float(meta.get("duration_s") or 0.0) < EMPTY_MIN_S)


def _with_flags(meta: Any) -> Any:
    """Признаки, которые считаются из самой записи, а не хранятся в ней."""
    if isinstance(meta, dict) and meta.get("id"):
        meta["empty"] = is_empty(meta)
    return meta


def get(rec_id: str) -> dict[str, Any] | None:
    if not _valid_id(rec_id):
        return None
    return _with_flags(_read_json(paths(rec_id)["meta"], None))


def update(rec_id: str, patch: dict[str, Any]) -> dict[str, Any] | None:
    if not _valid_id(rec_id):
        return None
    with _lock_for(rec_id):
        meta = _read_json(paths(rec_id)["meta"], None)
        if meta is None:
            return None
        meta.update(patch)
        meta.pop("empty", None)           # признак считается, а не хранится
        meta["updated_at"] = _now_iso()
        _write_json(paths(rec_id)["meta"], meta)
        return _with_flags(meta)


def list_all() -> list[dict[str, Any]]:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    items: list[dict[str, Any]] = []
    for d in config.DATA_DIR.iterdir():
        if not d.is_dir():
            continue
        meta = _read_json(d / "meta.json", None)
        if isinstance(meta, dict) and meta.get("id"):
            items.append(_with_flags(meta))
    items.sort(key=lambda m: str(m.get("created_at") or ""), reverse=True)
    return items


def delete(rec_id: str) -> bool:
    if not _valid_id(rec_id):
        return False
    d = rec_dir(rec_id)
    if not d.exists():
        return False
    with _lock_for(rec_id):
        shutil.rmtree(d, ignore_errors=True)
    return not d.exists()


#: Что в папке тяжёлых файлов НЕ считается тяжёлым и остаётся жить.
#: Перечисляем именно лёгкое, а не тяжёлое: у недокачанных файлов yt-dlp
#: расширения вида .part и .ytdl, и списком «тяжёлых» их не поймать —
#: гигабайты оставались бы лежать, а запись считалась бы очищенной.
_KEEP_SUFFIXES = (".txt", ".md", ".vtt", ".srt", ".json", ".log")


def _rm_file(path: Path) -> int:
    """Убрать файл. Возвращает освобождённые байты, 0 — если не вышло."""
    try:
        size = path.stat().st_size
    except OSError:
        return 0
    try:
        path.unlink()
    except OSError:
        return 0
    return size


def drop_media(rec_id: str) -> dict[str, Any]:
    """Стереть звук и видео записи, оставив весь текст.

    Ответ «удалить только видео и звук» в диалоге удаления: гигабайты уходят,
    а стенограмма остаётся — по ней потом собирается протокол или саммари.
    Метку `media_removed` ставим не для красоты: без неё интерфейс продолжал бы
    предлагать «Перечитать точнее» и разметку говорящих, а им нужен звук,
    которого уже нет.
    """
    meta = get(rec_id)
    if meta is None:
        return {"ok": False, "freed_bytes": 0, "files": 0}

    freed = 0
    count = 0
    left: list[str] = []
    with _lock_for(rec_id):
        p = paths(rec_id)
        for key in (TRACK_MIC, TRACK_FAR, TRACK_FILE):
            size = _rm_file(p[key])
            if size:
                freed += size
                count += 1
            elif p[key].exists():
                left.append(p[key].name)     # файл занят — соврать нельзя

        # Прямой путь к видео — для записей, сделанных до появления папок
        # ассетов: у них в meta есть video_path, а assets_folder пустой.
        # Трогаем только то, что лежит внутри хранилища тяжёлых файлов: если
        # человек указал своё видео где-то ещё, это его файл, не наш.
        raw_video = str(meta.get("video_path") or "").strip()
        if raw_video:
            v = Path(raw_video)
            try:
                ours = v.resolve().is_relative_to(assets_root().resolve())
            except (OSError, ValueError):
                ours = False
            if ours and v.is_file():
                size = _rm_file(v)
                if size:
                    freed += size
                    count += 1
                elif v.exists():
                    left.append(v.name)

        folder = str(meta.get("assets_folder") or "")
        if folder:
            d = assets_dir(rec_id, folder)
            try:
                entries = sorted(d.rglob("*")) if d.is_dir() else []
            except OSError:
                entries = []
            for item in entries:
                if not item.is_file() or item.suffix.lower() in _KEEP_SUFFIXES:
                    continue
                size = _rm_file(item)
                if size:
                    freed += size
                    count += 1
                elif item.exists():
                    left.append(item.name)
            # Пустую папку не оставляем, но текстовую копию стенограммы,
            # если она там лежит, не трогаем — это не «тяжёлое».
            try:
                if d.is_dir() and not any(d.iterdir()):
                    d.rmdir()
            except OSError:
                pass

    # Метку ставим ТОЛЬКО если на диске правда ничего не осталось. Файл может
    # быть занят антивирусом или открытым плеером; тогда кнопки погасли бы, а
    # гигабайты остались — и человек считал бы, что место освободилось.
    patch: dict[str, Any] = {"video_bytes": None}
    if left:
        log.warning("не удалось убрать: %s", ", ".join(left))
    else:
        patch.update({"tracks": [], "video_path": None, "media_removed": True})
    update(rec_id, patch)
    return {"ok": not left, "freed_bytes": freed, "files": count, "left": left}


def drop_assets(rec_id: str) -> int:
    """Снести папку записи в хранилище тяжёлых файлов целиком.

    Вместе с видео уходит и текстовая копия стенограммы — это ответ
    «удалить всё», когда от записи не должно остаться ничего.
    """
    meta = get(rec_id) or {}
    folder = str(meta.get("assets_folder") or "")
    if not folder:
        return 0
    d = assets_dir(rec_id, folder)
    if not d.is_dir():
        return 0
    freed = 0
    try:
        for item in d.rglob("*"):
            if item.is_file():
                try:
                    freed += item.stat().st_size
                except OSError:
                    pass
    except OSError:
        pass
    shutil.rmtree(d, ignore_errors=True)
    return freed


def load_transcript(rec_id: str) -> dict[str, Any]:
    data = _read_json(paths(rec_id)["transcript"], {"segments": []})
    if not isinstance(data, dict) or "segments" not in data:
        data = {"segments": []}
    return data


def save_transcript(rec_id: str, data: dict[str, Any]) -> None:
    with _lock_for(rec_id):
        _write_json(paths(rec_id)["transcript"], data)


@contextmanager
def edit_transcript(rec_id: str):
    """Прочитать стенограмму, поправить на месте, записать — под замком записи.

    Для тех, кто меняет реплики по одной, не пересобирая список: отсев эха
    ставит и снимает пометки. Замок держится всё время правки, чтобы соседний
    поток не вписал реплику поверх. Выход по исключению ничего не пишет.

    Не пишем и тогда, когда править оказалось нечего: `return` из блока с
    `with` всё равно доводит до конца, и стенограмма переписывалась бы на
    каждой остановке записи, а у записи без стенограммы появлялся бы файл с
    пустым списком реплик.
    """
    with _lock_for(rec_id):
        data = load_transcript(rec_id)
        before = json.dumps(data, ensure_ascii=False, sort_keys=True)
        yield data
        if json.dumps(data, ensure_ascii=False, sort_keys=True) != before:
            _write_json(paths(rec_id)["transcript"], data)


def make_segment(
    track: str,
    start: float,
    end: float,
    text: str,
    speaker: str | None = None,
    speaker_key: str | None = None,
) -> dict[str, Any]:
    if speaker is None:
        speaker = SPEAKER_ME if track == TRACK_MIC else SPEAKER_FAR
    if speaker_key is None:
        speaker_key = "me" if track == TRACK_MIC else "far"
    return {
        "id": uuid.uuid4().hex[:8],
        "track": track,
        "start": round(float(start), 3),
        "end": round(float(end), 3),
        "text": text,
        "speaker": speaker,
        "speaker_key": speaker_key,
        "speaker_locked": track == TRACK_MIC,
        "suggestion": None,
        "created_at": time.time(),
    }


def append_segment(rec_id: str, seg: dict[str, Any]) -> dict[str, Any]:
    with _lock_for(rec_id):
        data = load_transcript(rec_id)
        data["segments"].append(seg)
        _write_json(paths(rec_id)["transcript"], data)
        return seg


def replace_segments(rec_id: str, segments: list[dict[str, Any]]) -> None:
    with _lock_for(rec_id):
        data = load_transcript(rec_id)
        data["segments"] = segments
        _write_json(paths(rec_id)["transcript"], data)


def sorted_segments(rec_id: str, include_echo: bool = False) -> list[dict[str, Any]]:
    """Реплики по порядку. Эхо колонок по умолчанию не показываем.

    Помеченные эхом реплики остаются в файле — их видно, если специально
    попросить, — но ни в стенограмму, ни в протокол, ни в Obsidian они не идут.
    """
    segs = load_transcript(rec_id).get("segments") or []
    if not include_echo:
        segs = [s for s in segs if not s.get("echo")]
    return sorted(segs, key=lambda s: (float(s.get("start") or 0.0), str(s.get("track"))))


def _collect_participants(transcript: dict[str, Any], speakers: dict[str, Any]) -> list[str]:
    names: list[str] = []
    for seg in transcript.get("segments") or []:
        nm = (seg.get("speaker") or "").strip()
        if nm and nm not in names:
            names.append(nm)
    for _key, info in (speakers or {}).items():
        nm = (info or {}).get("name")
        if nm and nm not in names:
            names.append(nm)
    return names


def count_voices(transcript: dict[str, Any], meta: dict[str, Any]) -> dict[str, int]:
    """Сколько голосов в записи: своих и остальных.

    «Свои» — это дорожка микрофона: сам владелец и те, кто сидел рядом с ним
    (их разделяет переключатель «Со мной в комнате были ещё люди»). «Остальные»
    — дорожка собеседников, где все участники звонка идут одним звуком:
    трое в переговорке на том конце — это три голоса, а не один.

    У импортированных записей (видео, файл) дорожка одна и разделения на «своих»
    и «чужих» в ней нет. Владелец попадает в «своих», только если его голос
    узнан по базе голосов, то есть он когда-то подписал говорящего «Это я».
    """
    owner_id = None
    if str(meta.get("source") or "") != "live":
        try:
            from . import voices

            owner_id = (voices.owner_person(create=False) or {}).get("id")
        except Exception:
            owner_id = None
    speakers = meta.get("speakers") or {}
    mine: set[str] = set()
    others: set[str] = set()
    for seg in transcript.get("segments") or []:
        if seg.get("echo"):
            continue
        key = str(seg.get("speaker_key") or "")
        if not key:
            continue
        track = seg.get("track")
        if track == TRACK_MIC:
            mine.add(key)
        elif track == TRACK_FAR:
            others.add(key)
        elif owner_id and ((speakers.get(key) or {}).get("person_id") == owner_id):
            mine.add(key)
        else:
            others.add(key)
    return {"mine": len(mine), "others": len(others)}


def rename_speaker(rec_id: str, speaker_key: str, new_name: str, apply_all: bool = True) -> int:
    """Переименовать говорящего. Возвращает число изменённых реплик."""
    changed = 0
    with _lock_for(rec_id):
        data = load_transcript(rec_id)
        for seg in data.get("segments") or []:
            if apply_all and seg.get("speaker_key") == speaker_key:
                seg["speaker"] = new_name
                seg["speaker_locked"] = True
                seg["suggestion"] = None
                changed += 1
        _write_json(paths(rec_id)["transcript"], data)
        meta = _read_json(paths(rec_id)["meta"], None)
        if meta is not None:
            speakers = dict(meta.get("speakers") or {})
            entry = dict(speakers.get(speaker_key) or {})
            entry["name"] = new_name
            entry["confirmed"] = True
            speakers[speaker_key] = entry
            meta["speakers"] = speakers
            meta["participants"] = _collect_participants(data, speakers)
            meta["voices"] = count_voices(data, meta)
            _write_json(paths(rec_id)["meta"], meta)
    return changed


def refresh_participants(rec_id: str) -> list[str]:
    with _lock_for(rec_id):
        data = load_transcript(rec_id)
        meta = _read_json(paths(rec_id)["meta"], None) or {}
        names = _collect_participants(data, meta.get("speakers") or {})
        if meta:
            meta["participants"] = names
            meta["voices"] = count_voices(data, meta)
            _write_json(paths(rec_id)["meta"], meta)
        return names


def track_path(rec_id: str, track: str) -> Path:
    """Файл звуковой дорожки. Имя дорожки проверяется строго.

    Раньше на незнакомое имя молча возвращался source.wav. С появлением видео
    это стало опасно: опечатка в имени дорожки записала бы файл поверх звука
    записи. Лучше громкая ошибка, чем испорченная запись.
    """
    if track not in (TRACK_MIC, TRACK_FAR, TRACK_FILE):
        raise ValueError("Неизвестная звуковая дорожка: %r" % (track,))
    return paths(rec_id)[track]


def existing_tracks(rec_id: str) -> list[str]:
    p = paths(rec_id)
    out = []
    for t in (TRACK_MIC, TRACK_FAR, TRACK_FILE):
        fp = p[t]
        if fp.exists() and fp.stat().st_size > 44:
            out.append(t)
    return out


# --------------------------------------------------------------------------- #
# проект и теги записи (решение 17.09)
# --------------------------------------------------------------------------- #
# Признаки нужны не программе, а Obsidian: по ним он фильтрует, строит граф и
# собирает подборки. Внутри программы по ним ничего не строится — «псевдо-
# Obsidian» решено не делать. Поэтому здесь только чистка значения и
# память о том, что уже вводили, чтобы в следующий раз подсказать.

#: Тег в Obsidian — одно слово: пробел разорвал бы его на два тега.
_TAG_BAD = re.compile(r"[^0-9A-Za-zА-Яа-яЁё_/-]+")

MAX_PROJECT = 80
MAX_TAG = 40
MAX_TAGS = 12


def clean_project(name: Any) -> str:
    """Имя проекта: обрезано по краям и по длине. Пусто — значит «без проекта»."""
    return str(name or "").strip()[:MAX_PROJECT]


def clean_tag(value: Any) -> str:
    """Один тег: без решётки, без пробелов, не длиннее MAX_TAG.

    Пробелы и точки заменяются дефисом, а не выбрасываются: «третий квартал»
    должен остаться читаемым «третий-квартал», а не слипнуться в «третийквартал».
    """
    text = str(value or "").strip().lstrip("#").strip()
    text = _TAG_BAD.sub("-", text).strip("-")
    return text[:MAX_TAG]


def clean_tags(values: Any) -> list[str]:
    """Список тегов: почищенные, без пустых, без повторов, не больше MAX_TAGS."""
    if isinstance(values, str):
        values = re.split(r"[,\s]+", values)
    out: list[str] = []
    for raw in (values or []):
        tag = clean_tag(raw)
        if tag and tag.lower() not in {t.lower() for t in out}:
            out.append(tag)
        if len(out) >= MAX_TAGS:
            break
    return out


def _remember(key: str, values: list[str], limit: int = 200) -> None:
    """Дописать значения в список настроек, сохранив порядок и не плодя повторов."""
    known = [str(v) for v in (config.get(key) or []) if str(v).strip()]
    lowered = {v.lower() for v in known}
    added = False
    for value in values:
        if value and value.lower() not in lowered:
            known.append(value)
            lowered.add(value.lower())
            added = True
    if added:
        config.save({key: known[-limit:]})


def known_projects() -> list[str]:
    return [str(v) for v in (config.get("projects") or []) if str(v).strip()]


def known_tags() -> list[str]:
    return [str(v) for v in (config.get("rec_tags") or []) if str(v).strip()]


def remember_project(name: str) -> None:
    name = clean_project(name)
    if name:
        _remember("projects", [name])


def remember_tags(tags: list[str]) -> None:
    tags = clean_tags(tags)
    if tags:
        _remember("rec_tags", tags)


#: Папка проекта в сейфе (решение 20.09). У проекта может быть связанная папка:
#: тогда заметки этого проекта ложатся туда, а не в папку категории. Связка
#: необязательная — проект остаётся своим списком в настройках и работает без
#: сейфа. Здесь только имя и путь; во что превращается путь и лежит ли он внутри
#: сейфа — дело obsidian.py, хранилище про сейф не знает.
MAX_FOLDER = 240


def clean_project_folder(value: Any) -> str:
    """Путь папки относительно корня сейфа, через «/». Пусто — связки нет.

    Обратные косые черты приводятся к «/», края чистятся. Переходы «..» и
    двоеточие диска отбрасывают путь целиком: папка проекта живёт внутри сейфа,
    и «..\\..\\Windows» тут не адрес, а ошибка.
    """
    text = str(value or "").strip().replace("\\", "/").strip("/")
    text = re.sub(r"/{2,}", "/", text)
    if not text or ":" in text:
        return ""
    parts = [p.strip() for p in text.split("/")]
    if any(p in {"", ".", ".."} for p in parts):
        return ""
    return "/".join(parts)[:MAX_FOLDER]


def known_project_folders() -> dict[str, str]:
    """Карта «проект → папка в сейфе». Только непустые связки."""
    raw = config.get("project_folders") or {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, str] = {}
    for name, folder in raw.items():
        name = clean_project(name)
        folder = clean_project_folder(folder)
        if name and folder:
            out[name] = folder
    return out


def project_folder(name: Any) -> str:
    """Папка проекта или пусто. Регистр имени не важен: «АКП» и «акп» — один проект."""
    name = clean_project(name)
    if not name:
        return ""
    folders = known_project_folders()
    if name in folders:
        return folders[name]
    low = name.lower()
    for known, folder in folders.items():
        if known.lower() == low:
            return folder
    return ""


def set_project_folder(name: Any, folder: Any) -> dict[str, str]:
    """Привязать папку к проекту; пустая папка связку снимает.

    Проект заодно запоминается в списке подсказок: связку заводят для проекта,
    которым пользуются, и отдельно вводить его имя второй раз незачем.
    """
    name = clean_project(name)
    if not name:
        raise ValueError("Не указан проект")
    folder = clean_project_folder(folder)
    current = known_project_folders()
    low = name.lower()
    kept = {k: v for k, v in current.items() if k.lower() != low}
    if folder:
        kept[name] = folder
        remember_project(name)
    config.save({"project_folders": kept})
    return kept


def rec_project(meta: dict[str, Any]) -> str:
    return clean_project((meta or {}).get("project"))


def rec_tags(meta: dict[str, Any]) -> list[str]:
    return clean_tags((meta or {}).get("tags"))


#: Голосовые заметки к записи (20.09). Надиктованное своё: примечание, вывод,
#: поручение. В стенограмму они не попадают — там речь встречи, как она
#: прозвучала; заметки живут отдельным списком и отдельным разделом заметки, а
#: в документах учитываются как слово автора, то есть в первую очередь.
MAX_NOTES = 100
MAX_NOTE = 2000

#: Род заметки: просто запись для себя или поручение, которое уйдёт в Todoist.
NOTE_KINDS = ("note", "task")


def clean_note_kind(value: Any) -> str:
    kind = str(value or "note").strip().lower()
    return kind if kind in NOTE_KINDS else "note"


def clean_notes(values: Any) -> list[dict[str, Any]]:
    """Список заметок [{text, kind, at}] — без пустых, не длиннее MAX_NOTES."""
    out: list[dict[str, Any]] = []
    for raw in (values or []):
        if isinstance(raw, str):
            raw = {"text": raw}
        if not isinstance(raw, dict):
            continue
        text = re.sub(r"\s+", " ", str(raw.get("text") or "").strip())[:MAX_NOTE]
        if not text:
            continue
        out.append({"text": text,
                    "kind": clean_note_kind(raw.get("kind")),
                    "at": str(raw.get("at") or _now_iso()),
                    "sent": bool(raw.get("sent"))})
        if len(out) >= MAX_NOTES:
            break
    return out


def rec_notes(meta: dict[str, Any]) -> list[dict[str, Any]]:
    return clean_notes((meta or {}).get("notes"))


def add_note(rec_id: str, text: str, kind: str = "note",
             sent: bool = False) -> dict[str, Any] | None:
    """Дописать заметку к записи. Возвращает обновлённую meta или None."""
    meta = get(rec_id)
    if meta is None:
        return None
    notes = rec_notes(meta)
    notes.append({"text": str(text or ""), "kind": clean_note_kind(kind),
                  "at": _now_iso(), "sent": bool(sent)})
    return update(rec_id, {"notes": clean_notes(notes)})


#: Роли участников В ЭТОЙ ЗАПИСИ (20.09). Обычно роль у человека одна и живёт
#: в базе голосов, но подрядчик по одному проекту бывает партнёром по другому —
#: и тогда роль переопределяется здесь, только для этой записи. Ключ — имя
#: говорящего так, как оно стоит в репликах («Я» — владелец записи).
MAX_ROLES = 40


def clean_roles(values: Any) -> dict[str, dict[str, str]]:
    """{имя: {side, position}} — без пустых ролей и без имён без роли."""
    from . import voices

    out: dict[str, dict[str, str]] = {}
    if not isinstance(values, dict):
        return out
    for name, role in values.items():
        name = str(name or "").strip()[:MAX_PROJECT]
        if not name or not isinstance(role, dict):
            continue
        side = voices.clean_side(role.get("side"))
        position = voices.clean_position(role.get("position"))
        if not side and not position:
            continue
        out[name] = {"side": side, "position": position}
        if len(out) >= MAX_ROLES:
            break
    return out


def rec_roles(meta: dict[str, Any]) -> dict[str, dict[str, str]]:
    return clean_roles((meta or {}).get("roles"))


#: Связи записи с заметками сейфа («Связать с», 17.09). Хранятся здесь, а не в
#: файле заметки: заметка целиком перерисовывается из программы (решение 15.09), и дописанное руками в Obsidian пропало бы при сохранении.
MAX_LINKS = 20


def clean_links(values: Any) -> list[dict[str, str]]:
    """Список связей [{title, path}]: без пустых, без повторов по пути."""
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for raw in (values or []):
        if isinstance(raw, str):
            raw = {"title": raw, "path": ""}
        if not isinstance(raw, dict):
            continue
        title = str(raw.get("title") or "").strip()[:MAX_PROJECT]
        path = str(raw.get("path") or "").strip().replace("\\", "/")
        if not title:
            continue
        key = (path or title).lower()
        if key in seen:
            continue
        seen.add(key)
        out.append({"title": title, "path": path})
        if len(out) >= MAX_LINKS:
            break
    return out


def rec_links(meta: dict[str, Any]) -> list[dict[str, str]]:
    return clean_links((meta or {}).get("links"))


#: Вычеркнутые пункты документов (17.09). Модель выдала пять
#: пунктов, часть недостойна протокола — отметить лишние щелчком, и в заметку
#: они не уйдут. Ключ — вид документа, значение — тексты вычеркнутых строк.
#: Хранится текст, а не номер строки: номера съезжают при любой правке, а
#: пересборка документа сама обнуляет отметки, что и нужно.
MAX_DROPPED = 100


def norm_line(text: Any) -> str:
    """Строка документа в сравнимом виде: без разметки, пробелов и регистра."""
    body = str(text or "").strip()
    body = re.sub(r"^[\s>*+-]+", "", body)          # маркеры списка и цитаты
    body = re.sub(r"^\d+[.)]\s*", "", body)         # нумерация
    body = body.replace("*", "").replace("`", "").replace("~", "")
    body = re.sub(r"\s+", " ", body).strip().lower()
    # Точка в конце не должна решать судьбу пункта: человек мог отметить строку
    # и до, и после правки текста, а модель ставит точки непоследовательно.
    return body.rstrip(".,;:!")


def clean_dropped(values: Any) -> dict[str, list[str]]:
    """{вид документа: [вычеркнутые строки]} — без пустых и повторов."""
    out: dict[str, list[str]] = {}
    if not isinstance(values, dict):
        return out
    for key, lines in values.items():
        doc = str(key or "").strip()
        if not doc:
            continue
        kept: list[str] = []
        seen: set[str] = set()
        for raw in (lines or []):
            text = str(raw or "").strip()
            norm = norm_line(text)
            if not norm or norm in seen:
                continue
            seen.add(norm)
            kept.append(text)
            if len(kept) >= MAX_DROPPED:
                break
        if kept:
            out[doc] = kept
    return out


def rec_dropped(meta: dict[str, Any], doc: str = "") -> Any:
    """Вычеркнутое: всё сразу или по одному виду документа."""
    all_dropped = clean_dropped((meta or {}).get("dropped"))
    return all_dropped.get(doc, []) if doc else all_dropped
