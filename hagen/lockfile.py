# -*- coding: utf-8 -*-
"""Опись выпуска: точные версии всего, что программа докачивает при установке.

Архив выпуска несёт только код, Python и модель разметки. Остальное —
библиотеки (torch, yt-dlp и прочие), ffmpeg, модели распознавания — установщик
берёт из их открытых источников. Чтобы у человека оказалось ровно то, на чём
выпуск проверен, а не «последнее на сегодня», всё это закреплено в
`lock.json`:

    {
      "python": {"version": "3.12.10"},
      "packages": [{"name", "version", "url" или "file", "sha256"}, …],
      "extras": {"pyannote": [… пакеты только для релиза с pyannote …]},
      "floor": ["yt-dlp"],
      "ffmpeg": {"version", "url", "sha256"},
      "models": {"istupakov/gigaam-v3-onnx": "<ревизия>", …}
    }

`packages` — всё окружение целиком, со всеми зависимостями зависимостей, а не
только то, что названо в requirements.txt. `file` — пакет, который лежит в
самом выпуске (vendor/): у него нет готовой сборки в открытом каталоге.
`floor` — пакеты, которые человек может обновить сам кнопкой (yt-dlp): их
обновление выпуска не откатывает назад. Опись собирает `tools/make_lock.py`
из окружения, на котором выпуск проверен.

Модуль берёт только стандартную библиотеку Python: им пользуется установщик
до появления окружения и обновление на старте программы.
"""
from __future__ import annotations

import io
import json
import re
import shutil
import urllib.parse
from pathlib import Path
from typing import Any, Callable, Iterable

from . import download

PROJECT_DIR = Path(__file__).resolve().parent.parent
PATH = PROJECT_DIR / "lock.json"

#: Что оставляем в .venv\Scripts: pip кладёт туда лаунчеры .exe, и каждый новый
#: файл — это лишний вопрос антивируса (та же чистка, что в install.py --prune).
KEEP_SCRIPTS = {"python.exe", "pythonw.exe"}


class LockError(RuntimeError):
    """Опись не читается или не сходится с тем, что пришло."""


