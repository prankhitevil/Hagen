# -*- coding: utf-8 -*-
"""Собрать переносимый архив «Hagen» для другого компьютера.

    .venv\\Scripts\\python.exe tools\\make_portable.py [--out ПАПКА] [--no-zip] [--no-keys]
    .venv\\Scripts\\python.exe tools\\make_portable.py --for-tester --to "Иван Иванов"
    .venv\\Scripts\\python.exe tools\\make_portable.py --diarize pyannote

Движок разметки говорящих выбирает ключ --diarize (по умолчанию — как в
release.json этой папки), и он же пишется в release.json сборки:
  * onnx — модель лежит в models\\diar, токен не нужен; в сборку не кладутся
    ни кэш моделей pyannote, ни библиотеки, которые нужны только ему (их
    список считается по зависимостям пакетов, а не вручную);
  * pyannote — как раньше: pyannote.audio и его модели в сборке, получателю
    нужен свой токен Hugging Face.

Что попадает в архив:
  * код, проверки (без результатов прогонов), документы, пакет тем;
  * python\\ (переносимый Python) и .venv (библиотеки) — без лишнего: тестов
    внутри библиотек, заголовков C++, кэша байт-кода (см. install.prune_ignore);
  * одна модель распознавания — рекомендованная точная (asr.recommended_part),
    модели разметки говорящих, ffmpeg; прочие модели распознавания,
    английская и playwright НЕ кладутся — они качаются по требованию из самой
    программы; вернуть в архив — ключ --all-models;
  * settings.json — с ключами (решение 13.09), но без устройств этого
    компьютера; с --no-keys — без токена и ключей (архив для других людей);

Ключ --for-tester — сборка для постороннего человека (решение 16.09).
Он включает --no-keys и вдобавок:
  * не берёт .git (вся история разработки) и .gitignore;
  * стирает личное в settings.json: имя владельца, путь к его сейфу Obsidian,
    свои замены с названиями компаний, инструкции документов, папки снимков;
  * ставит рекомендованный выбор моделей — в архиве лежит только такая;
  * выключает автозапуск — чужому компьютеру он прописывается без спроса;
  * вместо LICENSE кладёт «Условия передачи.txt»: кому, версия, дата, что
    можно и чего нельзя.
Защита кода этим не достигается: исходники Python лежат в архиве открытым
текстом, и иначе не бывает. Ограничение — письменное, а не техническое.
  * журнал версий .git — чтобы на новом месте работали git pull / push.

Ключ --public — сборка для открытого релиза на GitHub. Как --for-tester, но
лицензия остаётся GPL (никаких «Условий передачи»: они запрещали бы то, что
GPL разрешает), а настроек владельца в сборке нет вовсе — программа начнёт с
заводских. --version v0.9 даёт архиву имя «Hagen-v0.9-windows.zip».

Что НЕ попадает никогда: записи (data\\), журнал (logs\\), копии (_backup\\),
hf_token.txt, файлы с «.local.» в имени (закрыты от git: например, список
личного для проверки витрины), следы прогонов проверок, журналы и кэш загрузок
моделей (models\\hf\\xet), скрипты activate из .venv. Пути этой машины в
.venv\\pyvenv.cfg заменяются нейтральными — на новом месте их переписывает
установщик.

Сборка для других людей (--for-tester, --public) перед упаковкой проверяется
тем же списком личного и ключей, что и витрина (tools/make_mirror.py):
нашлось хоть что-то — архив не собирается.

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

TOP_FILES = ("run.py", "install.py", "requirements.txt", "requirements-pyannote.txt",
             "lock.json", "Hagen.cmd", "Ustanovka.cmd", "Как устроен Hagen.md",
             ".gitignore", "README.md", "LICENSE", "CONTRIBUTING.md")
ENGINES = ("onnx", "pyannote")
TOP_DIRS = ("hagen", "tools", "hagen-themes", "vendor", "python", ".venv", "models", "ffmpeg",
            ".git")
#: Метка в имени файла, закрытого от git и живущего только на этой машине.
LOCAL_MARK = ".local."
#: Куда в чужой сборке смотрят .venv\pyvenv.cfg и скрипты .venv\Scripts вместо
#: путей этой машины.
NEUTRAL_ROOT = "C:\\Hagen"
#: Чужие программы и библиотеки в сборке. Их проверяют только на следы этой
#: машины: в них полно чужих адресов и случайных совпадений со списком личного.
RUNTIME_DIRS = (".venv", "python", "ffmpeg")
#: Что относится к этому компьютеру. asr_keep_parts и asr_pending_delete — что
#: здесь оставить и что удалить при запуске: на новом месте другие файлы.
MACHINE_KEYS = ("mic_device_index", "far_device_index", "mic_device_name", "vault_confirmed",
                "asr_keep_parts", "asr_pending_delete")
#: Что вычищается из settings.json при --no-keys. yt_proxy — тоже секрет:
#: адрес прокси обычно содержит логин и пароль.
SECRET_KEYS = ("hf_token", "api_keys", "yt_proxy", "todoist_token")
#: Что не кладётся в архив, кроме моделей распознавания (о них — model_skips):
#: это качается по требованию (решение 16.09). Путь — относительно папки
#: программы.
#: * playwright (100 МБ) — ставится по кнопке, нужен только входу в SharePoint.
#: torch остаётся: без него не работает поиск речи (silero-vad) при каждой записи.
SKIP_HEAVY = (
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
#: Файлы внутри копируемых папок, которым в архиве не место: токен, журналы и
#: кэш загрузок с путями этой машины, копия pyvenv.cfg, скрипты activate
#: (программа ими не пользуется, а путь к .venv в них — этой машины).
SECRET_FILES = {("models", "hf", "token"), ("models", "hf", "xet"),
                (".venv", "pyvenv.cfg.bak")} | {
    (".venv", "Scripts", n) for n in ("activate", "activate.bat", "Activate.ps1",
                                      "deactivate.bat", "activate.fish", "activate.nu",
                                      "activate.csh")}
STORE_EXT = (".onnx", ".ckpt", ".bin", ".safetensors", ".pt", ".zip", ".mp4", ".wav", ".exe", ".dll", ".pyd")


def say(msg: str) -> None:
    print(msg, flush=True)


def remove_tree(path: Path) -> None:
    """Удалить прежнюю сборку. Файлы .git лежат «только для чтения» — снимаем пометку."""
    def force(func, name, _exc):
        os.chmod(name, stat.S_IWRITE)
        func(name)
    shutil.rmtree(path, onexc=force)


def model_skips() -> set[tuple[str, ...]]:
    """Файлы моделей распознавания, которые в архив не кладутся (решение 21.09).

    Кладётся одна рекомендованная точная модель (asr.recommended_part): на ней
    программа сразу, без интернета, распознаёт всё — звонки, голосовой ввод,
    файлы. Остальное — быстрая модель, torch, вторые веса onnx-asr, английская,
    неиспользуемые файлы — качается кнопкой «Скачать нужное». Списки файлов те
    же, по которым программа сама считает и удаляет модели (needs.part_files).
    """
    from hagen import asr, config, needs

    keep = set(needs.part_files(asr.recommended_part()))
    out: set[tuple[str, ...]] = set()
    for key in needs.MODEL_PARTS:
        for path in needs.part_files(key):
            if path not in keep:
                out.add(path.relative_to(config.PROJECT_DIR).parts)
    return out


def _dist_name(requirement: str) -> str:
    """«pyannote.audio==4.0.7; extra == …» → «pyannote-audio»."""
    import re

    head = re.split(r"[ ;<>=!~\[(@]", requirement.strip(), maxsplit=1)[0]
    return re.sub(r"[-_.]+", "-", head).lower()


def _closure(roots: list[str]) -> set[str]:
    """Пакеты вместе со всеми обязательными зависимостями (без необязательных extra)."""
    from importlib import metadata

    seen: set[str] = set()
    todo = [_dist_name(r) for r in roots]
    while todo:
        name = todo.pop()
        if name in seen:
            continue
        try:
            reqs = metadata.requires(name) or []
        except metadata.PackageNotFoundError:
            continue
        seen.add(name)
        todo.extend(_dist_name(r) for r in reqs if "extra ==" not in r and "extra==" not in r)
    return seen


def pyannote_skips(root: Path = PROJECT) -> set[tuple[str, ...]]:
    """Что не кладётся в сборку с разметкой ONNX: всё, что нужно только pyannote.

    Библиотеки — те, что тянет pyannote.audio и не тянет ничто из
    requirements.txt (то есть чего не было бы при чистой установке релиза
    ONNX). Считается по метаданным пакетов этого окружения: список сам
    следует за версиями. Плюс модели pyannote в кэше Hugging Face.
    """
    from importlib import metadata

    keep_roots = ["torch", "torchaudio", "pip", "setuptools", "wheel"]
    for line in io.open(root / "requirements.txt", encoding="utf-8"):
        line = line.split("#", 1)[0].strip()
        if line and not line.startswith("-"):
            keep_roots.append(line)
    keep = _closure(keep_roots)
    drop = _closure(["pyannote.audio"]) - keep

    def entries(dists: set[str]) -> tuple[set[str], set[str]]:
        """Верхние имена в site-packages и лаунчеры в Scripts у этих пакетов."""
        tops: set[str] = set()
        scripts: set[str] = set()
        for name in dists:
            try:
                files = metadata.distribution(name).files or []
            except metadata.PackageNotFoundError:
                continue
            for f in files:
                parts = Path(str(f)).parts
                if parts[:2] == ("..", ".."):
                    if len(parts) > 3 and parts[2] == "Scripts":
                        scripts.add(parts[3])
                elif parts:
                    tops.add(parts[0])
        return tops, scripts

    others = {_dist_name(d.metadata["Name"]) for d in metadata.distributions()} - drop
    drop_tops, drop_scripts = entries(drop)
    keep_tops, keep_scripts = entries(others)
    site = (".venv", "Lib", "site-packages")
    out = {site + (t,) for t in drop_tops - keep_tops}
    out |= {(".venv", "Scripts", s) for s in drop_scripts - keep_scripts}
    for sub in ("hub", "hub/.locks"):
        folder = root / "models" / "hf" / sub
        if folder.is_dir():
            out |= {("models", "hf") + tuple(sub.split("/")) + (p.name,)
                    for p in folder.iterdir() if p.name.startswith("models--pyannote--")}
    return out


def ignore_for(root: Path, lean: bool = True, engine: str = "onnx"):
    """Что не копировать. ``lean`` — без тяжёлого, которое качается по требованию;
    ``engine`` — движок разметки сборки: у onnx не нужно ничего от pyannote."""
    heavy = set(SKIP_HEAVY) | model_skips() if lean else set()
    if engine == "onnx":
        heavy |= pyannote_skips()

    def ignore(src: str, names: list[str]) -> set[str]:
        skip = install.prune_ignore(src, names, root)
        here = Path(src)
        if here == root / "hagen" or "hagen" in here.relative_to(root).parts[:1]:
            skip |= {n for n in names if n == "__pycache__"}
        rel = here.relative_to(root).parts
        skip |= {n for n in names if tuple(rel) + (n,) in SECRET_FILES}
        skip |= {n for n in names if tuple(rel) + (n,) in heavy}
        # закрытое от git по имени — только для этой машины; в .venv таких нет
        if rel[:1] != (".venv",):
            skip |= {n for n in names if LOCAL_MARK in n}
        return skip
    return ignore


def test_patterns(root: Path = PROJECT) -> list[str]:
    """Что из tests\\ идёт в сборку: то же, что под git, — разрешения «!tests/…»
    из .gitignore. Один список на оба места: следы прогонов (*_result.txt,
    t8_rec.json и прочее) не попадут ни в git, ни в сборку."""
    out: list[str] = []
    for line in io.open(root / ".gitignore", encoding="utf-8"):
        line = line.strip()
        if line.startswith("!tests/"):
            out.append(line[len("!tests/"):])
    return out or ["*.py"]


def copy_tests(dst: Path) -> None:
    import fnmatch

    src = PROJECT / "tests"
    (dst / "tests").mkdir(parents=True, exist_ok=True)
    keep = test_patterns()
    for item in src.iterdir():
        if item.is_file() and any(fnmatch.fnmatch(item.name, p) for p in keep):
            shutil.copy2(item, dst / "tests" / item.name)


def machine_paths() -> list[str]:
    """Пути этой машины, которые могут остаться в файлах сборки: папка программы
    и папка, где на самом деле лежит .venv (в рабочей копии это ссылка)."""
    out = {str(PROJECT), str((PROJECT / ".venv").resolve().parent)}
    return sorted(out, key=len, reverse=True)


def neutral_venv(stage: Path, paths: list[str] | None = None) -> None:
    """Пути этой машины в .venv — на нейтральную папку.

    В pyvenv.cfg строку home на новом месте переписывают установщик и сама
    программа при запуске (portable.fix_venv_cfg); остальные строки справочные.
    Скрипты пакетов в .venv\\Scripts несут в первой строке путь к python.exe этой
    машины; программа их не запускает. ``paths`` — какие пути считать путями
    этой машины (по умолчанию — machine_paths()).
    """
    import re

    cfg = stage / ".venv" / "pyvenv.cfg"
    if cfg.exists():
        python = NEUTRAL_ROOT + "\\python"
        lines = []
        for line in io.open(cfg, encoding="utf-8").read().splitlines():
            key = line.split("=", 1)[0].strip().lower()
            if key == "home":
                line = "home = %s" % python
            elif key == "executable":
                line = "executable = %s\\python.exe" % python
            elif key == "command":
                line = "command = %s\\python.exe -m venv %s\\.venv" % (python, NEUTRAL_ROOT)
            lines.append(line)
        with io.open(cfg, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("\n".join(lines) + "\n")

    scripts = stage / ".venv" / "Scripts"
    if not scripts.is_dir():
        return
    paths = [re.compile(re.escape(p), re.IGNORECASE) for p in (paths or machine_paths())]
    for item in scripts.iterdir():
        if not item.is_file() or item.suffix.lower() in (".exe", ".dll", ".pyd"):
            continue
        try:
            with io.open(item, encoding="utf-8", newline="") as fh:
                text = fh.read()
        except (UnicodeDecodeError, OSError):
            continue
        new = text
        for pat in paths:
            new = pat.sub(lambda _m: NEUTRAL_ROOT, new)
        if new != text:
            with io.open(item, "w", encoding="utf-8", newline="") as fh:
                fh.write(new)


def check_stage(stage: Path) -> list[str]:
    """Проверить сборку перед тем, как отдать её другим людям.

    Свой код, модели и документы — тем же списком личного и ключей, что и
    витрину (tools/make_mirror.py). Чужие программы и библиотеки (RUNTIME_DIRS)
    — только на следы этой машины: имя домашней папки и путь к программе. Смотрит
    все файлы, кроме весов моделей и звука: путь с именем пользователя прячется и
    в журналах, и в скриптах, а не только в текстах. Возвращает «путь — что
    нашлось»; пусто — сборку можно отдавать.
    """
    import re

    sys.path.insert(0, str(PROJECT / "tools"))
    import make_mirror

    words, allowed = make_mirror.load_check_list()
    machine = [re.compile(re.escape(Path.home().name), re.IGNORECASE)]
    machine += [re.compile(re.escape(p), re.IGNORECASE) for p in machine_paths()]
    own = list(make_mirror.SECRETS) + words + machine
    skip_ext = {".onnx", ".npz", ".bin", ".safetensors", ".ckpt", ".pt", ".wav", ".mp4",
                ".zip", ".dll", ".pyd", ".lib", ".so"}
    bad: list[str] = []
    for cur, _dirs, files in os.walk(stage):
        for name in files:
            path = Path(cur) / name
            if path.suffix.lower() in skip_ext or path.stat().st_size > 60 * 1048576:
                continue
            rel = path.relative_to(stage).as_posix()
            text = path.read_bytes().decode("utf-8", "ignore")
            for pat in (machine if rel.split("/", 1)[0] in RUNTIME_DIRS else own):
                for m in pat.finditer(text):
                    line = text[text.rfind("\n", 0, m.start()) + 1:text.find("\n", m.end())]
                    if any(rel == a_path and a_piece in line for a_path, a_piece in allowed):
                        continue
                    bad.append("%s — «%s»" % (rel, m.group(0)[:40]))
                    break
    return bad


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
        # Модели — рекомендованные: другой в архиве нет (model_skips).
        from hagen import asr

        data.update(asr.RECOMMENDED)
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
{diarize_need}* Claude CLI или свой ключ облачной модели — только для документов
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
5. В архиве одна модель распознавания — точная, она распознаёт всё: звонки,
   голосовой ввод, файлы. Остальное качается по кнопке, когда понадобится:
   быстрая модель (440 МБ), английская (630 МБ), браузер для входа в
   SharePoint (100 МБ). Программа спросит перед скачиванием; выбор моделей —
   «Настройки → Модели». Запись, стенограмма и разметка говорящих работают
   сразу, без докачки.
{diarize}
Если папку потом перенести ещё раз — программа сама исправит пути при запуске.
Подробнее — «Как устроен Hagen.md».
"""

