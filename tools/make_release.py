# -*- coding: utf-8 -*-
r"""Собрать архив выпуска для релиза на GitHub.

    .venv\Scripts\python.exe tools\make_release.py            лёгкий архив
    .venv\Scripts\python.exe tools\make_release.py --full     и ещё полный

Лёгкий — «Hagen-v<версия>-windows.zip», около 70 МБ, единственный архив
релиза. Внутри:
  * код — снимок коммита (git archive), ровно то же, что уходит в витрину
    (tools/make_mirror.py), без внутреннего;
  * Python 3.12 (папка python\ этой машины, без лишнего) — чтобы установщику
    было на чём запуститься без интернета и без Python у человека;
  * files.json — список файлов программы с суммами: по нему обновление
    понимает, что заменить и что убрать (hagen/updates.py).
Библиотеки, ffmpeg и модель распознавания в архив не кладутся: установщик
докачивает их из открытых источников ровно тех версий, что в описи lock.json.
Этот же архив программа берёт при обновлении — из него нужен только код.

Полный — «Hagen-v<версия>-windows-full.zip», около 1,6 ГБ, для компьютеров без
доступа к PyPI, Hugging Face и GitHub. Собирается только по запросу и НЕ из
рабочей папки: лёгкий архив распаковывается в отдельную папку, и в ней
запускается установщик — полный архив равен «лёгкий плюс то, что он поставил
бы сам». Ничего из этой машины (свои модели, pyannote, следы проверок) туда не
попадает; пути папки сборки в окружении заменяются нейтральными.

Оба архива перед упаковкой проверяются списком личного и ключей, как витрина.
Номер версии — из release.json; перед выпуском его поднимают, собирают опись
(tools/make_lock.py) и коммитят.
"""
from __future__ import annotations

import argparse
import io
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
sys.path.insert(0, str(PROJECT / "tools"))

import install  # noqa: E402  правила чистки Python общие с установщиком
import make_mirror  # noqa: E402
import make_portable  # noqa: E402
from hagen import download, lockfile, release  # noqa: E402

#: Папка программы внутри архива: человек распаковывает и видит «Hagen».
TOP = "Hagen"
#: Список файлов программы в архиве. Сам в себя не входит.
FILES = "files.json"
#: Что при полной сборке убирается после установщика: следы его работы и
#: журналы загрузок моделей (в них пути этой машины).
AFTER_INSTALL = ("data", "logs", "settings.json", "_ffmpeg.zip", "models/hf/xet")


def say(msg: str = "") -> None:
    print(msg, flush=True)


def copy_python(dst: Path) -> None:
    """Python этой машины без лишнего: кэша, тестов, посторонних пакетов."""
    src = PROJECT / "python"
    if not (src / "python.exe").exists():
        raise SystemExit("нет python\\python.exe — выпуску не на чем запускаться")

    def ignore(cur: str, names: list[str]) -> set[str]:
        skip = install.prune_ignore(cur, names, PROJECT)
        here = Path(cur)
        if here == src / "Lib" / "site-packages":
            skip |= {n for n in names if n != "hagen-venv.pth"}
        if here == src:
            skip |= {n for n in names if n in ("Scripts", "Doc")}
        return skip

    shutil.copytree(src, dst / "python", ignore=ignore)


def program_files(stage: Path) -> dict[str, str]:
    """Файлы программы в сборке с суммами: всё, кроме Python и самого списка."""
    out: dict[str, str] = {}
    for cur, _dirs, names in os.walk(stage):
        for name in names:
            path = Path(cur) / name
            rel = path.relative_to(stage).as_posix()
            if rel.split("/", 1)[0] == "python" or rel == FILES:
                continue
            out[rel] = download.sha256_file(path)
    return dict(sorted(out.items()))


