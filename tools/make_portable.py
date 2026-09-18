# -*- coding: utf-8 -*-
"""Собрать переносимый архив «Hagen» для другого компьютера.

    .venv\\Scripts\\python.exe tools\\make_portable.py [--out ПАПКА] [--no-zip] [--no-keys]
    .venv\\Scripts\\python.exe tools\\make_portable.py --for-tester --to "Иван Иванов"

Что попадает в архив:
  * код, проверки (без результатов прогонов), документы, пакет тем;
  * python\\ (переносимый Python) и .venv (библиотеки) — без лишнего: тестов
    внутри библиотек, заголовков C++, кэша байт-кода (см. install.prune_ignore);
  * быстрая модель распознавания, модели разметки говорящих, ffmpeg;
    тяжёлое (точная модель, английская, playwright) НЕ кладётся — оно качается
    по требованию из самой программы; вернуть в архив — ключ --all-models;
  * settings.json — с ключами (решение 13.09), но без устройств этого
    компьютера; с --no-keys — без токена и ключей (архив для других людей);

Ключ --for-tester — сборка для постороннего человека (решение 16.09).
Он включает --no-keys и вдобавок:
  * не берёт .git (вся история разработки) и .gitignore;
  * стирает личное в settings.json: имя владельца, путь к его сейфу Obsidian,
    свои замены с названиями компаний, инструкции документов, папки снимков;
  * выключает автозапуск — чужому компьютеру он прописывается без спроса;
  * вместо LICENSE кладёт «Условия передачи.txt»: кому, версия, дата, что
    можно и чего нельзя.
Защита кода этим не достигается: исходники Python лежат в архиве открытым
текстом, и иначе не бывает. Ограничение — письменное, а не техническое.
  * журнал версий .git — чтобы на новом месте работали git pull / push.

Что НЕ попадает: записи (data\\), журнал (logs\\), копии (_backup\\), hf_token.txt.

Архив кладётся рядом с папкой программы, в ..\\hagen-dist — не в сейф
Obsidian: он на Яндекс.Диске, и гигабайты уехали бы в облако.
"""
from __future__ import annotations

import argparse
import io
import json
import os
import shutil
import stat
import sys
import time
import zipfile
from datetime import datetime
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

import install  # noqa: E402  правила чистки общие с установщиком

TOP_FILES = ("run.py", "install.py", "requirements.txt", "constraints.txt", "Hagen.cmd",
             "Ustanovka.cmd", "Как устроен Hagen.md", ".gitignore", "README.md", "LICENSE",
             "CONTRIBUTING.md")
TOP_DIRS = ("hagen", "tools", "hagen-themes", "python", ".venv", "models", "ffmpeg", ".git")
TEST_KEEP_EXT = (".py", ".wav", ".mp4", ".json")
MACHINE_KEYS = ("mic_device_index", "far_device_index", "mic_device_name", "vault_confirmed")
#: Что вычищается из settings.json при --no-keys. yt_proxy — тоже секрет:
#: адрес прокси обычно содержит логин и пароль.
SECRET_KEYS = ("hf_token", "api_keys", "yt_proxy", "todoist_token")
#: Что не кладётся в архив: это качается по требованию (решение 16.09)
#: или просто не используется. Путь — относительно папки программы.
#: * точная модель (430 МБ) и английская (630 МБ) — качаются по кнопке;
#: * playwright (100 МБ) — ставится по кнопке, нужен только входу в SharePoint;
#: * v3_e2e_rnnt_*.onnx (850 МБ) — точная модель через ONNX; все вызовы идут
#:   через torch (words=True), этот путь не используется;
#: * v3_e2e_ctc.ckpt (420 МБ) — быстрая модель в формате torch, дубль ONNX-овой.
#: torch остаётся: без него не работает поиск речи (silero-vad) при каждой записи.
SKIP_HEAVY = (
    ("models", "gigaam", "v3_e2e_rnnt.ckpt"),
    ("models", "gigaam", "v3_e2e_rnnt_tokenizer.model"),
    ("models", "gigaam", "v3_e2e_ctc.ckpt"),
    ("models", "gigaam", "v3_e2e_ctc_tokenizer.model"),
    ("models", "onnx", "v3_e2e_rnnt_encoder.onnx"),
    ("models", "onnx", "v3_e2e_rnnt_decoder.onnx"),
    ("models", "onnx", "v3_e2e_rnnt_joint.onnx"),
    ("models", "onnx", "v3_e2e_rnnt.yaml"),
    ("models", "onnx-asr",),
    (".venv", "Lib", "site-packages", "playwright"),
)

