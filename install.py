# -*- coding: utf-8 -*-
r"""Установка «Hagen» на компьютер с Windows.

Проще всего — двойной щелчок по «Ustanovka.cmd»: он найдёт Python (или скачает
его в папку программы) и запустит этот файл.

Два случая, установщик различает их сам:

ПЕРЕНЕСЁННАЯ ПАПКА (архив с другого компьютера: внутри уже есть python\, .venv и
модели). Ничего не скачивается: исправляются пути к Python, создаются ярлыки,
спрашивается папка хранилища Obsidian, печатаются файлы для антивируса.

ЧИСТАЯ УСТАНОВКА (только код). Нужен интернет:
    1. проверяет Python, ffmpeg (скачает сам) и место на диске;
    2. создаёт окружение .venv и ставит зависимости (torch — CPU-сборкой);
    3. кладёт копию Python внутрь папки (узкое правило антивируса, переносимость);
    4. скачивает модели распознавания и переводит их в ONNX (около 2,6 ГБ);
    5. спрашивает папку хранилища Obsidian и токен HuggingFace;
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
TORCH_INDEX = "https://download.pytorch.org/whl/cpu"
TORCH_PINS = ["torch==2.14.0", "torchaudio==2.11.0"]
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


# ---------------------------------------------------------------- проверки


def check_python() -> bool:
    v = sys.version_info
    say("  Python: %d.%d.%d — %s" % (v.major, v.minor, v.micro, sys.executable))
    if (v.major, v.minor) < (3, 10):
        say("%s нужен Python 3.10 или новее" % BAD)
        return False
    if os.name != "nt":
        say("%s приложение рассчитано на Windows" % BAD)
        return False
    say("%s версия подходит" % OK)
    return True


FFMPEG_DIR = PROJECT / "ffmpeg"
FFMPEG_URL = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"


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


def _try_winget() -> bool:
    if not shutil.which("winget"):
        return False
    say("  пробую поставить ffmpeg через winget…")
    try:
        res = subprocess.run(
            ["winget", "install", "--id", "Gyan.FFmpeg", "-e", "--silent",
             "--accept-package-agreements", "--accept-source-agreements"],
            timeout=1800)
    except Exception as err:
        say("  winget не справился: %s" % err)
        return False
    if res.returncode != 0:
        say("  winget вернул код %s" % res.returncode)
        return False
    # PATH в текущем процессе не обновится — ищем напрямую
    base = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" / "Packages"
    for cand in base.glob("Gyan.FFmpeg*/**/bin/ffmpeg.exe"):
        return _adopt_ffmpeg(cand.parent)
    return bool(shutil.which("ffmpeg"))


def _adopt_ffmpeg(bin_dir: Path) -> bool:
    """Скопировать ffmpeg.exe и ffprobe.exe в папку проекта."""
    try:
        target = FFMPEG_DIR / "bin"
        target.mkdir(parents=True, exist_ok=True)
        taken = 0
        for name in ("ffmpeg.exe", "ffprobe.exe"):
            src = bin_dir / name
            if src.exists():
                shutil.copy2(src, target / name)
                taken += 1
        if taken:
            say("%s ffmpeg перенесён в папку проекта (%d файла)" % (OK, taken))
            return True
    except Exception as err:
        say("  не удалось перенести ffmpeg: %s" % err)
    return False


def _download_ffmpeg() -> bool:
    """Скачать переносимую сборку ffmpeg прямо в папку проекта."""
    import urllib.request
    import zipfile

    say("  скачиваю переносимую сборку ffmpeg (около 45 МБ)…")
    tmp_zip = PROJECT / "_ffmpeg.zip"
    try:
        with urllib.request.urlopen(FFMPEG_URL, timeout=120) as src, \
                open(tmp_zip, "wb") as dst:
            shutil.copyfileobj(src, dst)
    except Exception as err:
        say("%s скачать не вышло: %s" % (BAD, err))
        tmp_zip.unlink(missing_ok=True)
        return False

    try:
        with zipfile.ZipFile(tmp_zip) as zf:
            names = [n for n in zf.namelist()
                     if n.endswith(("/bin/ffmpeg.exe", "/bin/ffprobe.exe"))]
            if not names:
                say("%s в архиве нет ffmpeg.exe" % BAD)
                return False
            target = FFMPEG_DIR / "bin"
            target.mkdir(parents=True, exist_ok=True)
            for n in names:
                with zf.open(n) as src, open(target / Path(n).name, "wb") as dst:
                    shutil.copyfileobj(src, dst)
        say("%s ffmpeg распакован в папку проекта" % OK)
        return True
    except Exception as err:
        say("%s распаковать не вышло: %s" % (BAD, err))
        return False
    finally:
        tmp_zip.unlink(missing_ok=True)


def check_ffmpeg() -> bool:
    """Найти ffmpeg, а если его нет — поставить самим.

    Порядок: своя копия в папке проекта, затем PATH, затем winget, затем
    скачивание переносимой сборки. Так установка работает и на чистой машине.
    """
    mine = _ffmpeg_in_project()
    if mine:
        say("%s ffmpeg свой, в папке проекта: %s" % (OK, _ffmpeg_version(mine)))
        return True

    exe = shutil.which("ffmpeg")
    if exe:
        say("%s ffmpeg в системе: %s" % (OK, _ffmpeg_version(exe)))
        say("       (своя копия в папке проекта не делается: полные сборки весят")
        say("        сотни мегабайт. На другом компьютере установщик достанет")
        say("        лёгкую переносимую сборку сам.)")
        return True

    say("  ffmpeg не найден — ставлю сам.")
    if _try_winget() and (_ffmpeg_in_project() or shutil.which("ffmpeg")):
        say("%s ffmpeg установлен" % OK)
        return True
    if _download_ffmpeg():
        return True

    say("%s ffmpeg поставить не удалось." % BAD)
    say("       Сделайте это вручную и запустите установку заново:")
    say("           winget install Gyan.FFmpeg")
    say("       или скачайте %s" % FFMPEG_URL)
    say("       и положите ffmpeg.exe и ffprobe.exe в папку %s" % (FFMPEG_DIR / "bin"))
    return False


def check_space(need_gb: float = 4.0) -> bool:
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


def install_deps(offline_models: bool = False) -> bool:
    if not run(pip("install", "--upgrade", "pip", "setuptools", "wheel"),
               "обновляю pip", 900):
        return False
    if not run(pip("install", *TORCH_PINS, "--index-url", TORCH_INDEX),
               "ставлю torch (сборка для процессора, без CUDA)", 3600):
        return False
    req = PROJECT / "requirements.txt"
    if not req.exists():
        say("%s нет файла requirements.txt рядом с install.py" % BAD)
        return False
    # torch уже стоит нужной версии — не даём его переустановить
    con = PROJECT / "constraints.txt"
    with io.open(con, "w", encoding="utf-8") as fh:
        fh.write("torch==2.14.0+cpu\ntorchaudio==2.11.0+cpu\n")
    return run(pip("install", "-c", str(con), "-r", str(req)),
               "ставлю остальные зависимости", 3600)



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
#: C++ для сборки своих расширений torch, подсказки редактора, кэш байт-кода
#: (Python пересоздаёт его сам для того, что реально загружает).
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
    """Папка перенесена целиком: Python, окружение и модели уже на месте."""
    return ((PROJECT / "python" / "python.exe").exists() and VENV_PY.exists()
            and (PROJECT / "models" / "onnx").exists() and (PROJECT / "models" / "gigaam").exists())


def prepare_models() -> bool:
    say("  Скачиваю модели распознавания и перевожу их в ONNX.")
    say("  Это один раз, примерно 2,6 ГБ и несколько минут.")
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
    vault = ask("Путь к хранилищу", default_vault)
    data["vault_path"] = vault
    data.setdefault("vault_subfolder", "Meetings")
    if not Path(vault).exists():
        say("       Папки пока нет — приложение создаст её при первом сохранении.")

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
    head("Ярлыки")
    ps = r'''
$proj = '%s'
$py = Join-Path $proj 'python\python.exe'
if (-not (Test-Path $py)) { $py = Join-Path $proj '.venv\Scripts\python.exe' }
$ws = New-Object -ComObject WScript.Shell
$targets = @(
  (Join-Path ([Environment]::GetFolderPath('Desktop')) 'Hagen.lnk'),
  (Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\Hagen.lnk')
)
foreach ($lnk in $targets) {
  $sc = $ws.CreateShortcut($lnk)
  $sc.TargetPath = $py
  $sc.Arguments = 'run.py --app'
  $sc.WorkingDirectory = $proj
  $sc.Description = 'Your Consigliere'
  $sc.IconLocation = "$env:SystemRoot\System32\imageres.dll,175"
  $sc.WindowStyle = 7
  $sc.Save()
  Write-Output ('  создан: ' + $lnk)
}
''' % str(PROJECT)
    tmp = PROJECT / "_mkshortcut.ps1"
    with io.open(tmp, "w", encoding="utf-8-sig") as fh:
        fh.write(ps)
    try:
        subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                        "-File", str(tmp)], timeout=120, creationflags=NO_WINDOW)
        say("%s ярлыки созданы" % OK)
    except Exception as err:
        say("%s ярлыки не создались: %s" % (BAD, err))
    finally:
        tmp.unlink(missing_ok=True)


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

    head("Зависимости")
    if not install_deps():
        say("")
        say("  Не удалось поставить зависимости. Частые причины:")
        say("    - нет интернета или он через прокси;")
        say("    - антивирус блокирует запись в папку проекта.")
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
