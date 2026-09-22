# -*- coding: utf-8 -*-
"""Программа работает из любой папки (перенос на другой компьютер, 13.09.2026).

Всё своё программа и так ищет от своей папки: записи, модели, журнал, настройки.
Полный путь к папке был зашит в трёх местах — здесь они исправляются сами:

1. Запуск. Ярлыки запускают python\\python.exe из папки программы — это
   переносимая копия Python (её же разрешают в антивирусе). Библиотеки лежат
   в .venv и подхватываются файлом hagen-venv.pth, в котором путь относительный.
2. .venv\\pyvenv.cfg хранит полный путь к python\\. Через .venv\\Scripts\\python.exe
   запускаются проверки и pip; после переноса папки строка home переписывается.
3. Ярлыки «Hagen» на рабочем столе, в «Пуске» и в «Автозагрузке», ведущие в
   старую папку или с чужим значком, пересоздаются на новую, со значком
   программы. Ставит их установщик — через make_shortcuts отсюда же.
"""
from __future__ import annotations

import io
import logging
import os
from pathlib import Path
from typing import Any

from ... import config

log = logging.getLogger("hagen.portable")

SHORTCUT_NAME = "Hagen.lnk"
PTH_NAME = "hagen-venv.pth"
PTH_LINE = ("import os, site, sys; site.addsitedir(os.path.join("
            "sys.prefix, os.pardir, '.venv', 'Lib', 'site-packages'))\n")


def project() -> Path:
    return config.PROJECT_DIR


def base_python() -> Path:
    return project() / "python" / "python.exe"


def launcher() -> Path:
    """Чем запускать программу: переносимым Python, если он есть в папке."""
    exe = base_python()
    return exe if exe.exists() else project() / ".venv" / "Scripts" / "python.exe"


def ensure_pth() -> bool:
    """Научить python\\python.exe видеть библиотеки из .venv (путь относительный)."""
    sp = project() / "python" / "Lib" / "site-packages"
    if not sp.parent.exists():
        return False
    target = sp / PTH_NAME
    try:
        if target.exists() and io.open(target, encoding="utf-8").read() == PTH_LINE:
            return False
        sp.mkdir(parents=True, exist_ok=True)
        _write_atomic(target, PTH_LINE)
        log.info("переносимый запуск: %s", target)
        return True
    except OSError as err:
        log.warning("не записал %s: %s", target, err)
        return False


def _write_atomic(path: Path, text: str) -> None:
    """Файл целиком или никак: сбой на середине pyvenv.cfg или .pth оставил бы
    Python без библиотек."""
    tmp = path.with_name(path.name + ".tmp")
    with io.open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def fix_venv_cfg() -> bool:
    """Строка home в .venv\\pyvenv.cfg — на python\\ текущей папки программы."""
    cfg = project() / ".venv" / "pyvenv.cfg"
    home = project() / "python"
    if not cfg.exists() or not (home / "python.exe").exists():
        return False
    try:
        lines = io.open(cfg, encoding="utf-8").read().splitlines()
    except OSError:
        return False
    changed = False
    out = []
    for ln in lines:
        key = ln.split("=", 1)[0].strip().lower()
        if key == "home":
            new = "home = %s" % home
            changed |= ln.strip() != new
            out.append(new)
        elif key == "executable":
            new = "executable = %s" % (home / "python.exe")
            changed |= ln.strip() != new
            out.append(new)
        elif key == "command":
            new = "command = %s -m venv %s" % (home / "python.exe", project() / ".venv")
            changed |= ln.strip() != new
            out.append(new)
        else:
            out.append(ln)
    if not changed:
        return False
    try:
        _write_atomic(cfg, "\n".join(out) + "\n")
    except OSError as err:
        log.warning("pyvenv.cfg не исправлен: %s", err)
        return False
    log.info("папка программы перенесена: pyvenv.cfg указывает на %s", home)
    return True


def _special_folders() -> dict[str, Path]:
    import win32com.client

    shell = win32com.client.Dispatch("WScript.Shell")
    return {"desktop": Path(shell.SpecialFolders("Desktop")),
            "menu": Path(shell.SpecialFolders("Programs")),
            "startup": Path(shell.SpecialFolders("Startup"))}


def app_icon() -> str | None:
    """Значок программы для ярлыков. Нет файла — ярлык останется как был."""
    path = project() / "hagen" / "icons" / "hagen-idle.ico"
    return str(path) if path.exists() else None


def write_shortcut(link: Path, arguments: str, description: str,
                   icon: str | None = None) -> None:
    import win32com.client

    shell = win32com.client.Dispatch("WScript.Shell")
    link.parent.mkdir(parents=True, exist_ok=True)
    sc = shell.CreateShortcut(str(link))
    sc.TargetPath = str(launcher())
    sc.Arguments = arguments
    sc.WorkingDirectory = str(project())
    sc.WindowStyle = 7                      # свёрнуто: консоль службы не мешает
    sc.Description = description
    if icon:
        sc.IconLocation = icon
    sc.Save()