def read(path: str | Path | None = None) -> dict[str, Any]:
    """Опись целиком. Нет файла или мусор — пустой словарь."""
    try:
        with io.open(path or PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def norm(name: str) -> str:
    """Имя пакета в одном написании: «PyAudioWPatch», «pyaudiowpatch» и
    «pyaudio_wpatch» — один пакет (правило PEP 503)."""
    return re.sub(r"[-_.]+", "-", str(name or "")).lower()


def packages(lock: dict[str, Any], engine: str = "onnx") -> list[dict[str, Any]]:
    """Пакеты выпуска с движком разметки engine: общие и добавочные для движка."""
    base = [p for p in lock.get("packages") or [] if isinstance(p, dict)]
    extra = [p for p in (lock.get("extras") or {}).get(engine) or [] if isinstance(p, dict)]
    return base + extra


def floor(lock: dict[str, Any]) -> set[str]:
    return {norm(n) for n in lock.get("floor") or []}


def model_revision(repo: str, lock: dict[str, Any] | None = None) -> str | None:
    """Ревизия модели Hugging Face из описи. Нет — None (качается последняя)."""
    lock = read() if lock is None else lock
    rev = (lock.get("models") or {}).get(repo)
    return str(rev) if rev else None


def site_packages(root: str | Path | None = None) -> Path:
    return Path(root or PROJECT_DIR) / ".venv" / "Lib" / "site-packages"


def installed(root: str | Path | None = None) -> dict[str, str]:
    """Что стоит в окружении программы: {имя: версия}. Окружения нет — пусто."""
    import importlib.metadata as md

    sp = site_packages(root)
    if not sp.is_dir():
        return {}
    out: dict[str, str] = {}
    for dist in md.distributions(path=[str(sp)]):
        name = dist.metadata.get("Name") if dist.metadata else None
        if name:
            out[norm(name)] = str(dist.version)
    return out


def version_key(text: str) -> tuple[Any, ...]:
    """Ключ для сравнения версий «не старше ли»: числа — числами.

    Полное правило версий (PEP 440) здесь не нужно: сравниваем только версии
    одного пакета из `floor`, а у yt-dlp это даты вида 2026.8.19.
    """
    parts = re.split(r"[.+-]", str(text or ""))
    return tuple((0, int(p)) if p.isdigit() else (1, p) for p in parts)


def to_install(pkgs: Iterable[dict[str, Any]], have: dict[str, str],
               floor_names: set[str] | None = None) -> list[dict[str, Any]]:
    """Какие пакеты описи поставить: нет в окружении или версия другая.

    Пакет из floor ставится, только если стоит версия старше описи: свежий
    yt-dlp, обновлённый человеком, назад не откатываем.
    """
    floor_names = floor_names or set()
    out = []
    for p in pkgs:
        name = norm(p.get("name", ""))
        now = have.get(name)
        want = str(p.get("version") or "")
        if now == want:
            continue
        if now and name in floor_names and version_key(now) > version_key(want):
            continue
        out.append(p)
    return out


def to_remove(old: Iterable[dict[str, Any]], new: Iterable[dict[str, Any]],
              have: dict[str, str]) -> list[str]:
    """Пакеты, которые были в прошлой описи, пропали из новой и стоят сейчас.

    Только они: то, что человек поставил сам (например, pyannote в общую
    папку), обновление не трогает.
    """
    new_names = {norm(p.get("name", "")) for p in new}
    gone = {norm(p.get("name", "")) for p in old} - new_names
    return sorted(gone & set(have))


def _file_name(pkg: dict[str, Any]) -> str:
    src = str(pkg.get("url") or pkg.get("file") or "")
    name = urllib.parse.unquote(src.split("?", 1)[0].split("#", 1)[0].rsplit("/", 1)[-1])
    if not name.endswith(".whl"):
        raise LockError("В описи у пакета %s не готовая сборка (.whl): %s"
                        % (pkg.get("name"), name or "адреса нет"))
    return name


def fetch_packages(pkgs: list[dict[str, Any]], dest: str | Path,
                   root: str | Path | None = None,
                   progress: Callable[[int, int, str], Any] | None = None,
                   cancelled: Callable[[], bool] | None = None) -> list[Path]:
    """Скачать сборки пакетов в dest и сверить суммы. Возвращает пути к файлам.

    progress(номер, всего, имя) — перед каждым пакетом. Пакет из самого
    выпуска («file») берётся из папки программы root.
    """
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    out: list[Path] = []
    for i, pkg in enumerate(pkgs):
        name = _file_name(pkg)
        if progress is not None:
            progress(i, len(pkgs), str(pkg.get("name") or name))
        want = str(pkg.get("sha256") or "")
        target = dest / name
        if pkg.get("file"):
            src = Path(root or PROJECT_DIR) / str(pkg["file"])
            if not src.is_file():
                raise LockError("В папке программы нет %s." % pkg["file"])
            if want and download.sha256_file(src) != want:
                raise LockError("Файл %s в папке программы не совпадает с описью." % pkg["file"])
            if src.resolve() != target.resolve():
                shutil.copy2(src, target)
        else:
            if not want:
                raise LockError("В описи у пакета %s нет контрольной суммы." % pkg.get("name"))
            download.fetch(str(pkg["url"]), target, sha256=want, cancelled=cancelled)
        out.append(target)
    return out


FFMPEG_EXES = ("ffmpeg.exe", "ffprobe.exe")


def unpack_ffmpeg(zip_path: str | Path, target: str | Path) -> None:
    """Достать ffmpeg.exe и ffprobe.exe из архива сборки в target\\bin.

    Сначала во временную папку рядом, потом подменой: недораспакованный
    ffmpeg не должен остаться на месте рабочего.
    """
    import zipfile

    target = Path(target)
    tmp = target.with_name(target.name + ".new")
    shutil.rmtree(tmp, ignore_errors=True)
    (tmp / "bin").mkdir(parents=True)
    with zipfile.ZipFile(zip_path) as zf:
        names = {Path(n).name: n for n in zf.namelist()
                 if n.endswith(tuple("/bin/" + e for e in FFMPEG_EXES))}
        if set(names) != set(FFMPEG_EXES):
            shutil.rmtree(tmp, ignore_errors=True)
            raise LockError("В архиве ffmpeg нет %s." % ", ".join(set(FFMPEG_EXES) - set(names)))
        for exe, member in names.items():
            with zf.open(member) as src, open(tmp / "bin" / exe, "wb") as dst:
                shutil.copyfileobj(src, dst)
    old = target.with_name(target.name + ".old")
    shutil.rmtree(old, ignore_errors=True)
    if target.exists():
        target.rename(old)
    tmp.rename(target)
    shutil.rmtree(old, ignore_errors=True)


def install_args(files: Iterable[str | Path], root: str | Path | None = None) -> list[str]:
    """Аргументы pip: поставить скачанные сборки в окружение программы, без сети.

    --prefix — чтобы пакеты легли в .venv, даже когда pip запущен в процессе
    самой программы: она стартует через python\\python.exe, и без этого pip
    положил бы их в python\\Lib\\site-packages. --no-deps — зависимости уже в
    описи, свои pip не ищет.
    """
    return ["install", "--no-deps", "--no-index", "--no-input", "--disable-pip-version-check",
            "--no-warn-script-location", "--prefix", str(Path(root or PROJECT_DIR) / ".venv"),
            *[str(f) for f in files]]


def uninstall_args(names: Iterable[str]) -> list[str]:
    return ["uninstall", "--yes", "--no-input", "--disable-pip-version-check", *names]


def pip_run(args: list[str]) -> tuple[int, list[str]]:
    """Запустить pip в этом же процессе. Возвращает (код, последние строки вывода).

    Не отдельным процессом: корпоративный антивирус не даёт программе
    порождать фоновые процессы, и «python -m pip» не запустился бы.
    """
    import contextlib

    try:
        from pip._internal.cli.main import main as pip_main
    except Exception as err:
        return 1, ["в окружении нет pip: %s" % err]
    out = io.StringIO()
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = pip_main(list(args))
    except SystemExit as err:            # pip иногда выходит через SystemExit
        code = int(getattr(err, "code", 1) or 0)
    except Exception as err:
        return 1, ["pip упал: %s" % err]
    tail = [ln for ln in out.getvalue().splitlines() if ln.strip()][-5:]
    return int(code or 0), tail


def drop_new_launchers(root: str | Path | None = None) -> list[str]:
    """Убрать лаунчеры .exe, которые pip положил в .venv\\Scripts."""
    scripts = Path(root or PROJECT_DIR) / ".venv" / "Scripts"
    dropped: list[str] = []
    if not scripts.is_dir():
        return dropped
    for item in scripts.iterdir():
        if item.suffix.lower() == ".exe" and item.name.lower() not in KEEP_SCRIPTS:
            try:
                item.unlink()
                dropped.append(item.name)
            except OSError:
                pass
    return dropped
