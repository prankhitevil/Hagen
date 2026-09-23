# -*- coding: utf-8 -*-
"""Обновление программы: «Настройки → О программе», только по кнопке.

Как оно идёт:

1. **Проверить** (кнопка «Проверить обновления»): спросить GitHub, какой
   выпуск последний (github.py). Сама программа в сеть не ходит.
2. **Подготовить**, пока программа работает: скачать архив выпуска — или взять
   архив, который человек скачал сам («Установить из файла…»), — распаковать
   из него код во временную папку и докачать то, что поменялось в описи
   (lock.json): сборки библиотек, ffmpeg. Всё сверяется с контрольными суммами.
   Не скачалось что-то — ничего не тронуто, программа остаётся прежней.
3. **Заменить** — при следующем запуске, до того как загружен любой другой
   модуль программы (run.py зовёт apply_pending первым делом): старые файлы
   откладываются для отката, новые ложатся на место, удалённые из выпуска
   убираются, скачанные библиотеки ставятся без сети, ffmpeg подменяется.
4. **Откат**: если новая версия дважды подряд не поднялась (служба не дошла до
   confirm_started), старый код возвращается. Библиотеки при этом остаются
   новыми — они сосуществуют со старым кодом, а откатить их без сети нельзя.

Не обновляется кнопкой рабочая копия git (папка разработки): её код приходит
через git. Модели распознавания обновление не трогает: если в описи сменилась
ревизия, новая скачается кнопкой «Скачать» в «Настройки → Модели».

Модуль берёт только стандартную библиотеку Python и модули программы, которые
тоже ею обходятся (release, lockfile, download, github): на старте он
работает раньше всего остального.
"""
from __future__ import annotations

import datetime
import hashlib
import io
import json
import os
import re
import shutil
import sys
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

from . import download, jobs, lockfile, release

PROJECT_DIR = Path(__file__).resolve().parent.parent

#: Имя архива выпуска в релизе: «Hagen-v0.9.2-windows.zip».
ASSET_RE = re.compile(r"^Hagen-v?(\d+(?:\.\d+)*)-windows\.zip$", re.IGNORECASE)
#: Список файлов программы с суммами — в архиве выпуска и в папке программы.
FILES = "files.json"
#: Папки, которые никогда не приходят из архива обновления: своё у машины.
NEVER = ("python/", ".venv/", "data/", "logs/", "models/onnx-asr/", "models/hf/",
         "models/gigaam/", "models/onnx/", "ffmpeg/", ".git/")


class UpdateError(RuntimeError):
    """Обновление не готовится или не ставится; текст — для человека."""


# ------------------------------------------------------------------ служебное


def work_dir(root: Path) -> Path:
    return root / "data" / "_update"


def _read_json(path: Path) -> dict[str, Any]:
    try:
        with io.open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_json(path: Path, data: dict[str, Any]) -> None:
    """Целиком или никак — тем же способом, что настройки и карточки записей.

    Своя подмена «tmp → replace» здесь была без `fsync` и без повтора на
    «Отказано в доступе»: после потери питания файл состояния обновления мог
    остаться пустым, а под антивирусом — не записаться вовсе. `config` берёт
    только стандартную библиотеку, так что на раннем старте он не мешает.
    """
    from . import config

    path.parent.mkdir(parents=True, exist_ok=True)
    config.atomic_json(path, data, indent=1)


def _now() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def _safe_rel(name: str) -> str | None:
    """Путь внутри архива без выхода наружу папки программы."""
    p = PurePosixPath(name.replace("\\", "/"))
    if p.is_absolute() or ".." in p.parts or (p.parts and ":" in p.parts[0]):
        return None
    return p.as_posix()


# ------------------------------------------------------------------ состояние


def can_update(root: Path | None = None) -> tuple[bool, str]:
    """Можно ли обновлять эту папку кнопкой, и если нет — почему."""
    root = root or PROJECT_DIR
    if (root / ".git").exists():
        return False, ("Это рабочая копия git: код в ней обновляется через git, а не кнопкой.")
    if not (root / "lock.json").is_file():
        return False, ("В папке нет описи выпуска lock.json — эту установку кнопкой не "
                       "обновить. Скачайте архив нового выпуска и запустите Ustanovka.cmd.")
    if not release.updates_repo(root / "release.json"):
        return False, "В release.json не указано, откуда брать обновления."
    return True, ""