def _same_path(a: Any, b: Any) -> bool:
    """Один ли это файл или папка, как бы путь ни был записан.

    Windows пишет одну и ту же папку по-разному: полным именем и коротким
    («C:\\Users\\PETROV~1» вместо «C:\\Users\\petrov_ivan» — имя пользователя
    длиннее восьми букв), с косой чертой на конце и без. Ярлык отдаёт путь
    полным, а папка могла прийти короткой — буквальное сравнение считало такой
    ярлык чужим и переписывало его при каждом запуске (найдено 14.09 на рабочем
    ноутбуке проверкой t55).
    """
    def norm(p: Any) -> str:
        text = str(p or "").strip().rstrip("\\/")
        if not text:
            return ""
        try:
            text = os.path.realpath(text)
        except (OSError, ValueError):
            pass
        return os.path.normcase(os.path.normpath(text))

    na, nb = norm(a), norm(b)
    return bool(na) and na == nb


#: Ярлыки, которые ставит установщик. «Автозагрузка» — не его: её ярлык
#: появляется и пропадает галочкой в настройках (tray.set_autostart).
INSTALL_FOLDERS = ("desktop", "menu")
DESCRIPTION = "Hagen, Your Consigliere"


def make_shortcuts(folders: dict[str, Path] | None = None) -> list[str]:
    """Ярлыки «Hagen» на рабочем столе и в «Пуске» — на эту папку, со своим значком.

    Раньше их писал сам установщик, отдельным кодом, и ставил значок Windows
    вместо значка программы; а refresh_shortcuts такой ярлык не трогал — он вёл
    куда надо. Теперь ярлык один на всех: этот.
    """
    try:
        folders = folders or {k: v for k, v in _special_folders().items() if k in INSTALL_FOLDERS}
    except Exception as err:
        log.info("папки ярлыков не определились: %s", err)
        return []
    made = []
    for folder in folders.values():
        link = folder / SHORTCUT_NAME
        try:
            write_shortcut(link, "run.py --app", DESCRIPTION, app_icon())
            made.append(str(link))
        except Exception as err:
            log.warning("ярлык %s не создан: %s", link, err)
    return made


def _icon_path(location: Any) -> str:
    """Файл значка из IconLocation ярлыка: «путь,номер» → «путь»."""
    text = str(location or "").strip()
    head, sep, tail = text.rpartition(",")
    return head if sep and tail.strip().lstrip("-").isdigit() else text


def refresh_shortcuts(folders: dict[str, Path] | None = None) -> list[str]:
    """Ярлыки «Hagen», которые ведут в другую папку, не тем Python или с чужим
    значком, — на эту папку и со значком программы.

    Чужой значок — у ярлыков прежнего установщика (значок Windows): их
    поправляет первый же запуск, переустанавливать не нужно.
    """
    import win32com.client

    try:
        folders = folders or _special_folders()
    except Exception as err:
        log.info("папки ярлыков не определились: %s", err)
        return []
    shell = win32com.client.Dispatch("WScript.Shell")
    icon = app_icon()
    fixed = []
    for folder in folders.values():
        link = folder / SHORTCUT_NAME
        if not link.exists():
            continue
        try:
            sc = shell.CreateShortcut(str(link))
            if (_same_path(str(sc.TargetPath), launcher())
                    and _same_path(str(sc.WorkingDirectory), project())
                    and (not icon or _same_path(_icon_path(sc.IconLocation), icon))):
                continue
            args = str(sc.Arguments or "run.py --app")
            sc.TargetPath = str(launcher())
            sc.WorkingDirectory = str(project())
            sc.Arguments = args
            if icon:
                sc.IconLocation = icon      # свой значок вместо питоновского (17.09)
            sc.Save()
            fixed.append(str(link))
        except Exception as err:
            log.info("ярлык %s не обновлён: %s", link, err)
    if fixed:
        log.info("ярлыки поправлены на эту папку и значок программы: %s", ", ".join(fixed))
    return fixed


def ensure(shortcuts: bool = True) -> dict[str, Any]:
    """Всё, что нужно для работы из текущей папки. Дёшево, зовётся при каждом запуске."""
    res: dict[str, Any] = {"pth": False, "cfg": False, "shortcuts": []}
    if os.name != "nt":
        return res
    res["pth"] = ensure_pth()
    res["cfg"] = fix_venv_cfg()
    if shortcuts:
        res["shortcuts"] = refresh_shortcuts()
    return res