#: Что заменяется в settings.json при --for-tester: по этим полям видно, кто и
#: где работает, а автозапуск чужому человеку прописывать без спроса нельзя.
TESTER_SETTINGS = {
    "owner_name": "Я",
    "vault_path": "",
    "vault_subfolder": "Meetings",
    "vault_confirmed": False,
    "dictate_replacements": [],
    "prompt_overrides": {},
    "todoist_project": {},
    "screenshot_folders": [],
    "assets_dir": "",
    "autostart_windows": False,
    "categories": ["Встречи", "Звонки", "Заметки"],
    "hidden_categories": [],
    "default_category": "Встречи",
}
#: Файлы внутри копируемых папок, которым в чужом архиве не место.
SECRET_FILES = {("models", "hf", "token")}
STORE_EXT = (".onnx", ".ckpt", ".bin", ".safetensors", ".pt", ".zip", ".mp4", ".wav", ".exe", ".dll", ".pyd")


def say(msg: str) -> None:
    print(msg, flush=True)


def remove_tree(path: Path) -> None:
    """Удалить прежнюю сборку. Файлы .git лежат «только для чтения» — снимаем пометку."""
    def force(func, name, _exc):
        os.chmod(name, stat.S_IWRITE)
        func(name)
    shutil.rmtree(path, onexc=force)


def ignore_for(root: Path, lean: bool = True):
    """Что не копировать. ``lean`` — без тяжёлого, которое качается по требованию."""
    def ignore(src: str, names: list[str]) -> set[str]:
        skip = install.prune_ignore(src, names, root)
        here = Path(src)
        if here == root / "hagen" or "hagen" in here.relative_to(root).parts[:1]:
            skip |= {n for n in names if n == "__pycache__"}
        rel = here.relative_to(root).parts
        skip |= {n for n in names if tuple(rel) + (n,) in SECRET_FILES}
        if lean:
            skip |= {n for n in names if tuple(rel) + (n,) in SKIP_HEAVY}
        return skip
    return ignore


def copy_tests(dst: Path) -> None:
    src = PROJECT / "tests"
    (dst / "tests").mkdir(parents=True, exist_ok=True)
    for item in src.iterdir():
        if item.is_file() and item.suffix.lower() in TEST_KEEP_EXT and not item.name.endswith("_result.txt"):
            shutil.copy2(item, dst / "tests" / item.name)


