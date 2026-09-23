# -*- coding: utf-8 -*-
"""Снимки экрана со встречи — в заметку, в тот момент разговора, когда сделаны.

Решения 13.09.2026:
* сами картинки модели НЕ показываются: модель получает время и имя файла и
  ставит ссылку в нужное место документа;
* снимки лежат в сейфе Obsidian рядом с заметкой — видны на всех устройствах;
  это 0,2–1 МБ на снимок, а не гигабайты видео; удаляются вместе с заметкой;
* снимки берутся ТОЛЬКО из папки «Снимки экрана» (Win+Shift+S в Windows 11
  сохраняет туда сам): файлы, появившиеся между «Старт» и «Стоп». Папок может
  быть несколько, список задаётся в настройках; пустой — ищем сами.

Без перехвата клавиатуры: ровно на такой перехват Kaspersky и ругается.

Буфер обмена — запасной режим, по умолчанию выключен (screenshots_from_clipboard):
раз в секунду сравниваем номер содержимого буфера (GetClipboardSequenceNumber)
и берём картинку, только если он сменился. Если включить, один и тот же снимок
придёт и в буфер, и в папку — повтор узнаётся по уменьшенной копии.
"""
from __future__ import annotations

import hashlib
import io
import logging
import os
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from ... import config, store

log = logging.getLogger("hagen.shots")

IMG_EXT = {".png", ".jpg", ".jpeg", ".bmp", ".webp"}
#: известная папка Windows «Снимки экрана»
_SCREENSHOTS_KNOWN_FOLDER = "{B7BEDE81-DF94-4682-A7D8-57A52620B86F}"
_SHELL_FOLDERS = r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders"


# ---------------------------------------------------------------- где искать и куда класть


def _shell_folder(name: str) -> Path | None:
    """Папка пользователя по записи в реестре — там, куда её перенесли."""
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _SHELL_FOLDERS) as key:
            raw = str(winreg.QueryValueEx(key, name)[0] or "").strip()
    except OSError:
        return None
    return Path(os.path.expandvars(raw)) if raw else None


def screenshot_folders(auto: bool = False) -> list[Path]:
    """Папки, куда снимки экрана сохраняются сами. Настройка сильнее догадки.

    `auto` — только догадка, без настройки: её показывают в настройках, пока
    список папок пуст. Возвращаются только существующие папки.
    """
    manual = [] if auto else (config.get("screenshot_folders") or [])
    cands: list[Path] = [Path(os.path.expandvars(str(p).strip())) for p in manual
                         if str(p).strip()]
    if not cands:
        # «Изображения» бывают перенесены в OneDrive, а своей записи у «Снимков
        # экрана» в реестре может не быть вовсе. Тогда Win+Shift+S сохраняет в
        # «Изображения\Screenshots» на новом месте, а не в домашней папке.
        shots = _shell_folder(_SCREENSHOTS_KNOWN_FOLDER)
        if shots is not None:
            cands.append(shots)
        pictures = _shell_folder("My Pictures")
        if pictures is not None:
            cands += [pictures / "Screenshots", pictures / "Снимки экрана"]
        home = Path.home()
        cands += [home / "Pictures" / "Screenshots", home / "Pictures" / "Снимки экрана",
                  home / "Yandex.Disk" / "Скриншоты"]
    out: list[Path] = []
    for p in cands:
        try:
            if p.is_dir() and p not in out:
                out.append(p)
        except OSError:
            continue
    return out


def _fingerprint(img: Any) -> str:
    small = img.convert("L").resize((48, 48))
    return hashlib.sha1(small.tobytes()).hexdigest()


# ---------------------------------------------------------------- буфер обмена


def _clipboard_seq() -> int:
    import ctypes

    return int(ctypes.windll.user32.GetClipboardSequenceNumber())


def _grab_clipboard() -> Any:
    from PIL import ImageGrab

    return ImageGrab.grabclipboard()


# ---------------------------------------------------------------- наблюдатель