#: Что сказать про разметку говорящих — зависит от движка сборки.
DIARIZE_README = {
    "pyannote": ("6. Для разметки говорящих нужен свой токен HuggingFace и принятые условия\n"
                 "   моделей pyannote — «Настройки → Голоса». Без него запись и стенограмма\n"
                 "   работают, а имена говорящих не расставляются.\n"),
    "onnx": ("6. Разметка говорящих работает сразу: её модель лежит в папке, ни токен,\n"
             "   ни интернет для неё не нужны.\n"),
}
DIARIZE_TERMS = {
    "pyannote": ("* Свой токен HuggingFace (huggingface.co/settings/tokens) и принятые условия\n"
                 "  моделей pyannote — без этого не работает разметка говорящих. Токен\n"
                 "  вводится в «Настройки → Голоса».\n"),
    "onnx": "",
}


def readme_for(engine: str) -> str:
    """Памятка «КАК УСТАНОВИТЬ» для сборки с этим движком разметки."""
    return README.format(diarize=DIARIZE_README[engine])


def terms_for(engine: str, **fields: str) -> str:
    """«Условия передачи» для сборки с этим движком разметки."""
    return TERMS.format(diarize_need=DIARIZE_TERMS[engine], **fields)


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
                         "(все модели распознавания, английская, playwright)")
    ap.add_argument("--diarize", choices=ENGINES, default=None,
                    help="движок разметки говорящих сборки (по умолчанию — из release.json)")
    ap.add_argument("--public", action="store_true",
                    help="сборка для открытого релиза: без ключей, без .git, без настроек "
                         "владельца, с лицензией GPL")
    ap.add_argument("--version", default="", help="номер релиза в имени архива, например v0.9")
    args = ap.parse_args()
    if args.for_tester:
        args.no_keys = True
    if args.public:
        args.no_keys = True
    # сборка уходит другим людям: без истории разработки и под проверкой личного
    outside = args.for_tester or args.public
    if args.diarize is None:
        from hagen import diarize

        args.diarize = diarize.release_engine()
    say("разметка говорящих в сборке: %s" % args.diarize)

    t0 = time.time()
    out = Path(args.out)
    stage = out / "Hagen"
    if stage.exists():
        say("убираю прежнюю сборку: %s" % stage)
        remove_tree(stage)
    stage.mkdir(parents=True)

    files = [f for f in TOP_FILES if not (args.for_tester and f in ("LICENSE", ".gitignore"))
             and not (args.public and f == ".gitignore")]
    # .git — это вся история разработки; постороннему она не передаётся.
    dirs = [d for d in TOP_DIRS if not (outside and d == ".git")]
    for name in files:
        if (PROJECT / name).exists():
            shutil.copy2(PROJECT / name, stage / name)
    for name in dirs:
        src = PROJECT / name
        if src.exists():
            say("копирую %s…" % name)
            shutil.copytree(src, stage / name,
                            ignore=ignore_for(PROJECT, lean=not args.all_models, engine=args.diarize))
    copy_tests(stage)
    neutral_venv(stage)
    # В открытом релизе чужих настроек нет вовсе: программа начнёт с заводских.
    if not args.public:
        settings_for(stage, keys=not args.no_keys, tester=args.for_tester)
    from hagen import release

    info = dict(release.read(), diarize=args.diarize)   # номер версии и адрес обновлений — как есть
    with io.open(stage / "release.json", "w", encoding="utf-8") as fh:
        json.dump(info, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    with io.open(stage / "КАК УСТАНОВИТЬ.txt", "w", encoding="utf-8-sig", newline="\r\n") as fh:
        fh.write(readme_for(args.diarize))
    if args.for_tester:
        terms = terms_for(args.diarize,
                          to=args.to or "________________________",
                          version=build_version(),
                          date=datetime.now().strftime("%d.%m.%Y"),
                          to_contact="Иван Петров")
        with io.open(stage / "Условия передачи.txt", "w", encoding="utf-8-sig", newline="\r\n") as fh:
            fh.write(terms)
        say("условия передачи записаны: кому — %s" % (args.to or "не указано, впишите в файл"))

    if not args.all_models:
        say("тяжёлое не кладу — оно качается по кнопке: прочие модели распознавания, "
            "английская, playwright")
    n, b = count(stage)
    say("папка готова: %s — файлов %d, %.1f ГБ (%.0f мин)" % (stage, n, b / 1073741824.0, (time.time() - t0) / 60))
    if outside:
        say("проверяю сборку на личное и ключи…")
        bad = check_stage(stage)
        if bad:
            say("СБОРКА НЕ ОТДАЁТСЯ: нашлось то, чему в чужих руках не место.")
            for line in bad[:60]:
                say("   " + line)
            if len(bad) > 60:
                say("   … и ещё %d" % (len(bad) - 60))
            say("папка оставлена для разбора: %s" % stage)
            return 1
        say("проверка на личное и ключи: чисто")
    if not args.no_zip:
        mark = "-для-проверки" if args.for_tester else ("-без-ключей" if args.no_keys else "")
        if args.public and args.version:
            name = "Hagen-%s-windows.zip" % args.version
        else:
            name = "Hagen-portable-%s-%s%s.zip" % (datetime.now().strftime("%Y-%m-%d"),
                                                  args.diarize, mark)
        target = out / name
        say("собираю архив %s…" % target.name)
        make_zip(stage, target)
        say("архив готов: %s — %.1f ГБ (%.0f мин всего)" % (target, target.stat().st_size / 1073741824.0,
                                                           (time.time() - t0) / 60))
    return 0


if __name__ == "__main__":
    sys.exit(main())
