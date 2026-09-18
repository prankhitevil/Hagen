# -*- coding: utf-8 -*-
"""Проверка 55: программа работает из любой папки, переносимый архив.

Решения 13.09.2026: перенос на рабочий ноутбук переносимой папкой
(архив с ключами), установщик из интернета — для других людей; все пути
программы — от её папки, вручную задаются только хранилище Obsidian и устройства.

Всё, что меняет файлы, проверяется в песочнице во временной папке: настоящие
ярлыки, pyvenv.cfg и settings.json не трогаются. Настоящий python\\python.exe
только запускается — видит ли он библиотеки .venv.
"""
import io
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
sys.path.insert(0, str(PROJECT / "tools"))

LINES = []
FAIL = []


def say(msg):
    LINES.append(str(msg))
    try:
        print(str(msg), flush=True)
    except Exception:
        # Консоль не знает этих букв (бывает cp1251) — печатаем без них.
        try:
            print(str(msg).encode("ascii", "replace").decode("ascii"), flush=True)
        except Exception:
            pass


def check(name, ok, detail=""):
    if not ok:
        FAIL.append(name)
    say(("   ok    " if ok else "   ПЛОХО ") + name + (": " + str(detail)[:300] if detail != "" else ""))


import install  # noqa: E402
import make_portable  # noqa: E402

from hagen import config  # noqa: E402
from hagen.platform.windows import portable  # noqa: E402

SANDBOX = Path(tempfile.mkdtemp(prefix="hagen-t55-"))
REAL_PROJECT = config.PROJECT_DIR


def fake_project(name):
    root = SANDBOX / name
    (root / "python" / "Lib").mkdir(parents=True)
    (root / "python" / "python.exe").write_bytes(b"")
    (root / ".venv" / "Scripts").mkdir(parents=True)
    return root