def status(root: Path | None = None) -> dict[str, Any]:
    """Что показать в «О программе»: версия, можно ли обновлять, что готово, чем кончилось."""
    root = root or PROJECT_DIR
    ok, why = can_update(root)
    work = work_dir(root)
    staged = _read_json(work / "staged" / "plan.json") if (work / "staged" / "ready").exists() else {}
    return {
        "version": release.version(root / "release.json"),
        "repo": release.updates_repo(root / "release.json"),
        "can_update": ok,
        "reason": why,
        "staged": {k: staged.get(k) for k in ("to", "notes", "wheels", "ffmpeg", "models",
                                              "write", "delete")} if staged else None,
        "last": _read_json(work / "state.json") or None,
    }


def mark_seen(root: Path | None = None) -> None:
    """Итог прошлого обновления показан человеку — больше не показывать."""
    path = work_dir(root or PROJECT_DIR) / "state.json"
    state = _read_json(path)
    if state and not state.get("seen"):
        state["seen"] = True
        _write_json(path, state)


# ------------------------------------------------------------------ проверка


def check(root: Path | None = None, releases: Any = None) -> dict[str, Any]:
    """Какой выпуск последний и новее ли он. releases — подмена для проверок."""
    from . import github

    root = root or PROJECT_DIR
    ok, why = can_update(root)
    if not ok:
        raise UpdateError(why)
    repo = release.updates_repo(root / "release.json")
    current = release.version(root / "release.json")
    items = (releases or github.releases)(repo)
    best: dict[str, Any] | None = None
    for item in items:
        if item.get("draft"):
            continue
        for asset in item.get("assets") or []:
            m = ASSET_RE.match(asset.get("name") or "")
            if not m:
                continue
            ver = m.group(1)
            if best is None or release.newer(ver, best["version"]):
                best = {"version": ver, "title": item.get("title") or item.get("tag"),
                        "notes": item.get("notes") or "", "page": item.get("page") or "",
                        "published": item.get("published") or "", "asset": asset}
    if best is None:
        return {"current": current, "latest": None, "newer": False}
    return {"current": current, "latest": best["version"],
            "newer": release.newer(best["version"], current), **best}


def download_latest(handle: Any = None, root: Path | None = None,
                    releases: Any = None) -> Path:
    """Скачать архив свежего выпуска. Адрес берём у GitHub сами, а не со страницы."""
    handle = jobs.as_handle(handle)
    root = root or PROJECT_DIR
    info = check(root, releases)
    if not info.get("newer"):
        raise UpdateError("Новее версии %s нет." % info.get("current"))
    asset = info["asset"]
    dest = work_dir(root) / "incoming" / asset["name"]
    size = int(asset.get("size") or 0)

    def progress(done: int, total: int) -> None:
        total = total or size
        if total:
            handle.progress(0.4 * done / total,
                            "Скачиваю %s: %d из %d МБ" % (asset["name"], done >> 20, total >> 20))

    handle.progress(0.0, "Скачиваю выпуск %s…" % info["latest"])
    download.fetch(asset["url"], dest, sha256=asset.get("sha256") or None, size=size or None,
                   progress=progress, cancelled=lambda: handle.cancelled)
    return dest


# ------------------------------------------------------------------ подготовка


def _archive_prefix(zf: zipfile.ZipFile) -> str:
    """Папка программы внутри архива: та, где лежит release.json."""
    cands = [n[: -len("release.json")] for n in zf.namelist()
             if n.endswith("release.json") and n.count("/") <= 1]
    if not cands:
        raise UpdateError("Это не архив выпуска Hagen: в нём нет release.json.")
    return min(cands, key=len)