def build_light(ref: str, stage: Path) -> Path:
    say("снимок кода: %s…" % ref)
    files = make_mirror.export(ref, stage)
    say("Python…")
    copy_python(stage)
    # Код — как витрину; Python — только на следы этой машины (check_stage
    # сам различает свои файлы и чужие программы).
    bad = make_mirror.check(stage, files) + make_portable.check_stage(stage)
    if bad:
        for line in bad[:40]:
            say("   " + line)
        raise SystemExit("ВЫПУСК НЕ СОБИРАЕТСЯ: нашлось то, чему в чужих руках не место.")
    info = release.read(stage / "release.json")
    listing = {"version": release.version(stage / "release.json"), "files": program_files(stage)}
    with io.open(stage / FILES, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(listing, fh, ensure_ascii=False, indent=1)
        fh.write("\n")
    say("файлов программы: %d, движок разметки: %s"
        % (len(listing["files"]), info.get("diarize", "?")))
    return stage


def build_full(light: Path, stage: Path) -> Path:
    """Лёгкая сборка плюс то, что по описи поставил бы установщик."""
    say("копия лёгкой сборки для полной…")
    shutil.copytree(light, stage)
    say("запускаю установщик в ней (библиотеки, ffmpeg, модель — по описи)…")
    # --prune — без тестов внутри библиотек, заголовков C++ и кэша: минус две
    # трети файлов, как в переносимой сборке.
    res = subprocess.run([str(stage / "python" / "python.exe"), "install.py", "--yes",
                          "--no-shortcuts", "--prune"], cwd=str(stage))
    if res.returncode != 0:
        raise SystemExit("установщик в сборке не справился (код %s)" % res.returncode)
    for name in AFTER_INSTALL:
        path = stage / name
        if path.is_dir():
            shutil.rmtree(path)
        elif path.exists():
            path.unlink()
    for cache in list(stage.rglob("__pycache__")):
        shutil.rmtree(cache, ignore_errors=True)
    paths = sorted({str(stage), str(stage.resolve())}, key=len, reverse=True)
    make_portable.neutral_venv(stage, paths)
    neutral_direct_urls(stage, paths)
    bad = make_portable.check_stage(stage)
    if bad:
        for line in bad[:40]:
            say("   " + line)
        raise SystemExit("ПОЛНЫЙ ВЫПУСК НЕ СОБИРАЕТСЯ: в нём следы этой машины.")
    return stage


def neutral_direct_urls(stage: Path, paths: list[str]) -> None:
    """Пакеты, поставленные из файла (vendor/), помнят путь к нему — меняем на нейтральный."""
    sp = lockfile.site_packages(stage)
    for item in sp.glob("*.dist-info/direct_url.json"):
        text = item.read_text(encoding="utf-8")
        new = text
        for p in paths:
            for form in (p, p.replace("\\", "/")):
                new = new.replace(form, make_portable.NEUTRAL_ROOT.replace("\\", "/"))
        if new != text:
            item.write_text(new, encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description="Архив выпуска Hagen для релиза на GitHub")
    ap.add_argument("--ref", default="HEAD", help="какой коммит выпускать")
    ap.add_argument("--out", default=str(PROJECT.parent / "hagen-release"))
    ap.add_argument("--full", action="store_true",
                    help="собрать ещё и полный архив (всё уже поставлено, без интернета)")
    args = ap.parse_args()

    t0 = time.time()
    out = Path(args.out)
    work = out / "_build"
    if work.exists():
        make_portable.remove_tree(work)
    light = build_light(args.ref, work / "light" / TOP)
    # Номер и опись — из снимка, а не из рабочей папки: выпускается коммит.
    ver = release.version(light / "release.json")
    if not release.parse_version(ver):
        raise SystemExit("в release.json коммита нет номера версии")
    if not lockfile.read(light / "lock.json"):
        raise SystemExit("в коммите нет описи lock.json — сначала tools\\make_lock.py")
    target = out / ("Hagen-v%s-windows.zip" % ver)
    say("упаковываю %s…" % target.name)
    make_portable.make_zip(light, target)
    say("готово: %s — %.0f МБ" % (target, target.stat().st_size / 1048576.0))
    if args.full:
        full = build_full(light, work / "full" / TOP)
        target = out / ("Hagen-v%s-windows-full.zip" % ver)
        say("упаковываю %s…" % target.name)
        make_portable.make_zip(full, target)
        say("готово: %s — %.1f ГБ" % (target, target.stat().st_size / 1073741824.0))
    make_portable.remove_tree(work)
    say("за %.0f мин" % ((time.time() - t0) / 60))
    return 0


if __name__ == "__main__":
    sys.exit(main())