def settings_for(dst: Path, keys: bool, tester: bool = False, src: Path | None = None) -> None:
    """Настройки для архива. ``src`` задаётся проверками, чтобы не читать настоящие."""
    src = src or (PROJECT / "settings.json")
    if not src.exists():
        return
    data = json.load(io.open(src, encoding="utf-8"))
    for k in MACHINE_KEYS:
        data.pop(k, None)
    if not keys:
        for k in SECRET_KEYS:
            data.pop(k, None)
    if tester:
        # Имя владельца, путь к его сейфу, замены с названиями компаний — по ним
        # видно, кто и где работает. Автозапуск чужому человеку не прописываем.
        data.update(TESTER_SETTINGS)
    with io.open(dst / "settings.json", "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)


def build_version() -> str:
    """Короткий номер сборки из .git, без запуска git (Kaspersky не любит процессы)."""
    head = PROJECT / ".git" / "HEAD"
    try:
        ref = head.read_text(encoding="utf-8").strip()
        if ref.startswith("ref:"):
            ref_path = PROJECT / ".git" / ref.split(" ", 1)[1].strip()
            return ref_path.read_text(encoding="utf-8").strip()[:7]
        return ref[:7]
    except OSError:
        return "без номера"


TERMS = """Hagen — условия передачи
==========================

Кому передано: {to}
Версия сборки: {version}, {date}
Правообладатель: @prankhitevil

Что можно
---------
* Установить эту сборку на свои компьютеры и пользоваться ею.
* Записывать свои встречи, делать стенограммы и документы.
* Показывать результаты работы программы кому угодно.
* Сообщать замечания и пожелания правообладателю.

Чего нельзя без письменного разрешения правообладателя
------------------------------------------------------
* Передавать сборку, её части или исходный код третьим лицам.
* Публиковать исходный код и выкладывать сборку в открытый доступ.
* Продавать программу или услуги на её основе.
* Выдавать программу за свою разработку.

Про исходный код
----------------
Программа написана на Python, и её исходные файлы лежат внутри папки открытым
текстом — это обычное свойство таких программ, а не недосмотр. Поэтому
передача ограничена этим документом, а не техническими средствами.

Про данные
----------
Записи, стенограммы и документы остаются на компьютере получателя. Программа
ничего никуда не отправляет сама. Облачные модели подключаются только своим
ключом получателя и только по его команде.

Что нужно получателю сверх архива
---------------------------------
* Свой токен HuggingFace (huggingface.co/settings/tokens) и принятые условия
  моделей pyannote — без этого не работает разметка говорящих. Токен
  вводится в «Настройки → Голоса».
* Claude CLI или свой ключ облачной модели — только для документов
  (протокол, саммари, конспект). Запись и стенограмма работают без них.

Вопросы и замечания: {to_contact}
"""


README = """Hagen — переносимая папка
=============================

1. Распакуйте архив в постоянное место, например C:\\Apps\\Hagen
   (не в Яндекс.Диск и не на рабочий стол).
2. Запустите Ustanovka.cmd. Он ничего не скачивает: исправит пути, создаст ярлык
   «Hagen», спросит папку хранилища Obsidian и напечатает файлы для антивируса.
3. Антивирус (Kaspersky): доверенные программы по путям, которые напечатает
   установщик, — python\\python.exe и ffmpeg\\bin\\ffmpeg.exe, ffprobe.exe.
4. Для документов по подписке — Claude CLI: установить и войти отдельно.
5. Часть тяжёлого в архив не входит и качается по кнопке, когда понадобится:
   точная модель распознавания (430 МБ), английская модель (630 МБ), браузер
   для входа в SharePoint (100 МБ). Программа спросит перед скачиванием;
   список — «Настройки → Модели». Запись, живая стенограмма и разметка
   говорящих работают сразу, без докачки.
6. Для разметки говорящих нужен свой токен HuggingFace и принятые условия
   моделей pyannote — «Настройки → Голоса». Без него запись и стенограмма
   работают, а имена говорящих не расставляются.

Если папку потом перенести ещё раз — программа сама исправит пути при запуске.
Подробнее — «Как устроен Hagen.md».
"""


def count(path: Path) -> tuple[int, int]:
    n = b = 0
    for cur, _dirs, files in os.walk(path):
        for f in files:
            try:
                b += os.path.getsize(os.path.join(cur, f))
                n += 1
            except OSError:
                pass
    return n, b


def make_zip(stage: Path, target: Path) -> None:
    tmp = target.with_suffix(".zip.part")
    base = stage.parent
    done = 0
    with zipfile.ZipFile(tmp, "w", allowZip64=True) as zf:
        for cur, dirs, files in os.walk(stage):
            dirs.sort()
            for f in sorted(files):
                full = Path(cur) / f
                arc = full.relative_to(base).as_posix()
                method = zipfile.ZIP_STORED if full.suffix.lower() in STORE_EXT else zipfile.ZIP_DEFLATED
                zf.write(full, arc, compress_type=method, compresslevel=6 if method else None)
                done += 1
                if done % 5000 == 0:
                    say("  в архиве файлов: %d" % done)
    os.replace(tmp, target)


def main() -> int:
    ap = argparse.ArgumentParser(description="Переносимый архив Hagen")
    ap.add_argument("--out", default=str(PROJECT.parent / "hagen-dist"))
    ap.add_argument("--no-zip", action="store_true", help="только папка, без архива")
    ap.add_argument("--no-keys", action="store_true", help="без токена и ключей (для других людей)")
    ap.add_argument("--for-tester", action="store_true",
                    help="сборка для постороннего: без ключей, без .git, без личных настроек, "
                         "с условиями передачи вместо лицензии")
    ap.add_argument("--to", default="", help="кому передаётся (попадёт в условия передачи)")
    ap.add_argument("--all-models", action="store_true",
                    help="положить в архив и то, что обычно качается по требованию "
                         "(точная и английская модели, playwright) — папка вырастет на 1,2 ГБ")
    args = ap.parse_args()
    if args.for_tester:
        args.no_keys = True

    t0 = time.time()
    out = Path(args.out)
    stage = out / "Hagen"
    if stage.exists():
        say("убираю прежнюю сборку: %s" % stage)
        remove_tree(stage)
    stage.mkdir(parents=True)

    files = [f for f in TOP_FILES if not (args.for_tester and f in ("LICENSE", ".gitignore"))]
    # .git — это вся история разработки; постороннему она не передаётся.
    dirs = [d for d in TOP_DIRS if not (args.for_tester and d == ".git")]
    for name in files:
        if (PROJECT / name).exists():
            shutil.copy2(PROJECT / name, stage / name)
    for name in dirs:
        src = PROJECT / name
        if src.exists():
            say("копирую %s…" % name)
            shutil.copytree(src, stage / name, ignore=ignore_for(PROJECT, lean=not args.all_models))
    copy_tests(stage)
    settings_for(stage, keys=not args.no_keys, tester=args.for_tester)
    with io.open(stage / "КАК УСТАНОВИТЬ.txt", "w", encoding="utf-8-sig", newline="\r\n") as fh:
        fh.write(README)
    if args.for_tester:
        terms = TERMS.format(to=args.to or "________________________",
                             version=build_version(),
                             date=datetime.now().strftime("%d.%m.%Y"),
                             to_contact="Иван Петров")
        with io.open(stage / "Условия передачи.txt", "w", encoding="utf-8-sig", newline="\r\n") as fh:
            fh.write(terms)
        say("условия передачи записаны: кому — %s" % (args.to or "не указано, впишите в файл"))

    if not args.all_models:
        say("тяжёлое не кладу — оно качается по кнопке: точная модель, английская, playwright")
    n, b = count(stage)
    say("папка готова: %s — файлов %d, %.1f ГБ (%.0f мин)" % (stage, n, b / 1073741824.0, (time.time() - t0) / 60))
    if not args.no_zip:
        mark = "-для-проверки" if args.for_tester else ("-без-ключей" if args.no_keys else "")
        target = out / ("Hagen-portable-%s%s.zip" % (datetime.now().strftime("%Y-%m-%d"), mark))
        say("собираю архив %s…" % target.name)
        make_zip(stage, target)
        say("архив готов: %s — %.1f ГБ (%.0f мин всего)" % (target, target.stat().st_size / 1073741824.0,
                                                           (time.time() - t0) / 60))
    return 0


if __name__ == "__main__":
    sys.exit(main())
