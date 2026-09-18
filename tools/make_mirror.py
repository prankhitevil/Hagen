# -*- coding: utf-8 -*-
"""Собрать зеркало для публикации: снимок кода без внутренней истории.

    .venv\\Scripts\\python.exe tools\\make_mirror.py                 — собрать и проверить
    .venv\\Scripts\\python.exe tools\\make_mirror.py --commit --tag v1.0
    .venv\\Scripts\\python.exe tools\\make_mirror.py --commit --push

Как устроено. Частный репозиторий — мастерская: вся история, ветки, черновики,
`_внутреннее/`. Зеркало — витрина: отдельный репозиторий со своей короткой
историей, по коммиту на выпуск. Скрипт берёт состояние выбранного коммита
мастерской (`git archive`, то есть ровно то, что под контролем git), выкидывает
исключённое и кладёт результат в папку зеркала одним новым коммитом. Коммиты
мастерской в зеркало не переносятся вовсе — поэтому ни почта из них, ни
сообщения, ни черновые ветки наружу попасть не могут.

Перед коммитом — проверка. Скрипт отказывается собирать, если нашёл:
  * исключённое (папку `_внутреннее/`, правила `CLAUDE*.md`, настройки с
    ключами);
  * похожее на ключи сервисов;
  * личное: имена, пути, названия — по списку из `tools/mirror_check.local.txt`.

Список личного лежит в отдельном файле, закрытом от git, а не здесь: этот
скрипт сам уезжает в зеркало, и список выдал бы ровно то, что должен прятать.
Нет списка — нет зеркала: без него проверка была бы видимостью.

Автор коммитов зеркала — ник и служебный адрес GitHub `noreply`: настоящая почта
в витрину не попадает. Настройка задаётся только папке зеркала.
"""
from __future__ import annotations

import argparse
import io
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from datetime import date
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
CHECK_LIST = PROJECT / "tools" / "mirror_check.local.txt"

AUTHOR_NAME = "prankhitevil"
AUTHOR_EMAIL = "165505819+prankhitevil@users.noreply.github.com"
REMOTE = "https://github.com/prankhitevil/Hagen.git"

#: Что не едет наружу никогда. Папкой, а не списком файлов: новый внутренний
#: документ, положенный в `_внутреннее/`, исключится сам.
EXCLUDE_DIRS = ("_внутреннее/",)
EXCLUDE_FILES = ("CLAUDE.md", "CLAUDE.local.md", "settings.json", "hf_token.txt")

#: Похожее на ключи сервисов. Это не личное, поэтому живёт здесь.
SECRETS = (
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}"),
    re.compile(r"sk-[A-Za-z0-9]{32,}"),
    re.compile(r"hf_[A-Za-z0-9]{30,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"ghp_[A-Za-z0-9]{30,}"),
)

TEXT_EXT = {".py", ".js", ".html", ".css", ".md", ".txt", ".json", ".yml", ".yaml",
            ".cmd", ".toml", ".cfg", ".ini", ".svg", ".gitignore", ""}


def say(msg: str = "") -> None:
    try:
        print(msg, flush=True)
    except UnicodeEncodeError:
        print(msg.encode("ascii", "replace").decode("ascii"), flush=True)


def git(*args: str, cwd: Path = PROJECT, env: dict | None = None) -> str:
    out = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True,
                         encoding="utf-8", errors="replace", env=env)
    if out.returncode != 0:
        raise RuntimeError("git %s: %s" % (" ".join(args), (out.stderr or out.stdout).strip()))
    return out.stdout


def excluded(rel: str) -> bool:
    return rel.startswith(EXCLUDE_DIRS) or rel in EXCLUDE_FILES


def load_check_list() -> tuple[list[re.Pattern], list[tuple[str, str]]]:
    """Список личного: по выражению на строку, `= путь | кусок` — разрешённое исключение."""
    if not CHECK_LIST.exists():
        raise SystemExit("Нет %s — без списка личного зеркало не собирается.\n"
                         "По слову или выражению на строку; исключение — "
                         "«= путь/к/файлу | разрешённый кусок»." % CHECK_LIST.name)
    words: list[re.Pattern] = []
    allowed: list[tuple[str, str]] = []
    for raw in CHECK_LIST.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("="):
            path, _, piece = line[1:].partition("|")
            allowed.append((path.strip(), piece.strip()))
        else:
            words.append(re.compile(line, re.IGNORECASE))
    if not words:
        raise SystemExit("Список личного пуст — проверять нечем.")
    return words, allowed


def export(ref: str, dst: Path) -> list[str]:
    """Содержимое коммита мастерской без исключённого. Возвращает пути."""
    raw = subprocess.run(["git", "archive", "--format=tar", ref], cwd=str(PROJECT),
                         capture_output=True)
    if raw.returncode != 0:
        raise RuntimeError(raw.stderr.decode("utf-8", "replace"))
    kept: list[str] = []
    with tarfile.open(fileobj=io.BytesIO(raw.stdout)) as tar:
        for m in tar.getmembers():
            if not m.isfile() or excluded(m.name):
                continue
            target = dst / m.name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(tar.extractfile(m).read())
            kept.append(m.name)
    return kept