def touch(path, data=b"x"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


try:
    say("=== 1. Запуск переносимым Python: файл .pth ===")
    old = fake_project("old")
    config.PROJECT_DIR = old
    check("запускать — python\\python.exe из папки программы", portable.launcher() == old / "python" / "python.exe")
    check(".pth записан", portable.ensure_pth() is True)
    pth = old / "python" / "Lib" / "site-packages" / portable.PTH_NAME
    text = io.open(pth, encoding="utf-8").read() if pth.exists() else ""
    check("в .pth нет полного пути — только от sys.prefix", text == portable.PTH_LINE and str(old) not in text, text)
    check("повторный запуск ничего не переписывает", portable.ensure_pth() is False)
    check("установщик пишет ту же строку", install.PTH_LINE == portable.PTH_LINE)
    bare = SANDBOX / "bare"
    bare.mkdir()
    config.PROJECT_DIR = bare
    check("без python\\ в папке — .pth не нужен", portable.ensure_pth() is False)
    check("без python\\ — запуск через .venv", portable.launcher() == bare / ".venv" / "Scripts" / "python.exe")

    say("")
    say("=== 2. Папку перенесли: pyvenv.cfg ===")
    config.PROJECT_DIR = old
    cfg = old / ".venv" / "pyvenv.cfg"
    io.open(cfg, "w", encoding="utf-8").write(
        "home = C:\\Users\\someone\\Apps\\hagen\\python\n"
        "include-system-site-packages = false\n"
        "version = 3.12.10\n"
        "executable = C:\\Users\\someone\\Apps\\hagen\\python\\python.exe\n"
        "command = C:\\Users\\someone\\Apps\\hagen\\python\\python.exe -m venv C:\\Users\\someone\\Apps\\hagen\\.venv\n")
    check("старые пути исправлены", portable.fix_venv_cfg() is True)
    body = io.open(cfg, encoding="utf-8").read()
    check("home — на python\\ новой папки", ("home = %s\n" % (old / "python")) in body, body)
    check("executable и command — тоже", ("executable = %s" % (old / "python" / "python.exe")) in body
          and ("-m venv %s" % (old / ".venv")) in body)
    check("остальные строки на месте", "include-system-site-packages = false" in body and "version = 3.12.10" in body)
    check("старого пути не осталось", "someone" not in body)
    check("второй раз — без изменений", portable.fix_venv_cfg() is False)

    say("")
    say("=== 3. Ярлыки ведут в старую папку ===")
    links = SANDBOX / "links"
    link = links / portable.SHORTCUT_NAME
    portable.write_shortcut(link, "run.py --app --tray", "Hagen (проверка 55)")
    new = fake_project("new")
    config.PROJECT_DIR = new
    fixed = portable.refresh_shortcuts({"test": links})
    check("ярлык переведён на новую папку", fixed == [str(link)], fixed)
    import win32com.client
    sc = win32com.client.Dispatch("WScript.Shell").CreateShortcut(str(link))
    # Сравниваем файлы, а не написание: ярлык отдаёт полный путь, а временная
    # папка бывает короткой («PETROV~1» — имя пользователя длиннее 8 букв).
    check("цель — python.exe новой папки", portable._same_path(sc.TargetPath, new / "python" / "python.exe"),
          sc.TargetPath)
    check("рабочая папка — новая", portable._same_path(sc.WorkingDirectory, new), sc.WorkingDirectory)
    check("параметры запуска сохранены (трей)", sc.Arguments == "run.py --app --tray", sc.Arguments)
    check("второй раз ничего не трогает", portable.refresh_shortcuts({"test": links}) == [])
    check("чужих ярлыков нет — ничего не делает", portable.refresh_shortcuts({"x": SANDBOX / "bare"}) == [])
    import win32api
    short = Path(win32api.GetShortPathName(str(new)))
    check("один путь, записанный коротко и полно, — один и тот же",
          portable._same_path(short, new) and portable._same_path(str(new) + "\\", new), (str(short), str(new)))
    check("разные папки не путаются", not portable._same_path(new, SANDBOX / "old")
          and not portable._same_path("", new))

    say("")
    say("=== 4. Настоящая папка программы ===")
    config.PROJECT_DIR = REAL_PROJECT
    run_text = io.open(REAL_PROJECT / "run.py", encoding="utf-8").read()
    tray_text = io.open(REAL_PROJECT / "hagen" / "platform" / "windows" / "tray.py", encoding="utf-8").read()
    # Оба места зовут систему через розетку — см. hagen/platform/base.py.
    check("run.py чинит пути при каждом запуске", "ensure_portable(" in run_text)
    check("автозапуск с Windows — через тот же ярлык", "write_shortcut(" in tray_text)
    base = REAL_PROJECT / "python" / "python.exe"
    if base.exists():
        env = {k: v for k, v in os.environ.items() if k.upper() not in ("PYTHONPATH", "PYTHONHOME")}
        r = subprocess.run([str(base), "-c", "import fastapi, sys; print(fastapi.__file__); print(sys.prefix)"],
                           capture_output=True, text=True, encoding="utf-8", timeout=120, env=env,
                           cwd=tempfile.gettempdir())
        out = (r.stdout or "").splitlines()
        sp = str(REAL_PROJECT / ".venv" / "Lib" / "site-packages").lower()
        check("python\\python.exe видит библиотеки .venv из любой текущей папки",
              r.returncode == 0 and out and out[0].lower().startswith(sp), (r.stdout or r.stderr)[-300:])
        cfg_real = io.open(REAL_PROJECT / ".venv" / "pyvenv.cfg", encoding="utf-8").read()
        check("pyvenv.cfg указывает на python\\ этой папки", ("home = %s" % (REAL_PROJECT / "python")) in cfg_real)
    else:
        say("   python\\python.exe нет — пропускаю (установка без переносимого Python)")

    for name in ("Hagen.cmd", "Ustanovka.cmd"):
        raw = (REAL_PROJECT / name).read_bytes()
        try:
            raw.decode("ascii")
            ascii_ok = True
        except UnicodeDecodeError:
            ascii_ok = False
        check("%s — только ASCII (иначе cmd.exe не разберёт)" % name, ascii_ok)
        check("%s — переносы строк CRLF" % name, raw.count(b"\n") == raw.count(b"\r\n") and raw.count(b"\n") > 5)
    ust = io.open(REAL_PROJECT / "Ustanovka.cmd", encoding="ascii").read()
    check("установщик берёт python\\python.exe, иначе py, иначе скачивает",
          ust.index("python\\python.exe") < ust.index("where py") < ust.index("curl.exe"))
    check("Python скачивается по рабочему адресу nuget", "api.nuget.org/v3-flatcontainer/python/" in ust)
    dk = io.open(REAL_PROJECT / "Hagen.cmd", encoding="ascii").read()
    check("Hagen.cmd запускает python\\python.exe, запасной — .venv",
          'set "PY=python\\python.exe"' in dk and ".venv\\Scripts\\python.exe" in dk)

    say("")
    say("=== 5. Что убирается из библиотек ===")
    root = SANDBOX / "prune"
    sp = root / ".venv" / "Lib" / "site-packages"
    keep = [touch(sp / "numpy" / "core.py"), touch(sp / "torch" / "lib" / "torch_cpu.dll"),
            touch(root / ".venv" / "Scripts" / "python.exe"), touch(root / "tests" / "t1.py"),
            touch(sp / "pyannote" / "audio" / "models" / "segmentation.py"),
            touch(root / "python" / "python.exe")]
    drop = [touch(sp / "numpy" / "tests" / "test_a.py"), touch(sp / "scipy" / "test" / "x.py"),
            touch(sp / "torch" / "include" / "c10" / "a.h"), touch(sp / "torch" / "share" / "cmake" / "x.cmake"),
            touch(sp / "numpy" / "__pycache__" / "core.cpython-312.pyc"), touch(sp / "onnxruntime" / "capi" / "x.pdb"),
            touch(sp / "numpy" / "core.pyi"), touch(root / ".venv" / "Scripts" / "pip.exe"),
            touch(root / "python" / "Lib" / "__pycache__" / "os.cpython-312.pyc")]
    n, b = install.prune_runtime(root)
    check("лишнее удалено", all(not p.exists() for p in drop), [str(p.relative_to(root)) for p in drop if p.exists()])
    check("нужное осталось (и tests\\ самой программы)", all(p.exists() for p in keep),
          [str(p.relative_to(root)) for p in keep if not p.exists()])
    check("посчитано удалённое", n == len(drop), n)

    say("")
    say("=== 6. Переносимый архив ===")
    check("в архив не идут записи, журнал, копии, токен-файл",
          not {"data", "logs", "_backup"} & set(make_portable.TOP_DIRS)
          and "hf_token.txt" not in make_portable.TOP_FILES and "settings.json" not in make_portable.TOP_FILES)
    src = SANDBOX / "src"
    src.mkdir()
    io.open(src / "settings.json", "w", encoding="utf-8").write(
        '{"vault_path": "D:/vault", "hf_token": "t55-fake", "api_keys": {"x": "t55-fake"},'
        ' "mic_device_name": "Brio", "mic_device_index": 3, "far_device_index": 5, "vault_confirmed": true}')
    real_mp = make_portable.PROJECT
    make_portable.PROJECT = src
    try:
        import json
        with_keys, no_keys = SANDBOX / "k1", SANDBOX / "k2"
        with_keys.mkdir()
        no_keys.mkdir()
        make_portable.settings_for(with_keys, keys=True)
        make_portable.settings_for(no_keys, keys=False)
        a = json.load(io.open(with_keys / "settings.json", encoding="utf-8"))
        z = json.load(io.open(no_keys / "settings.json", encoding="utf-8"))
    finally:
        make_portable.PROJECT = real_mp
    check("архив для себя — с ключами", a.get("hf_token") == "t55-fake" and "api_keys" in a)
    check("устройства этого компьютера не переносятся",
          not {"mic_device_name", "mic_device_index", "far_device_index", "vault_confirmed"} & set(a), sorted(a))
    check("архив для других — без токена и ключей", "hf_token" not in z and "api_keys" not in z, sorted(z))
    check("остальные настройки на месте", a.get("vault_path") == "D:/vault" and z.get("vault_path") == "D:/vault")

    stage = SANDBOX / "dist" / "Hagen"
    touch(stage / "run.py", b"print('hi')\n" * 50)
    touch(stage / "models" / "m.onnx", os.urandom(4096))
    target = SANDBOX / "dist" / "Hagen-portable.zip"
    make_portable.make_zip(stage, target)
    with zipfile.ZipFile(target) as zf:
        info = {i.filename: i for i in zf.infolist()}
    check("в архиве верхняя папка Hagen\\", sorted(info) == ["Hagen/models/m.onnx", "Hagen/run.py"], sorted(info))
    check("модели — без сжатия (быстро, всё равно не жмутся)",
          info.get("Hagen/models/m.onnx") and info["Hagen/models/m.onnx"].compress_type == zipfile.ZIP_STORED)
    check("код — сжат", info.get("Hagen/run.py") and info["Hagen/run.py"].compress_type == zipfile.ZIP_DEFLATED)
    check("недособранный архив не остаётся", not target.with_suffix(".zip.part").exists())

    ro = stage / ".git" / "objects" / "01" / "abc"
    touch(ro, b"x")
    os.chmod(ro, stat.S_IREAD)
    try:
        make_portable.remove_tree(stage)
        gone = not stage.exists()
    except OSError as err:
        gone = err
    check("прежняя сборка удаляется, даже файлы .git «только для чтения»", gone is True, gone)
finally:
    config.PROJECT_DIR = REAL_PROJECT
    shutil.rmtree(SANDBOX, ignore_errors=True)

say("")
say("ИТОГО провалов: %d" % len(FAIL))
for f in FAIL:
    say("   - " + f)
io.open(PROJECT / "tests" / "t55_result.txt", "w", encoding="utf-8").write("\n".join(LINES))
sys.exit(1 if FAIL else 0)
