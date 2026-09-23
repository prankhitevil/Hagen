# -*- coding: utf-8 -*-
"""Входящие с телефона: папки, за которыми следит программа, и общий приём файла.

Запись с телефона (диктофон в переговорной, разговор на ходу) должна попадать
в программу без кабеля и ручного переноса. Дорог две, и обе сходятся здесь:
  * папка входящих — человек указывает папки, программа смотрит за ними и
    берёт новый звуковой или видеофайл. Чем папка наполняется — LocalSend,
    «Связь с телефоном», облачный диск, загрузки Telegram Desktop — программе
    всё равно: ей важен только файл;
  * страница в локальной сети (`lan.py`) — телефон присылает файл сам.

Что с файлом делается — одно правило на обе дороги (`OPTS`, решение 23.09):
распознать и разметить голоса, документ не делать. Документ — по кнопке,
когда человек посмотрит стенограмму.

Слежение — опрос папок раз в несколько секунд из ядра: обычное чтение папки,
системных вызовов для этого не нужно. Файл берётся, только когда дописан:
размер и время не менялись `STABLE_S` подряд, и файл открывается на чтение —
LocalSend и облачные клиенты пишут по частям. Взятые файлы помнятся в
data\\inbox.json (путь, размер, время), чтобы не брать дважды и после
перезапуска. Файлы, лежавшие в папке в момент, когда её добавили, не берутся:
«входящие» — то, что пришло потом, в том числе пока программа была закрыта.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from . import audio_io, config
from .events import hub

log = logging.getLogger("hagen.inbox")

#: Что делать с пришедшим файлом: стенограмма и голоса, без документа. Звук
#: остаётся у записи, чтобы работали «Кто это говорит?» и повторная разметка.
OPTS: dict[str, Any] = {
    "make_summary": False,
    "diarize_auto": True,
    "video_kind": "meeting",
    "store_media": "audio",
    "prefer_transcript": False,
}

#: Как часто заглядывать в папки.
POLL_S = 5.0
#: Сколько файл должен пролежать без изменений, прежде чем его взять.
STABLE_S = 8.0
#: Записи о взятых файлах, которых в папках давно нет, забываются.
FORGET_AFTER_S = 30 * 86400

#: Куда кладётся копия файла до обработки — то же место, что у загрузки со
#: страницы: обработка удаляет файл оттуда сама, когда закончит.
UPLOAD_DIR = config.DATA_DIR / "_uploads"

#: Где помнить взятые файлы. Проверки подменяют путь.
SEEN_PATH: Path | None = None


def _seen_path() -> Path:
    return SEEN_PATH or (config.DATA_DIR / "inbox.json")


# ---------------------------------------------------------------- папки


def folders() -> list[dict[str, Any]]:
    """Папки из настроек: как записаны, раскрытый путь и есть ли такая папка."""
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in config.get("inbox_folders") or []:
        raw = str(raw or "").strip()
        if not raw:
            continue
        path = os.path.expandvars(raw).rstrip("\\/")
        if path.lower() in seen:
            continue
        seen.add(path.lower())
        out.append({"raw": raw, "path": path, "exists": Path(path).is_dir()})
    return out


# ---------------------------------------------------------------- приём файла


def accept(tmp: Path, name: str, origin: str, source_path: str = "") -> dict[str, Any]:
    """Файл уже лежит в data\\_uploads: завести запись и поставить обработку.

    `origin` — откуда пришёл: «inbox» (папка) или «phone» (страница в сети);
    для страницы и заметки это обычная запись из файла.
    """
    from . import media

    opts = dict(OPTS)
    opts["category"] = media.category_for_kind("meeting")
    extra: dict[str, Any] = {"origin": origin}
    if source_path:
        extra["inbox_from"] = source_path
    res = media.submit_file(Path(tmp), name, opts, extra=extra)
    hub.publish({"type": "notice", "level": "ok",
                 "text": "С телефона: «%s» — распознаю и размечаю голоса" % name})
    return res


def take(src: Path) -> dict[str, Any]:
    """Файл из папки входящих: копия — в обработку, оригинал остаётся человеку."""
    src = Path(src)
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    tmp = UPLOAD_DIR / ("%s_%s" % (uuid.uuid4().hex[:8], src.name))
    shutil.copy2(src, tmp)
    try:
        return accept(tmp, src.name, "inbox", str(src))
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


def _readable(path: Path) -> bool:
    """Открывается ли файл: тот, кого ещё пишут, Windows на чтение не отдаёт."""
    try:
        with open(path, "rb") as fh:
            fh.read(1)
        return True
    except OSError:
        return False


def _recording_now() -> bool:
    from . import recordings

    return recordings.active_id() is not None


# ---------------------------------------------------------------- сторож папок


class Watcher:
    """Опрос папок входящих. Один на программу; проверки заводят свой с малыми сроками."""

    def __init__(self, poll_s: float = POLL_S, stable_s: float = STABLE_S,
                 take_fn: Callable[[Path], dict[str, Any]] = take,
                 busy: Callable[[], bool] = _recording_now,
                 clock: Callable[[], float] = time.time) -> None:
        self.poll_s = float(poll_s)
        self.stable_s = float(stable_s)
        self._take = take_fn
        self._busy = busy
        self._clock = clock
        # путь (в нижнем регистре) → размер, время, с какого момента не меняется
        self._pending: dict[str, tuple[int, float, float]] = {}
        self._seen: dict[str, Any] | None = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last: dict[str, Any] = {}       # последний взятый файл — для настроек

    # ---- память о взятом

    def _load(self) -> dict[str, Any]:
        if self._seen is None:
            data: dict[str, Any] = {}
            try:
                with open(_seen_path(), "r", encoding="utf-8") as fh:
                    data = json.load(fh)
            except (OSError, ValueError):
                data = {}
            if not isinstance(data, dict):
                data = {}
            data.setdefault("folders", {})
            data.setdefault("files", {})
            self._seen = data
        return self._seen

    def _save(self) -> None:
        if self._seen is None:
            return
        try:
            _seen_path().parent.mkdir(parents=True, exist_ok=True)
            config.atomic_json(_seen_path(), self._seen)
        except OSError as err:
            log.warning("память входящих не записалась: %s", err)

    # ---- один обход

    def scan_once(self) -> list[Path]:
        """Обойти папки один раз. Возвращает взятые файлы."""
        with self._lock:
            return self._scan()

    def _scan(self) -> list[Path]:
        if not config.feature("phone"):
            return []
        seen = self._load()
        now = self._clock()
        taken: list[Path] = []
        present: set[str] = set()
        changed = False
        current = folders()
        # Папку убрали из списка — забываем её отправную точку: вернут —
        # начнём заново с того, что в ней будет лежать на тот момент.
        alive = {f["path"].lower() for f in current}
        for fkey in list(seen["folders"]):
            if fkey not in alive:
                del seen["folders"][fkey]
                changed = True
        for f in current:
            if not f["exists"]:
                continue
            folder = Path(f["path"])
            fkey = f["path"].lower()
            try:
                entries = [p for p in folder.iterdir()
                           if p.is_file() and audio_io.is_media(p.name)]
            except OSError as err:
                log.warning("папка входящих не читается: %s (%s)", folder, err)
                continue
            if fkey not in seen["folders"]:
                # Первая встреча с папкой: что в ней уже есть — не входящие.
                for p in entries:
                    try:
                        st = p.stat()
                    except OSError:
                        continue
                    seen["files"][str(p).lower()] = {
                        "size": st.st_size, "mtime": int(st.st_mtime), "at": now, "baseline": True}
                seen["folders"][fkey] = now
                changed = True
                continue
            for p in entries:
                key = str(p).lower()
                present.add(key)
                try:
                    st = p.stat()
                except OSError:
                    continue
                rec = seen["files"].get(key)
                if rec and rec.get("size") == st.st_size and rec.get("mtime") == int(st.st_mtime):
                    continue
                pend = self._pending.get(key)
                if pend is None or pend[0] != st.st_size or pend[1] != st.st_mtime:
                    self._pending[key] = (st.st_size, st.st_mtime, now)
                    continue
                if now - pend[2] < self.stable_s:
                    continue
                if self._busy():
                    continue          # идёт запись — файл подождёт
                if not _readable(p):
                    continue
                entry: dict[str, Any] = {"size": st.st_size, "mtime": int(st.st_mtime), "at": now}
                try:
                    res = self._take(p)
                    entry["rec_id"] = res.get("rec_id")
                    self.last = {"name": p.name, "at": now, "rec_id": res.get("rec_id")}
                    taken.append(p)
                except Exception as err:      # noqa: BLE001 — одна ошибка не останавливает сторожа
                    log.warning("входящий файл не взят: %s (%s)", p, err)
                    entry["error"] = str(err)[:200]
                    hub.publish({"type": "notice", "level": "err",
                                 "text": "С телефона: «%s» не взят — %s" % (p.name, err)})
                seen["files"][key] = entry
                self._pending.pop(key, None)
                changed = True
        for key in list(self._pending):
            if key not in present:
                del self._pending[key]
        # Записи о файлах, которых давно нет, — долой, иначе память растёт вечно.
        for key, rec in list(seen["files"].items()):
            if key not in present and now - float(rec.get("at") or 0) > FORGET_AFTER_S:
                del seen["files"][key]
                changed = True
        if changed:
            self._save()
        return taken

    # ---- поток

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="inbox", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        t = self._thread
        if t is not None and t.is_alive():
            t.join(timeout=self.poll_s + 2.0)
        self._thread = None

    def _loop(self) -> None:
        while not self._stop.wait(self.poll_s):
            try:
                self.scan_once()
            except Exception as err:     # noqa: BLE001 — сторож живёт дальше
                log.warning("обход папок входящих не удался: %s", err)

    def stats(self) -> dict[str, Any]:
        seen = self._load()
        files = seen.get("files") or {}
        taken = sum(1 for r in files.values() if r.get("rec_id"))
        return {"taken": taken, "last": dict(self.last)}


_watcher: Watcher | None = None


def start() -> None:
    """Поднять сторожа папок. Сам решает по настройкам, есть ли ему работа."""
    global _watcher
    if _watcher is None:
        _watcher = Watcher()
    _watcher.start()


def stop() -> None:
    if _watcher is not None:
        _watcher.stop()


def scan_now() -> list[Path]:
    """Обойти папки сейчас, не дожидаясь опроса (кнопка в настройках, проверки)."""
    global _watcher
    if _watcher is None:
        _watcher = Watcher()
    return _watcher.scan_once()


def state() -> dict[str, Any]:
    """Что показать в настройках: папки и что из них взято."""
    out: dict[str, Any] = {"folders": folders(), "enabled": config.feature("phone")}
    out.update((_watcher or Watcher()).stats())
    return out
