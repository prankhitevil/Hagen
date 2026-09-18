# -*- coding: utf-8 -*-
"""Место на диске: сколько занимают звук записей и видео, срок хранения звука.

Решения 13.09.2026:
  * звук звонков хранится как раньше — бессрочно, пока его не удалят;
  * в настройках видно, где он лежит и сколько места занимает;
  * есть параметр «удалять звук звонков старше N дней» (0 — не удалять).

Срок касается только записей с микрофона (звонков): у видеозаписей, что
хранить, решает сама форма раздела «Видео». Возраст считается по последней
записи звука в дорожку (время изменения файла): дописанная после «Стоп» запись
не удалится раньше срока. Удаляется только звук — стенограмма, документы и
заметка остаются, как при «Удалить → только видео и звук».
"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any, Callable

from . import config, store

log = logging.getLogger("hagen.storage")

TRACKS = (store.TRACK_MIC, store.TRACK_FAR, store.TRACK_FILE)


def _size(path: Path) -> tuple[int, float]:
    try:
        st = path.stat()
    except OSError:
        return 0, 0.0
    return int(st.st_size), float(st.st_mtime)


def retention_days() -> int:
    try:
        return max(0, int(config.get("audio_retention_days") or 0))
    except (TypeError, ValueError):
        return 0


def audio_usage() -> dict[str, Any]:
    """Звук записей в папке программы: всего, по звонкам и видео, самый старый."""
    total = calls = videos = 0
    with_audio = calls_n = 0
    oldest = None
    for meta in store.list_all():
        rec_bytes = 0
        newest = 0.0
        for tr in TRACKS:
            size, mtime = _size(store.track_path(meta["id"], tr))
            rec_bytes += size
            newest = max(newest, mtime)
        if not rec_bytes:
            continue
        with_audio += 1
        total += rec_bytes
        if meta.get("source") == "live":
            calls += rec_bytes
            calls_n += 1
            oldest = newest if oldest is None else min(oldest, newest)
        else:
            videos += rec_bytes
    return {"path": str(config.DATA_DIR), "bytes": total, "records": with_audio,
            "calls_bytes": calls, "calls_records": calls_n, "video_audio_bytes": videos,
            "oldest_call_audio": oldest}


def assets_usage() -> dict[str, Any]:
    """Видео и звук, скачанные разделом «Видео» (папка вне сейфа)."""
    root = store.assets_root()
    total = files = 0
    try:
        for item in root.rglob("*"):
            if item.is_file():
                size, _mtime = _size(item)
                total += size
                files += 1
    except OSError:
        pass
    return {"path": str(root), "bytes": total, "files": files, "exists": root.exists()}


def expired(days: int, busy: Callable[[str], bool] | None = None,
            now: float | None = None) -> list[dict[str, Any]]:
    """Звонки, чей звук старше срока и сейчас никому не нужен."""
    if days <= 0:
        return []
    from . import speakers

    limit = (now if now is not None else time.time()) - days * 86400
    out = []
    for meta in store.list_all():
        rec_id = meta["id"]
        if meta.get("source") != "live" or meta.get("media_removed"):
            continue
        if meta.get("status") in ("recording", "queued", "processing"):
            continue
        if speakers.pending_split(meta) or meta.get("diarize_status") in ("queued", "running"):
            continue
        if busy is not None and busy(rec_id):
            continue
        sizes = [_size(store.track_path(rec_id, tr)) for tr in TRACKS]
        present = [(s, m) for s, m in sizes if s]
        if not present:
            continue
        newest = max(m for _s, m in present)
        if newest < limit:
            out.append({"id": rec_id, "title": meta.get("title"), "bytes": sum(s for s, _m in present),
                        "audio_at": newest})
    return out


def purge(days: int | None = None, busy: Callable[[str], bool] | None = None,
          now: float | None = None) -> dict[str, Any]:
    """Удалить звук звонков старше срока. Текст, документы и заметки остаются."""
    days = retention_days() if days is None else int(days)
    removed = []
    freed = 0
    for item in expired(days, busy=busy, now=now):
        res = store.drop_media(item["id"])
        if res.get("freed_bytes"):
            freed += int(res["freed_bytes"])
            removed.append(item["id"])
    if removed:
        log.info("срок хранения %d дн.: удалён звук записей — %d, освобождено %.0f МБ",
                 days, len(removed), freed / 1048576.0)
    return {"days": days, "removed": removed, "freed_bytes": freed}
