# -*- coding: utf-8 -*-
r"""Установка «Hagen» на компьютер с Windows.

Проще всего — двойной щелчок по «Ustanovka.cmd»: он найдёт Python (или скачает
его в папку программы) и запустит этот файл.

Два случая, установщик различает их сам:

ПЕРЕНЕСЁННАЯ ПАПКА (архив с другого компьютера: внутри уже есть python\, .venv и
модели). Ничего не скачивается: исправляются пути к Python, создаются ярлыки,
спрашивается папка хранилища Obsidian, печатаются файлы для антивируса.

ЧИСТАЯ УСТАНОВКА (архив выпуска или исходники). Нужен интернет. Всё, что
докачивается, — ровно тех версий, что в описи выпуска lock.json, и каждый файл
сверяется с контрольной суммой (см. hagen/lockfile.py):
    1. проверяет Python, ставит ffmpeg и проверяет место на диске;
    2. создаёт окружение .venv и ставит библиотеки по описи (torch программе
       не нужен; он есть только у релиза с разметкой через pyannote);
    3. кладёт копию Python внутрь папки (узкое правило антивируса, переносимость);
    4. скачивает модель распознавания — по умолчанию одну точную со сжатыми
       весами, около 230 МБ;
       остальные выбираются потом в «Настройки → Модели»;
    5. спрашивает папку хранилища Obsidian — окном выбора, как в Проводнике
       (и токен HuggingFace — только в релизе с разметкой через pyannote, см.
       release.json);
    6. создаёт ярлык «Hagen» на рабочем столе и в меню «Пуск»;
    7. печатает файлы, которые нужно разрешить в антивирусе.

Флаги: --prune (убрать из библиотек тесты, заголовки C++ и кэш — ~56 тыс. файлов
→ ~20 тыс.), --no-shortcuts, --skip-models, --yes (без вопросов).

Ничего в системе, кроме папки программы и ярлыков, не трогается.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

PROJECT = Path(__file__).resolve().parent
VENV = PROJECT / ".venv"
VENV_PY = VENV / "Scripts" / "python.exe"

# Модули программы, которые берут только стандартную библиотеку Python, —
# установщику они доступны и до появления окружения с библиотеками.
sys.path.insert(0, str(PROJECT))


def diarize_engine() -> str:
    """Движок разметки этого релиза из release.json: "onnx" или "pyannote"."""
    from hagen import release

    return release.diarize_engine(PROJECT / "release.json")


def lock() -> dict:
    """Опись выпуска: точные версии библиотек, ffmpeg и моделей (hagen/lockfile.py)."""
    from hagen import lockfile

    return lockfile.read(PROJECT / "lock.json")


NO_WINDOW = 0x08000000 if os.name == "nt" else 0

OK = "  [готово]"
BAD = "  [ошибка]"


def say(msg: str = "") -> None:
    try:
        print(msg, flush=True)
    except Exception:
        print(str(msg).encode("ascii", "replace").decode("ascii"), flush=True)


def head(msg: str) -> None:
    say("")
    say("=" * 66)
    say("  " + msg)
    say("=" * 66)


def run(cmd: list[str], title: str, timeout: int = 3600) -> bool:
    say("  %s…" % title)
    try:
        res = subprocess.run(cmd, timeout=timeout, cwd=str(PROJECT))
    except Exception as err:
        say("%s %s: %s" % (BAD, title, err))
        return False
    if res.returncode != 0:
        say("%s %s: код %s" % (BAD, title, res.returncode))
        return False
    say("%s %s" % (OK, title))
    return True


def ask(question: str, default: str = "") -> str:
    suffix = (" [%s]" % default) if default else ""
    try:
        got = input("  %s%s: " % (question, suffix)).strip()
    except EOFError:
        got = ""
    return got or default


def pick_folder(title: str, start: str) -> str | None:
    """Папка окном, как в Проводнике, — тем же, что у кнопки «Выбрать…» в
    настройках программы. None — окно закрыли или оно не открылось.

    Остальную программу установщик импортировать не может (см. diarize_engine),
    а окно выбора — может: его модули обходятся стандартной библиотекой Python.
    """
    try:
        from hagen import platform

        return platform.shell().pick_folder(title, start)
    except Exception as err:
        say("       Окно выбора папки не открылось: %s" % err)
        return None


# ---------------------------------------------------------------- проверки


def check_python() -> bool:
    """Python той же версии, что в описи: сборки библиотек сделаны под неё."""
    v = sys.version_info
    say("  Python: %d.%d.%d — %s" % (v.major, v.minor, v.micro, sys.executable))
    if os.name != "nt":
        say("%s приложение рассчитано на Windows" % BAD)
        return False
    want = str((lock().get("python") or {}).get("version") or "3.12")
    if "%d.%d" % (v.major, v.minor) != ".".join(want.split(".")[:2]):
        say("%s нужен Python %s: библиотеки в описи выпуска собраны под него." % (BAD, want))
        say("       Запустите Ustanovka.cmd — он возьмёт нужный Python сам.")
        return False
    say("%s версия подходит" % OK)
    return True


FFMPEG_DIR = PROJECT / "ffmpeg"


def _ffmpeg_in_project() -> Path | None:
    for folder in (FFMPEG_DIR / "bin", FFMPEG_DIR):
        cand = folder / "ffmpeg.exe"
        if cand.exists():
            return cand
    return None


def _ffmpeg_version(exe: Path | str) -> str:
    try:
        res = subprocess.run([str(exe), "-version"], capture_output=True, text=True,
                            encoding="utf-8", errors="replace", timeout=60,
                            creationflags=NO_WINDOW)
        first = (res.stdout or "").splitlines()[:1]
        return first[0] if first else str(exe)
    except Exception:
        return str(exe)


def _download_ffmpeg() -> bool:
    """Скачать сборку ffmpeg из описи выпуска и распаковать в папку программы."""
    from hagen import download, lockfile

    entry = lock().get("ffmpeg") or {}
    if not entry.get("url"):
        say("%s в описи выпуска нет ffmpeg" % BAD)
        return False
    say("  скачиваю ffmpeg %s (около %d МБ)…"
        % (entry.get("version", "?"), int(entry.get("size") or 0) // 1048576))
    tmp_zip = PROJECT / "_ffmpeg.zip"
    try:
        download.fetch(entry["url"], tmp_zip, sha256=entry.get("sha256"), size=entry.get("size"))
        lockfile.unpack_ffmpeg(tmp_zip, FFMPEG_DIR)
    except Exception as err:
        say("%s ffmpeg поставить не вышло: %s" % (BAD, err))
        return False
    finally:
        tmp_zip.unlink(missing_ok=True)
    say("%s ffmpeg %s в папке программы" % (OK, entry.get("version", "")))
    return True


def check_ffmpeg() -> bool:
    """Свой ffmpeg в папке программы — той версии, что в описи выпуска.

    Нет своего — скачиваем сборку из описи, с проверкой суммы. Не скачался —
    берём ffmpeg из системы, если он есть: работать будет, хоть и не той версии,
    на которой выпуск проверен.
    """
    mine = _ffmpeg_in_project()
    if mine:
        say("%s ffmpeg свой, в папке программы: %s" % (OK, _ffmpeg_version(mine)))
        return True
    if _download_ffmpeg():
        return True
    exe = shutil.which("ffmpeg")
    if exe:
        say("%s беру ffmpeg из системы: %s" % (OK, _ffmpeg_version(exe)))
        say("       Он не той версии, на которой проверен выпуск. Если видео или звук")
        say("       не откроются — запустите установку заново, когда будет интернет.")
        return True
    say("%s ffmpeg поставить не удалось. Нужен интернет до github.com —" % BAD)
    say("       запустите установку заново, когда он будет.")
    return False


def check_space(need_gb: float = 2.5) -> bool:
    try:
        free = shutil.disk_usage(str(PROJECT)).free / (1024 ** 3)
    except Exception:
        return True
    say("  свободно на диске: %.1f ГБ (нужно около %.1f ГБ)" % (free, need_gb))
    if free < need_gb:
        say("%s места мало" % BAD)
        return False
    say("%s места достаточно" % OK)
    return True


# ---------------------------------------------------------------- установка


def make_venv() -> bool:
    if VENV_PY.exists():
        say("%s окружение .venv уже есть" % OK)
        return True
    return run([sys.executable, "-m", "venv", str(VENV)], "создаю окружение .venv", 900)


def pip(*args: str) -> list[str]:
    return [str(VENV_PY), "-m", "pip", "--disable-pip-version-check", *args]


def install_deps() -> bool:
    """Поставить библиотеки ровно по описи выпуска (lock.json).

    Каждая сборка качается из своего открытого каталога (PyPI; у релиза с
    pyannote — ещё сайт pytorch) или берётся из vendor/ и сверяется с суммой в
    описи. Ставится из скачанного,
    без поиска зависимостей: все они уже в описи, с точными версиями. Уже
    стоящее той же версии не трогается — повторный запуск докачивает только
    недостающее. Релизу с pyannote добавляются его пакеты.
    """
    from hagen import download, lockfile

    data = lock()
    pkgs = lockfile.packages(data, diarize_engine())
    if not pkgs:
        say("%s нет описи выпуска lock.json рядом с install.py" % BAD)
        return False
    need = lockfile.to_install(pkgs, lockfile.installed(PROJECT), lockfile.floor(data))
    if not need:
        say("%s библиотеки уже стоят по описи" % OK)
        return True
    say("  скачиваю библиотеки по описи: %d из %d…" % (len(need), len(pkgs)))
    wheels = PROJECT / "data" / "_install"

    def step(i: int, total: int, name: str) -> None:
        say("    [%d/%d] %s" % (i + 1, total, name))

    try:
        files = lockfile.fetch_packages(need, wheels, PROJECT, progress=step)
    except (lockfile.LockError, download.DownloadError) as err:
        say("%s %s" % (BAD, err))
        return False
    if not run(pip(*lockfile.install_args(files, PROJECT)), "ставлю библиотеки", 3600):
        return False
    lockfile.drop_new_launchers(PROJECT)
    shutil.rmtree(wheels, ignore_errors=True)
    return True



def localize_python() -> bool:
    r"""Положить копию Python внутрь проекта и переключить на неё окружение.

    Зачем. На Windows файл .venv\Scripts\python.exe — это лишь запускающая
    заглушка: настоящим процессом становится БАЗОВЫЙ интерпретатор, и именно его
    путь видит антивирус, когда приложение просит микрофон. Если базовый Python
    общесистемный, правило в антивирусе пришлось бы выдавать на весь Python.
    Поэтому базовый интерпретатор копируется в папку проекта: правило получается
    узким, а приложение — самостоятельным.
    """
    target = PROJECT / "python"
    cfg = VENV / "pyvenv.cfg"
    if not cfg.exists():
        say("%s нет .venv/pyvenv.cfg" % BAD)
        return False

    lines = io.open(cfg, encoding="utf-8").read().splitlines()
    home = ""
    for ln in lines:
        if ln.strip().lower().startswith("home"):
            home = ln.split("=", 1)[1].strip()
    if not home:
        say("%s в pyvenv.cfg нет строки home" % BAD)
        return False
    if Path(home).resolve() == target.resolve():
        say("%s Python уже внутри проекта" % OK)
        return True

    say("  копирую Python в папку проекта (нужно для узкого правила антивируса)…")
    try:
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)
        shutil.copytree(home, target)
    except Exception as err:
        say("%s копирование не удалось: %s" % (BAD, err))
        return False

    # лишнее внутрь проекта тащить незачем
    for junk in ("Doc", "Lib/test", "Lib/idlelib", "Lib/__pycache__"):
        shutil.rmtree(target / junk, ignore_errors=True)
    sp = target / "Lib" / "site-packages"
    if sp.exists():
        for item in sp.iterdir():
            if item.is_dir():
                shutil.rmtree(item, ignore_errors=True)
            else:
                try:
                    item.unlink()
                except Exception:
                    pass
    scripts = target / "Scripts"
    if scripts.exists():
        for item in scripts.iterdir():
            if item.suffix.lower() in (".exe", ".py"):
                try:
                    item.unlink()
                except Exception:
                    pass

    text = io.open(cfg, encoding="utf-8").read().replace(home, str(target))
    write_atomic(cfg, text)
    say("%s Python перенесён в %s" % (OK, target))
    return True


def write_atomic(path: Path, text: str) -> None:
    """Записать файл целиком или никак: сбой на середине pyvenv.cfg или .pth
    оставил бы Python без библиотек."""
    tmp = path.with_name(path.name + ".tmp")
    with io.open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


PTH_LINE = ("import os, site, sys; site.addsitedir(os.path.join("
            "sys.prefix, os.pardir, '.venv', 'Lib', 'site-packages'))\n")


def portable_paths() -> bool:
    """Переносимый запуск: python\\python.exe видит библиотеки .venv, pyvenv.cfg — на эту папку.

    То же делает hagen/portable.py при каждом запуске программы; здесь —
    чтобы заработали проверки и pip ещё до первого запуска.
    """
    home = PROJECT / "python"
    if not (home / "python.exe").exists():
        return False
    sp = home / "Lib" / "site-packages"
    sp.mkdir(parents=True, exist_ok=True)
    write_atomic(sp / "hagen-venv.pth", PTH_LINE)
    cfg = VENV / "pyvenv.cfg"
    if cfg.exists():
        out = []
        for ln in io.open(cfg, encoding="utf-8").read().splitlines():
            key = ln.split("=", 1)[0].strip().lower()
            if key == "home":
                ln = "home = %s" % home
            elif key == "executable":
                ln = "executable = %s" % (home / "python.exe")
            elif key == "command":
                ln = "command = %s -m venv %s" % (home / "python.exe", VENV)
            out.append(ln)
        write_atomic(cfg, "\n".join(out) + "\n")
    say("%s запуск не зависит от папки: %s" % (OK, home / "python.exe"))
    return True


#: Что можно убрать из установленных библиотек без вреда для работы. Всё это
#: приходит вместе с пакетами при установке: тесты авторов библиотек, заголовки
#: C++ для сборки своих расширений torch (он есть у релиза с pyannote),
#: подсказки редактора, кэш байт-кода (Python пересоздаёт его сам для того,
#: что реально загружает).
PRUNE_DIRS = {"tests", "test", "__pycache__"}
PRUNE_TORCH = ("include", "share")
PRUNE_SUFFIXES = (".pdb", ".lib", ".h", ".hpp", ".cuh", ".pyi")
KEEP_SCRIPTS = {"python.exe", "pythonw.exe"}


def prune_ignore(src: str, names: list[str], root: Path) -> set[str]:
    """Для shutil.copytree(ignore=…) и для чистки на месте: что пропустить в папке src."""
    here = Path(src)
    try:
        rel = here.relative_to(root)
    except ValueError:
        return set()
    parts = [p.lower() for p in rel.parts]
    skip = set()
    in_sp = "site-packages" in parts
    for name in names:
        low = name.lower()
        full = here / name
        if low == "__pycache__":
            skip.add(name)
        elif in_sp and low in ("tests", "test") and full.is_dir():
            skip.add(name)
        elif in_sp and parts[-1:] == ["torch"] and low in PRUNE_TORCH and full.is_dir():
            skip.add(name)
        elif in_sp and low.endswith(PRUNE_SUFFIXES) and full.is_file():
            skip.add(name)
        elif parts[-1:] == ["scripts"] and ".venv" in parts and low.endswith(".exe") and low not in KEEP_SCRIPTS:
            skip.add(name)
    return skip


def prune_runtime(root: Path = PROJECT) -> tuple[int, int]:
    """Убрать лишнее из .venv и python\\ на месте. Возвращает (файлов, байт)."""
    files = size = 0
    for top in (root / ".venv", root / "python"):
        if not top.exists():
            continue
        for cur, dirs, names in os.walk(top, topdown=True):
            skip = prune_ignore(cur, dirs + names, root)
            for name in skip:
                path = Path(cur) / name
                if path.is_dir():
                    for sub in path.rglob("*"):
                        if sub.is_file():
                            files += 1
                            size += sub.stat().st_size
                    shutil.rmtree(path, ignore_errors=True)
                elif path.exists():
                    files += 1
                    size += path.stat().st_size
                    path.unlink(missing_ok=True)
            dirs[:] = [d for d in dirs if d not in skip]
    return files, size


def is_ready_copy() -> bool:
    """Папка перенесена целиком: Python, окружение и модели уже на месте.

    Смотрим на папку models целиком, а не на папки быстрой модели: её может
    не быть вовсе — по умолчанию стоит одна точная (решение 21.09).
    """
    return ((PROJECT / "python" / "python.exe").exists() and VENV_PY.exists()
            and (PROJECT / "models").is_dir())


def prepare_models() -> bool:
    say("  Скачиваю модель распознавания, выбранную в настройках.")
    say("  По умолчанию это одна точная модель, около 230 МБ; другие можно")
    say("  скачать потом в «Настройки → Модели».")
    return run([str(VENV_PY), "run.py", "--prepare"], "готовлю модели", 5400)


# ---------------------------------------------------------------- настройка


def configure() -> None:
    head("Настройка")
    settings_path = PROJECT / "settings.json"
    data: dict = {}
    if settings_path.exists():
        try:
            data = json.load(io.open(settings_path, encoding="utf-8"))
        except Exception:
            data = {}

    default_vault = data.get("vault_path") or str(Path.home() / "Obsidian")
    say("  Куда складывать стенограммы и протоколы (папка хранилища Obsidian).")
    say("  Открываю окно выбора папки…")
    vault = pick_folder("Папка хранилища Obsidian — сюда лягут стенограммы", default_vault)
    if vault:
        say("%s хранилище: %s" % (OK, vault))
    else:
        say("  Папку не выбрали — впишите путь здесь или нажмите Enter,")
        say("  чтобы взять предложенный.")
        vault = ask("Путь к хранилищу", default_vault)
    data["vault_path"] = vault
    data.setdefault("vault_subfolder", "Meetings")
    if not Path(vault).exists():
        say("       Папки пока нет — приложение создаст её при первом сохранении.")

    if diarize_engine() == "pyannote":
        say("")
        say("  Токен HuggingFace нужен только для разметки говорящих.")
        say("  Получить: huggingface.co/settings/tokens (тип Read), и один раз принять")
        say("  лицензию на huggingface.co/pyannote/speaker-diarization-community-1")
        say("  Можно пропустить сейчас и вписать позже в настройках приложения.")
        token = ask("Токен hf_… (Enter — пропустить)", "")
        if token:
            data["hf_token"] = token
            say("%s токен сохранён (в чат и в журналы он не попадает)" % OK)

    write_atomic(settings_path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    say("%s настройки записаны в settings.json" % OK)


def make_shortcuts() -> None:
    """Ярлыки «Hagen» на рабочем столе и в «Пуске» — их делает сама программа.

    Раньше установщик писал их своим кодом и ставил значок Windows вместо
    значка программы. Теперь ярлык один на всех (hagen/platform, run.py
    --shortcuts): со значком программы, и программа сама его поправит, если
    папку перенесут.
    """
    head("Ярлыки")
    run([str(VENV_PY), "run.py", "--shortcuts"], "создаю ярлыки «Hagen»", 180)


def _real_interpreter() -> Path:
    """Файл, который реально становится процессом (его и видит антивирус)."""
    cfg = VENV / "pyvenv.cfg"
    try:
        for ln in io.open(cfg, encoding="utf-8").read().splitlines():
            if ln.strip().lower().startswith("home"):
                cand = Path(ln.split("=", 1)[1].strip()) / "python.exe"
                if cand.exists():
                    return cand
    except Exception:
        pass
    return VENV_PY


def antivirus_note() -> None:
    head("Что разрешить в антивирусе")
    target = _real_interpreter()
    if not target.exists():
        say("  окружение не создано, пропускаю")
        return
    raw = target.read_bytes()
    sha = hashlib.sha256(raw).hexdigest().upper()
    say("  Если полоски уровня стоят на нуле и запись не идёт — антивирус не даёт")
    say("  программе доступ к звукозаписи. Нужно одно правило на ОДИН файл:")
    say("")
    say("      %s" % target)
    say("")
    say("      размер: %d байт" % len(raw))
    say("      SHA256: %s" % sha)
    try:
        res = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "(Get-AuthenticodeSignature '%s').Status" % str(target)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=60, creationflags=NO_WINDOW)
        status = (res.stdout or "").strip().splitlines()[-1:] or ["?"]
        say("      подпись: %s (издатель Python Software Foundation)" % status[0])
    except Exception:
        pass
    say("")
    say("  Это официальный подписанный интерпретатор Python, а не самосборный файл.")
    say("")
    say("  Для корпоративного антивируса (Kaspersky Endpoint) администратору нужны")
    say("  доверенные программы по этим путям — и больше ничего:")
    for exe in (PROJECT / "python" / "python.exe", PROJECT / "ffmpeg" / "bin" / "ffmpeg.exe",
                PROJECT / "ffmpeg" / "bin" / "ffprobe.exe"):
        if exe.exists():
            say("      %s" % exe)
    say("  Если стоит Claude CLI — ещё его claude.exe (документы по подписке).")
    say("  Необязательно: исключить папку программы из проверки — быстрее первый запуск.")


def main() -> int:
    ap = argparse.ArgumentParser(description="Установка Hagen")
    ap.add_argument("--skip-models", action="store_true",
                    help="не скачивать модели (например, если папка models скопирована)")
    ap.add_argument("--yes", action="store_true", help="не задавать вопросов")
    ap.add_argument("--no-shortcuts", action="store_true", help="не создавать ярлыки")
    ap.add_argument("--prune", action="store_true",
                    help="убрать из библиотек тесты, заголовки и кэш (меньше файлов)")
    args = ap.parse_args()

    t0 = time.time()
    # На первом экране установщика — полная форма названия.
    head("Hagen, Your Consigliere — установка")
    say("  Папка программы: %s" % PROJECT)

    if is_ready_copy():
        head("Папка перенесена целиком — ничего не скачиваю")
        portable_paths()
        if args.prune:
            n, b = prune_runtime()
            say("%s убрано лишних файлов: %d (%.0f МБ)" % (OK, n, b / 1048576.0))
        if not args.yes:
            configure()
        if not args.no_shortcuts:
            make_shortcuts()
        antivirus_note()
        head("Готово за %.0f мин" % ((time.time() - t0) / 60.0))
        say("  Запуск: ярлык «Hagen» на рабочем столе.")
        say("  Claude CLI (для документов по подписке) ставится и входит отдельно.")
        return 0

    head("Проверка окружения")
    if not check_python():
        return 1
    ok_ffmpeg = check_ffmpeg()
    check_space()
    if not ok_ffmpeg:
        return 1

    head("Окружение Python")
    if not make_venv():
        return 1

    head("Библиотеки по описи выпуска")
    if not install_deps():
        say("")
        say("  Не удалось поставить библиотеки. Частые причины:")
        say("    - нет интернета или он через прокси (нужны pypi.org, files.pythonhosted.org;")
        say("      релизу с pyannote — ещё download.pytorch.org);")
        say("    - антивирус блокирует запись в папку программы.")
        say("  Уже скачанное не пропадёт: запустите Ustanovka.cmd ещё раз.")
        return 1

    head("Свой Python внутри проекта")
    if not localize_python():
        say("  Пропускаю: приложение будет работать, но правило в антивирусе")
        say("  придётся выдавать на общесистемный Python.")
    portable_paths()
    if args.prune:
        n, b = prune_runtime()
        say("%s убрано лишних файлов: %d (%.0f МБ)" % (OK, n, b / 1048576.0))

    if not args.skip_models:
        head("Модели распознавания")
        if not prepare_models():
            say("  Модели можно доготовить позже командой:")
            say("      .venv\\Scripts\\python.exe run.py --prepare")

    if not args.yes:
        configure()
    if not args.no_shortcuts:
        make_shortcuts()
    antivirus_note()

    head("Установка завершена за %.0f мин" % ((time.time() - t0) / 60.0))
    say("  Запуск: ярлык «Hagen» на рабочем столе.")
    say("  Первый запуск занимает около минуты — прогреваются модели.")
    say("")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        say("")
        say("Установка прервана.")
        sys.exit(1)