def check(root: Path, files: list[str]) -> list[str]:
    """Всё подозрительное: «путь:строка — что нашлось». Пусто — можно публиковать."""
    words, allowed = load_check_list()
    bad: list[str] = []
    for rel in files:
        if excluded(rel):
            bad.append("%s — исключённый файл попал в снимок" % rel)
            continue
        path = root / rel
        if path.suffix.lower() not in TEXT_EXT:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for no, line in enumerate(text.splitlines(), 1):
            for pat in SECRETS:
                if pat.search(line):
                    bad.append("%s:%d — похоже на ключ сервиса" % (rel, no))
            for pat in words:
                m = pat.search(line)
                if not m:
                    continue
                if any(rel == a_path and a_piece in line for a_path, a_piece in allowed):
                    continue
                bad.append("%s:%d — «%s»" % (rel, no, m.group(0)))
    return bad


def sync(src: Path, mirror: Path) -> None:
    """Папка зеркала становится точной копией снимка; .git не трогаем."""
    for item in mirror.iterdir():
        if item.name == ".git":
            continue
        if item.is_dir():
            shutil.rmtree(item)
        else:
            item.unlink()
    for item in src.iterdir():
        if item.is_dir():
            shutil.copytree(item, mirror / item.name)
        else:
            shutil.copy2(item, mirror / item.name)


def main() -> int:
    ap = argparse.ArgumentParser(description="Собрать зеркало Hagen для публикации")
    ap.add_argument("--ref", default="main", help="какой коммит мастерской публиковать")
    ap.add_argument("--out", default=str(PROJECT.parent / "hagen-mirror"),
                    help="папка зеркала (свой репозиторий git)")
    ap.add_argument("--commit", action="store_true", help="записать снимок коммитом в зеркало")
    ap.add_argument("--message", default="", help="сообщение коммита зеркала")
    ap.add_argument("--tag", default="", help="метка выпуска, например v1.0")
    ap.add_argument("--push", action="store_true", help="отправить зеркало на GitHub")
    args = ap.parse_args()

    ref_hash = git("rev-parse", "--short", args.ref).strip()
    say("Снимок мастерской: %s (%s)" % (args.ref, ref_hash))

    with tempfile.TemporaryDirectory(prefix="hagen-mirror-") as tmp:
        stage = Path(tmp)
        files = export(args.ref, stage)
        size = sum((stage / f).stat().st_size for f in files)
        say("Файлов: %d, %.1f МБ" % (len(files), size / 1048576))

        bad = check(stage, files)
        if bad:
            say("")
            say("ЗЕРКАЛО НЕ СОБРАНО: нашлось то, чему наружу нельзя.")
            for b in bad[:60]:
                say("   " + b)
            if len(bad) > 60:
                say("   … и ещё %d" % (len(bad) - 60))
            return 1
        say("Проверка на личное и ключи: чисто.")

        if not args.commit:
            say("Коммит не делался: добавьте --commit.")
            return 0

        mirror = Path(args.out)
        mirror.mkdir(parents=True, exist_ok=True)
        if not (mirror / ".git").exists():
            git("init", "-b", "main", cwd=mirror)
            say("Заведён репозиторий зеркала: %s" % mirror)
        # Автор и коммитер — ник и служебный адрес GitHub. Только для этой папки.
        git("config", "user.name", AUTHOR_NAME, cwd=mirror)
        git("config", "user.email", AUTHOR_EMAIL, cwd=mirror)
        sync(stage, mirror)

    # В зеркале нужен именно полный снимок, с удалениями: add -A здесь уместен,
    # в отличие от мастерской, где рядом может работать другая сессия.
    git("add", "-A", cwd=mirror)
    if not git("status", "--porcelain", cwd=mirror).strip():
        say("Изменений против прошлого выпуска нет — коммит не нужен.")
    else:
        msg = args.message or ("Hagen %s" % (args.tag or date.today().strftime("%d.%m.%Y")))
        git("commit", "-q", "-m", msg, cwd=mirror)
        say("Коммит зеркала: %s — %s" % (git("rev-parse", "--short", "HEAD", cwd=mirror).strip(), msg))
    if args.tag:
        git("tag", "-a", args.tag, "-m", "Hagen %s" % args.tag, cwd=mirror)
        say("Метка: %s" % args.tag)
    say("Автор: %s" % git("log", "-1", "--format=%an <%ae>", cwd=mirror).strip())

    if args.push:
        remotes = git("remote", cwd=mirror).split()
        if "origin" not in remotes:
            git("remote", "add", "origin", REMOTE, cwd=mirror)
        git("push", "-u", "origin", "main", "--tags", cwd=mirror)
        say("Отправлено: %s" % REMOTE)
    return 0


if __name__ == "__main__":
    sys.exit(main())