def _archive_json(zf: zipfile.ZipFile, name: str) -> dict[str, Any]:
    try:
        data = json.loads(zf.read(name).decode("utf-8"))
    except (KeyError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _listing(zf: zipfile.ZipFile, prefix: str) -> dict[str, str]:
    """Файлы программы в архиве с суммами: из files.json, а если его нет
    (например, взяли «Source code» со страницы релиза) — всё, кроме Python."""
    listed = (_archive_json(zf, prefix + FILES).get("files") or {})
    if listed:
        return {k: str(v) for k, v in listed.items()}
    out = {}
    for name in zf.namelist():
        if not name.startswith(prefix) or name.endswith("/"):
            continue
        rel = name[len(prefix):]
        if rel == FILES or rel.startswith(NEVER):
            continue
        out[rel] = hashlib.sha256(zf.read(name)).hexdigest()
    return out


def _local_sha(path: Path) -> str:
    try:
        return download.sha256_file(path)
    except OSError:
        return ""


def prepare(zip_path: str | Path, handle: Any = None, root: Path | None = None) -> dict[str, Any]:
    """Разобрать архив выпуска и докачать всё нужное. Возвращает сводку плана.

    Ничего в папке программы не меняет: всё складывается в data\\_update\\staged,
    а заменяется при следующем запуске (apply_pending).
    """
    handle = jobs.as_handle(handle)
    root = root or PROJECT_DIR
    ok, why = can_update(root)
    if not ok:
        raise UpdateError(why)
    work = work_dir(root)
    staged = work / "staged"
    discard(root)
    current = release.version(root / "release.json")
    try:
        zf = zipfile.ZipFile(zip_path)
    except (OSError, zipfile.BadZipFile) as err:
        raise UpdateError("Файл не открылся как архив: %s" % err) from None
    with zf:
        prefix = _archive_prefix(zf)
        new_info = _archive_json(zf, prefix + "release.json")
        new_lock = _archive_json(zf, prefix + "lock.json")
        target = release.version_of(new_info)
        if not release.parse_version(target):
            raise UpdateError("В архиве нет номера версии.")
        if not release.newer(target, current):
            raise UpdateError("В архиве версия %s — она не новее той, что стоит (%s)."
                              % (target, current))
        if not new_lock:
            raise UpdateError("В архиве нет описи lock.json — этот выпуск ставится только "
                              "заново, через Ustanovka.cmd.")
        want_py = str((new_lock.get("python") or {}).get("version") or "")
        have_py = "%d.%d" % sys.version_info[:2]
        if want_py and ".".join(want_py.split(".")[:2]) != have_py:
            raise UpdateError("Выпуск %s сделан под Python %s, а здесь %s: его нужно поставить "
                              "заново — скачайте архив и запустите Ustanovka.cmd."
                              % (target, want_py, have_py))
        listing = _listing(zf, prefix)
        handle.progress(0.45, "Распаковываю код выпуска %s…" % target)
        write: list[str] = []
        for rel, sha in listing.items():
            safe = _safe_rel(rel)
            if safe is None or safe.startswith(NEVER):
                continue
            dst = staged / "files" / safe
            dst.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(prefix + rel) as src, open(dst, "wb") as out:
                shutil.copyfileobj(src, out)
            if _local_sha(root / safe) != sha:
                write.append(safe)
        with io.open(staged / "files" / FILES, "w", encoding="utf-8", newline="\n") as fh:
            json.dump({"version": target, "files": listing}, fh, ensure_ascii=False, indent=1)
    old_listing = _read_json(root / FILES).get("files") or {}
    delete = sorted(rel for rel in old_listing
                    if rel not in listing and _safe_rel(rel) and not rel.startswith(NEVER)
                    and (root / rel).is_file())

    old_lock = lockfile.read(root / "lock.json")
    engine = release.diarize_engine(root / "release.json")
    have = lockfile.installed(root)
    need = lockfile.to_install(lockfile.packages(new_lock, engine), have, lockfile.floor(new_lock))
    remove = lockfile.to_remove(lockfile.packages(old_lock, engine),
                                lockfile.packages(new_lock, engine), have)
    wheels: list[str] = []
    if need:
        def step(i: int, total: int, name: str) -> None:
            handle.progress(0.5 + 0.35 * i / max(1, total),
                            "Библиотеки по описи: %s (%d из %d)" % (name, i + 1, total))

        files = lockfile.fetch_packages(need, staged / "wheels", staged / "files",
                                        progress=step, cancelled=lambda: handle.cancelled)
        wheels = [p.name for p in files]
    ffmpeg = new_lock.get("ffmpeg") or {}
    ffmpeg_new = bool(ffmpeg.get("url")) and ffmpeg.get("version") != (old_lock.get("ffmpeg") or {}).get("version")
    if ffmpeg_new:
        handle.progress(0.87, "Скачиваю ffmpeg %s…" % ffmpeg.get("version"))
        download.fetch(ffmpeg["url"], staged / "ffmpeg.zip", sha256=ffmpeg.get("sha256"),
                       size=ffmpeg.get("size"), cancelled=lambda: handle.cancelled)
    models = sorted(repo for repo, rev in (new_lock.get("models") or {}).items()
                    if (old_lock.get("models") or {}).get(repo) not in (None, rev))
    plan = {"from": current, "to": target, "write": write, "delete": delete,
            "wheels": wheels, "remove": remove, "ffmpeg": ffmpeg_new, "models": models,
            "notes": "", "prepared": _now()}
    _write_json(staged / "plan.json", plan)
    (staged / "ready").write_text(target, encoding="utf-8")
    handle.progress(1.0, "Обновление до %s готово: закройте программу и откройте снова." % target)
    return plan


def discard(root: Path | None = None) -> None:
    """Убрать подготовленное обновление: передумали или готовим другое."""
    shutil.rmtree(work_dir(root or PROJECT_DIR) / "staged", ignore_errors=True)


# ------------------------------------------------------------------ замена


def pending(root: Path | None = None) -> bool:
    """Есть ли что делать на старте: готовое обновление или непроверенная новая версия."""
    work = work_dir(root or PROJECT_DIR)
    if (work / "staged" / "ready").exists():
        return True
    state = _read_json(work / "state.json")
    return state.get("phase") == "applied" and not state.get("confirmed")


def apply_pending(root: Path | None = None) -> dict[str, Any] | None:
    """Поставить подготовленное обновление или откатить неудачное. Зовёт run.py
    в самом начале запуска, пока не загружен ни один другой модуль программы.

    Возвращает итог (для журнала) или None, если делать было нечего.
    """
    root = root or PROJECT_DIR
    work = work_dir(root)
    if (work / "staged" / "ready").exists():
        return _apply(root, work)
    state = _read_json(work / "state.json")
    if state.get("phase") == "applied" and not state.get("confirmed"):
        state["starts"] = int(state.get("starts") or 0) + 1
        if state["starts"] >= 2:
            return _rollback(root, work, state)
        _write_json(work / "state.json", state)
    return None


def _apply(root: Path, work: Path) -> dict[str, Any]:
    staged = work / "staged"
    plan = _read_json(staged / "plan.json")
    backup = work / "backup"
    shutil.rmtree(backup, ignore_errors=True)
    (backup / "files").mkdir(parents=True)
    created: list[str] = []
    state: dict[str, Any] = {"from": plan.get("from"), "to": plan.get("to"), "at": _now(),
                             "models": plan.get("models") or []}
    try:
        for rel in list(plan.get("write") or []) + [FILES]:
            src, dst = staged / "files" / rel, root / rel
            if not src.is_file():
                continue
            if dst.exists():
                (backup / "files" / rel).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(dst, backup / "files" / rel)
            else:
                created.append(rel)
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
        for rel in plan.get("delete") or []:
            path = root / rel
            if path.is_file():
                (backup / "files" / rel).parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(path), str(backup / "files" / rel))
        state["created"] = created
        if plan.get("remove"):
            lockfile.pip_run(lockfile.uninstall_args(plan["remove"]))
        wheels = [staged / "wheels" / n for n in plan.get("wheels") or []]
        if wheels:
            code, tail = lockfile.pip_run(lockfile.install_args(wheels, root))
            if code != 0:
                raise UpdateError("библиотеки не встали: %s" % " | ".join(tail[-3:]))
            lockfile.drop_new_launchers(root)
        if plan.get("ffmpeg") and (staged / "ffmpeg.zip").exists():
            if (root / "ffmpeg").exists():
                shutil.copytree(root / "ffmpeg", backup / "ffmpeg")
            lockfile.unpack_ffmpeg(staged / "ffmpeg.zip", root / "ffmpeg")
    except Exception as err:
        state.update(phase="failed", error=str(err), created=created)
        _restore(root, backup, created)
        _write_json(work / "state.json", state)
        shutil.rmtree(staged, ignore_errors=True)
        return state
    state.update(phase="applied", starts=0, confirmed=False)
    _write_json(work / "state.json", state)
    shutil.rmtree(staged, ignore_errors=True)
    return state


def _restore(root: Path, backup: Path, created: list[str]) -> None:
    """Вернуть отложенные файлы на место и убрать то, чего раньше не было."""
    saved = backup / "files"
    if saved.is_dir():
        for cur, _dirs, names in os.walk(saved):
            for name in names:
                src = Path(cur) / name
                dst = root / src.relative_to(saved)
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
    for rel in created:
        (root / rel).unlink(missing_ok=True)
    if (backup / "ffmpeg").is_dir():
        shutil.rmtree(root / "ffmpeg", ignore_errors=True)
        shutil.copytree(backup / "ffmpeg", root / "ffmpeg")


def _rollback(root: Path, work: Path, state: dict[str, Any]) -> dict[str, Any]:
    _restore(root, work / "backup", list(state.get("created") or []))
    state.update(phase="rolled_back", at=_now(), seen=False)
    _write_json(work / "state.json", state)
    return state


def confirm_started(root: Path | None = None) -> None:
    """Служба поднялась — новая версия работает, откатывать не надо."""
    path = work_dir(root or PROJECT_DIR) / "state.json"
    state = _read_json(path)
    if state.get("phase") == "applied" and not state.get("confirmed"):
        state.update(confirmed=True, seen=False)
        _write_json(path, state)