class ScreenshotWatcher:
    """Следит за папками снимков (и, если включено, за буфером), пока идёт запись."""

    POLL_S = 1.0
    FOLDER_EVERY = 1          # папки — каждую секунду: другого источника по умолчанию нет

    def __init__(self, rec_id: str, position_s: Callable[[], float],
                 on_shot: Callable[[dict[str, Any]], None] | None = None,
                 folder: Callable[[], Path] | None = None) -> None:
        self.rec_id = rec_id
        self.position_s = position_s
        self.on_shot = on_shot
        # Куда класть снимок, решает логика заметок: папка спрашивается на
        # каждый снимок, категорию записи могут поменять по ходу.
        self.folder = folder
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._prints: set[str] = set()
        self._seen: set[str] = set()
        self._pending: dict[str, int] = {}
        self._folders: list[Path] = []
        self._t0 = 0.0
        self._seq = 0
        self.count = 0

    def start(self) -> None:
        self._t0 = time.time()
        try:
            self._seq = _clipboard_seq()     # то, что лежало в буфере до записи, не берём
        except Exception:
            self._seq = 0
        self._folders = screenshot_folders()
        for folder in self._folders:         # и старые файлы в папках тоже
            try:
                self._seen.update(str(p) for p in folder.iterdir())
            except OSError:
                pass
        for s in (store.get(self.rec_id) or {}).get("screenshots") or []:
            if s.get("print"):
                self._prints.add(str(s["print"]))
        self._thread = threading.Thread(target=self._run, name="shots-%s" % self.rec_id[-4:],
                                        daemon=True)
        self._thread.start()
        log.info("запись %s: слежу за снимками экрана (буфер и папки: %s)", self.rec_id,
                 ", ".join(str(f) for f in self._folders) or "папок нет")

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=5)
        # последний взгляд: снимок, сделанный за секунду до «Стоп», не теряем.
        # Дважды: новый файл берётся, когда его размер перестал меняться.
        try:
            self._tick(folders=True)
            if self._pending:
                time.sleep(0.7)
                self._tick(folders=True)
        except Exception:
            pass

    def _run(self) -> None:
        n = 0
        while not self._stop.wait(self.POLL_S):
            n += 1
            try:
                self._tick(folders=(n % self.FOLDER_EVERY == 0))
            except Exception:
                log.debug("проверка снимков экрана не удалась", exc_info=True)

    def _tick(self, folders: bool) -> None:
        # Решение 13.09: снимком считается только файл в папке снимков
        # экрана (Win+Shift+S сохраняет туда сам). Картинка, скопированная в
        # буфер из браузера, снимком не является. Буфер — только по настройке.
        if not config.get("screenshots_from_clipboard"):
            if folders:
                self._scan_folders()
            return
        seq = _clipboard_seq()
        if seq != self._seq:
            self._seq = seq
            try:
                img = _grab_clipboard()
            except Exception:
                img = None               # буфер занят другой программой — возьмём в следующий раз
                self._seq = 0
            from PIL import Image

            if isinstance(img, Image.Image):
                self._add(img, "clipboard")
        if folders:
            self._scan_folders()

    def _scan_folders(self) -> None:
        from PIL import Image

        for folder in self._folders:
            try:
                files = list(folder.iterdir())
            except OSError:
                continue
            for p in files:
                key = str(p)
                if key in self._seen or p.suffix.lower() not in IMG_EXT:
                    continue
                try:
                    st = p.stat()
                except OSError:
                    continue
                if st.st_mtime < self._t0 - 2:
                    self._seen.add(key)
                    continue
                # файл может ещё дописываться: берём, когда размер не менялся
                if self._pending.get(key) != st.st_size:
                    self._pending[key] = st.st_size
                    continue
                self._pending.pop(key, None)
                self._seen.add(key)
                try:
                    with Image.open(p) as im:
                        im.load()
                        self._add(im.copy(), "folder", src=p)
                except Exception as err:
                    log.debug("снимок %s не открылся: %s", p, err)

    def _add(self, img: Any, source: str, src: Path | None = None) -> dict[str, Any] | None:
        fp = _fingerprint(img)
        if fp in self._prints:
            return None
        self._prints.add(fp)
        meta = store.get(self.rec_id)
        if not meta:
            return None
        try:
            at_s = max(0.0, float(self.position_s()))
        except Exception:
            at_s = 0.0
        now = datetime.now()
        if self.folder is None:
            log.warning("запись %s: некуда класть снимок — папка не задана", self.rec_id)
            return None
        folder = Path(self.folder())
        folder.mkdir(parents=True, exist_ok=True)
        stem = "%s %s" % (now.strftime("%Y-%m-%d %H-%M-%S"), self.rec_id[-4:])
        path = folder / (stem + ".png")
        n = 2
        while path.exists():
            path = folder / ("%s (%d).png" % (stem, n))
            n += 1
        if img.mode not in ("RGB", "RGBA"):
            img = img.convert("RGB")
        buf = io.BytesIO()
        img.save(buf, format="PNG", optimize=True)
        path.write_bytes(buf.getvalue())
        entry = {"file": path.name, "path": str(path), "at_s": round(at_s, 1),
                 "source": source, "taken_at": now.isoformat(timespec="seconds"),
                 "w": int(img.width), "h": int(img.height), "print": fp,
                 "bytes": len(buf.getvalue())}
        if src is not None:
            entry["from"] = str(src)
        fresh = store.get(self.rec_id) or {}
        shots = list(fresh.get("screenshots") or [])
        shots.append(entry)
        store.update(self.rec_id, {"screenshots": shots})
        self.count += 1
        log.info("запись %s: снимок экрана %s на %.0f c (%s, %d КБ)", self.rec_id, path.name,
                 at_s, source, len(buf.getvalue()) // 1024)
        if self.on_shot is not None:
            try:
                self.on_shot(entry)
            except Exception:
                log.debug("слушатель снимков упал", exc_info=True)
        return entry


# ---------------------------------------------------------------- удаление и склейка


def delete_shots(rec_id: str, folder: Path) -> int:
    """Убрать файлы снимков записи. Только свои: из названной папки, внутри сейфа, картинки.

    Папку называет логика заметок — та же, куда снимки складывались. Без этой
    проверки удаление записи могло бы стереть чужую картинку, лежащую в сейфе
    рядом с заметками: путь снимка берётся из карточки записи.
    """
    meta = store.get(rec_id) or {}
    root = config.vault_root().resolve()
    try:
        own = Path(folder).resolve()
    except OSError:
        own = Path(folder)
    removed = 0
    for s in meta.get("screenshots") or []:
        p = Path(str(s.get("path") or ""))
        try:
            rp = p.resolve()
            if not rp.is_relative_to(root) or rp.parent != own \
                    or p.suffix.lower() not in IMG_EXT:
                log.warning("отказ удалять снимок не из папки снимков записи: %s", p)
                continue
            if p.exists():
                p.unlink()
                removed += 1
        except (OSError, ValueError) as err:
            log.warning("снимок не удалён (%s): %s", p, err)
    if removed:
        log.info("запись %s: удалено снимков экрана — %d", rec_id, removed)
    return removed


def shifted(shots: list[dict[str, Any]], offset_s: float) -> list[dict[str, Any]]:
    return [dict(s, at_s=round(float(s.get("at_s") or 0) + offset_s, 1)) for s in shots or []]
