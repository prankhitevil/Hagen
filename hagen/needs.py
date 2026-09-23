# -*- coding: utf-8 -*-
"""Тяжёлые части, которые качаются по требованию, а не лежат в сборке (16.09).

Переносимая папка весит гигабайты, и большая часть веса — то, чем пользуются не
все и не сразу: вторая модель распознавания, английская модель, браузер для
входа в SharePoint. Решение 16.09: их в сборку не класть, а качать
тогда, когда человек впервые нажал кнопку, которой они нужны, — с вопросом
«нужно скачать столько-то, качать?».

Что здесь есть:
  * список частей с весом и проверкой «уже на месте ли»;
  * скачивание — в этом же процессе (Kaspersky не даёт порождать фоновые) и с
    рассказом о ходе работы словами;
  * модели распознавания как набор частей под выбор в «Настройки → Модели»
    (решение 21.09): чего не хватает выбору, что ему не нужно и может быть
    удалено, «Сбросить всё».

Чего здесь нет: torch. Он программе больше не нужен (решение 23.09): поиск
речи, обе русские модели, английская и разметка голосов считаются на
onnxruntime. Все модели распознавания — готовый ONNX с Hugging Face, ничего
не переводится на этом компьютере.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Callable

from . import config

log = logging.getLogger("hagen.needs")

Note = Callable[[str], None]

#: Где прежние версии программы держали свои модели: веса для torch и файлы,
#: переведённые в ONNX на этом же компьютере. Программа их больше не читает,
#: но умеет посчитать и удалить (часть «unused»).
OLD_CKPT_DIR = config.MODELS_DIR / "gigaam"
OLD_ONNX_DIR = config.MODELS_DIR / "onnx"


class NeedError(RuntimeError):
    """Скачать не вышло, и человеку надо сказать почему."""


def _say(note: Note | None, msg: str) -> None:
    log.info(msg)
    if note:
        try:
            note(msg)
        except Exception:
            pass


# ------------------------------------------------- модели GigaAM для onnx-asr


def _ox_ready(engine: str) -> bool:
    from . import asr

    try:
        return bool(asr.ox_available(engine)[0])
    except Exception:
        return False


def _ox_install(engine: str, size_mb: int) -> Callable[..., dict[str, Any]]:
    """Скачать файлы движка: быстрая модель либо точная с полными или сжатыми весами.

    Сеть к Hugging Face включается на время скачивания: если на диске уже есть
    другие модели, программа работает с ним без сети (config.hf_online).
    """
    def run(note: Note | None = None) -> dict[str, Any]:
        from huggingface_hub import snapshot_download

        from . import asr, lockfile

        title = asr.ENGINE_TITLES[engine]
        _say(note, "Скачиваю модель распознавания — %s (около %d МБ)…" % (title, size_mb))
        try:
            with config.hf_online():
                snapshot_download(repo_id=asr.OX_REPO, local_dir=str(asr.OX_DIR),
                                  revision=lockfile.model_revision(asr.OX_REPO),
                                  allow_patterns=asr.ox_files(engine))
        except Exception as err:
            raise NeedError(
                "Модель распознавания скачать не вышло: %s\n"
                "Нужен интернет и доступ к huggingface.co; в корпоративной сети "
                "скачивание могут закрывать." % err
            ) from err
        _say(note, "Модель распознавания (%s) на месте." % title)
        return {"ready": _ox_ready(engine)}
    return run


# ------------------------------------------------------------ английская модель


def english_ready() -> bool:
    from . import asr

    try:
        return bool(asr.english_available()[0])
    except Exception:
        return False


def english_install(note: Note | None = None) -> dict[str, Any]:
    """Скачать Parakeet TDT: четыре файла из репозитория HuggingFace."""
    from huggingface_hub import snapshot_download

    from . import asr, lockfile

    _say(note, "Скачиваю английскую модель Parakeet (около 630 МБ)…")
    try:
        with config.hf_online():
            snapshot_download(
                repo_id=asr.EN_REPO,
                local_dir=str(asr.EN_DIR),
                revision=lockfile.model_revision(asr.EN_REPO),
                allow_patterns=["*int8.onnx", "nemo128.onnx", "vocab.txt", "config.json"],
            )
    except Exception as err:
        raise NeedError(
            "Английскую модель скачать не вышло: %s\n"
            "Нужен интернет и доступ к huggingface.co." % err
        ) from err
    _say(note, "Английская модель на месте.")
    return {"ready": english_ready()}


# ------------------------------------------------ браузер для входа в SharePoint


def browser_ready() -> bool:
    try:
        import playwright  # noqa: F401
    except Exception:
        return False
    return True


def browser_install(note: Note | None = None) -> dict[str, Any]:
    """Поставить playwright библиотекой, как обновление yt-dlp: своим процессом."""
    from . import fetch

    _say(note, "Ставлю playwright (около 100 МБ)…")
    fetch.pip_install(["playwright"], note=note)
    if not browser_ready():
        raise NeedError("playwright поставился, но не читается — перезапустите программу.")
    _say(note, "Готово. Браузер для входа ставится отдельно: "
               "python -m playwright install chromium")
    return {"ready": True, "browser_hint": True}


#: Что можно докачать. Вес — округлённый, для вопроса человеку.
PARTS: dict[str, dict[str, Any]] = {
    "fast": {
        "title": "Быстрая модель распознавания",
        "size_mb": 850,
        "why": "Нужна, если звонки или голосовой ввод идут быстрой моделью: фраза "
               "появляется почти сразу, но без времени слов и с ошибками чаще.",
        "ready": lambda: _ox_ready("fast"),
        "install": _ox_install("fast", 850),
    },
    "precise_ox_int8": {
        "title": "Точная модель, сжатые веса",
        "size_mb": 230,
        "why": "Точная модель со сжатыми весами — с ней программа ставится: вчетверо "
               "меньше места и втрое меньше памяти, чем полные, совпадает около 98 % слов.",
        "ready": lambda: _ox_ready("ox_int8"),
        "install": _ox_install("ox_int8", 230),
    },
    "precise_ox_fp32": {
        "title": "Точная модель, полные веса",
        "size_mb": 890,
        "why": "Точная модель с полными весами: текст слово в слово как у исходной "
               "модели. Стоит попробовать, если качество стенограмм со сжатыми не "
               "устраивает; места нужно вчетверо, памяти втрое больше.",
        "ready": lambda: _ox_ready("ox_fp32"),
        "install": _ox_install("ox_fp32", 890),
    },
    "english": {
        "title": "Английская модель Parakeet",
        "size_mb": 630,
        "why": "Нужна, только если запись на английском.",
        "ready": english_ready,
        "install": english_install,
    },
    "browser": {
        "title": "Браузер для входа в SharePoint",
        "size_mb": 100,
        "why": "Нужен только для входа в SharePoint через окно браузера; "
               "вход по коду устройства работает без него.",
        "ready": browser_ready,
        "install": browser_install,
        # Кому нужна: входу в SharePoint и сайтам по паролю. Обе возможности
        # выключены — части в списке нет, как и всего остального от них.
        "features": ("video_sharepoint", "video_password"),
    },
}


def _wanted(part: dict[str, Any]) -> bool:
    """Нужна ли часть при нынешних возможностях. Без списка — нужна всегда."""
    feats = part.get("features") or ()
    return not feats or any(config.feature(f) for f in feats)


def state() -> list[dict[str, Any]]:
    """Что из тяжёлого уже на месте, а что придётся скачать.

    Части выключенных возможностей не показываются: выключенная возможность
    убирает с экрана всё своё, и загружаемые части тоже.
    """
    out = []
    for key, part in PARTS.items():
        if not _wanted(part):
            continue
        try:
            ok = bool(part["ready"]())
        except Exception as err:
            log.debug("часть %s: проверка не прошла: %s", key, err)
            ok = False
        out.append({"key": key, "title": part["title"], "size_mb": part["size_mb"],
                    "why": part["why"], "ready": ok})
    return out


def ready(key: str) -> bool:
    part = PARTS.get(str(key))
    if part is None:
        raise NeedError("Неизвестная часть: %s" % key)
    try:
        return bool(part["ready"]())
    except Exception:
        return False


def install(key: str, note: Note | None = None) -> dict[str, Any]:
    """Скачать часть. Уже на месте — ничего не делаем и говорим об этом."""
    part = PARTS.get(str(key))
    if part is None:
        raise NeedError("Неизвестная часть: %s" % key)
    if ready(key):
        _say(note, "%s уже на месте." % part["title"])
        return {"ready": True, "skipped": True}
    return dict(part["install"](note) or {}, title=part["title"])


# ------------------------------------------------ модели распознавания на диске
#
# Выбор моделей (решение 21.09) решает, какие части нужны; здесь — что из них
# лежит на диске, сколько весит и как убрать ненужное, не сломав программу.

#: Части-модели, которыми управляет выбор в «Настройки → Модели». Английская
#: живёт своей жизнью (нужна только английским записям), поэтому в «ненужное»
#: не попадает, но «Сбросить всё» убирает и её.
MODEL_PARTS = ("fast", "precise_ox_int8", "precise_ox_fp32", "english", "unused")
#: Части из одной папки models\onnx-asr\gigaam-v3: у них есть общие файлы.
_OX_PARTS = ("fast", "precise_ox_int8", "precise_ox_fp32")
_OX_ENGINES = {"fast": "fast", "precise_ox_int8": "ox_int8", "precise_ox_fp32": "ox_fp32"}
UNUSED_TITLE = "Файлы моделей прежних версий программы"
UNUSED_WHY = ("Веса для torch и модели, переведённые в ONNX на этом компьютере: "
              "программа их больше не читает.")


def part_title(key: str) -> str:
    return UNUSED_TITLE if key == "unused" else PARTS[key]["title"]


def part_files(key: str) -> list[Path]:
    """Файлы части на диске: по ним считается вес и они же удаляются."""
    from . import asr

    if key in _OX_PARTS:
        return [asr.OX_DIR / n for n in asr.ox_files(_OX_ENGINES[key])]
    if key == "english":
        return [asr.EN_DIR / n for n in asr.english_files()]
    if key == "unused":
        fast = str(config.get("live_model") or "v3_e2e_ctc")
        precise = str(config.get("offline_model") or "v3_e2e_rnnt")
        return ([OLD_ONNX_DIR / (fast + s) for s in (".onnx", ".yaml")]
                + [OLD_ONNX_DIR / (precise + s) for s in ("_encoder.onnx", "_decoder.onnx", "_joint.onnx", ".yaml")]
                + [OLD_CKPT_DIR / (name + s) for name in (fast, precise) for s in (".ckpt", "_tokenizer.model")])
    raise NeedError("Неизвестная часть: %s" % key)


def _shared_with_others(key: str) -> set[Path]:
    """Файлы части, которые числятся и за другими частями той же папки
    (config.json — за всеми, словарь точной — за обоими вариантами весов)."""
    if key not in _OX_PARTS:
        return set()
    return {p for k in _OX_PARTS if k != key for p in part_files(k)} & set(part_files(key))


def _own_files(key: str) -> list[Path]:
    """Файлы, которые принадлежат только этой части: общие остаются, пока жива
    хоть одна другая часть, которой они нужны."""
    files = part_files(key)
    if key in _OX_PARTS:
        alive = {p for k in _OX_PARTS if k != key and on_disk(k) for p in part_files(k)}
        files = [p for p in files if p not in alive]
    return files


def on_disk(key: str) -> bool:
    """Лежит ли на диске хоть что-то от части, не считая общих с другими файлов."""
    shared = _shared_with_others(key)
    return any(p.exists() for p in part_files(key) if p not in shared)


def disk_mb(key: str) -> int:
    total = 0
    for p in _own_files(key):
        try:
            total += p.stat().st_size
        except OSError:
            pass
    return int(round(total / 2**20))


def _model_roots() -> list[Path]:
    from . import asr

    return [OLD_CKPT_DIR, OLD_ONNX_DIR, asr.EN_DIR, asr.OX_DIR]


def _inside(path: Path, roots: list[Path]) -> bool:
    """Лежит ли путь внутри одной из папок моделей. Сравнение по записи пути,
    без разворачивания ссылок: папка моделей может быть ссылкой на другую."""
    p = os.path.normcase(os.path.abspath(str(path)))
    for root in roots:
        r = os.path.normcase(os.path.abspath(str(root)))
        if os.path.dirname(p) == r:
            return True
    return False


def remove(key: str) -> dict[str, Any]:
    """Удалить файлы части. Что не удалилось (файл занят) — вернуть списком.

    Удаляет только файлы из известного списка и только внутри папок моделей:
    ни шаблонов, ни рекурсивного удаления папок.
    """
    if key not in MODEL_PARTS:
        raise NeedError("Эту часть так не удалить: %s" % key)
    roots = _model_roots()
    freed, busy = 0, []
    for p in _own_files(key):
        if not _inside(p, roots):
            raise NeedError("Файл вне папок моделей, удалять не стану: %s" % p)
        if not p.exists():
            continue
        try:
            size = p.stat().st_size
            p.unlink()
            freed += size
        except OSError as err:
            log.info("не удалился %s: %s", p, err)
            busy.append(str(p))
    if freed:
        log.info("удалено «%s»: %.0f МБ", part_title(key), freed / 2**20)
    return {"freed_mb": int(round(freed / 2**20)), "busy": busy}


def models_state() -> dict[str, Any]:
    """Что выбрано, чего выбору не хватает и что ему не нужно — для «Модели».

    missing — что скачать (с весом для кнопки); unneeded — что лежит на диске,
    но выбору не нужно (без английской и без того, что человек велел
    оставить); busy — занято работающей программой и уйдёт после перезапуска.
    """
    from . import asr

    want = asr.chosen()
    need = asr.needed_parts(want)
    running = set(asr.running_parts())
    keep = set(config.get("asr_keep_parts") or [])
    missing = [{"key": k, "title": PARTS[k]["title"], "size_mb": PARTS[k]["size_mb"]}
               for k in need if not ready(k)]
    unneeded = [{"key": k, "title": part_title(k), "disk_mb": disk_mb(k), "busy": k in running}
                for k in MODEL_PARTS
                if k != "english" and k not in need and k not in keep and on_disk(k)]
    notes = []
    if want["files"] == "fast":
        notes.append("Файлы и видео пойдут быстрой моделью: у неё нет времени слов, и "
                     "разметка голосов отдаёт фразу целиком тому, кто говорил дольше.")
    state = asr.live_state()
    running_key = state.get("key")
    return {
        "chosen": want,
        "title": asr.describe(want),
        "missing": missing,
        "download_mb": sum(m["size_mb"] for m in missing),
        "unneeded": unneeded,
        "unneeded_mb": sum(u["disk_mb"] for u in unneeded),
        "pending": list(config.get("asr_pending_delete") or []),
        "notes": notes,
        "running": state,
        # Выбор сработает после перезапуска: работает не то, что выбрано, а
        # всё нужное уже скачано (иначе перезапуск не поможет).
        "restart": bool(running_key) and running_key != asr.choice_key(want) and not missing,
    }


def _postpone(keys: list[str]) -> list[str]:
    have = list(config.get("asr_pending_delete") or [])
    for k in keys:
        if k not in have:
            have.append(k)
    config.save({"asr_pending_delete": have})
    return have


def delete_unneeded() -> dict[str, Any]:
    """Удалить то, что выбору не нужно. Занятое программой — после перезапуска.

    Пока выбору чего-то не хватает, не удаляет ничего: после перезапуска
    программа взяла бы для распознавания то, что есть, — и если это удалить,
    распознавать станет нечем.
    """
    st = models_state()
    if st["missing"]:
        raise NeedError("Сначала скачайте нужное этим настройкам: %s. Иначе после "
                        "перезапуска распознавать будет нечем."
                        % ", ".join(m["title"] for m in st["missing"]))
    removed, later, freed = [], [], 0
    for item in st["unneeded"]:
        if item["busy"]:
            later.append(item["key"])
            continue
        res = remove(item["key"])
        freed += res["freed_mb"]
        (later if res["busy"] else removed).append(item["key"])
    if later:
        _postpone(later)
    return {"removed": removed, "later": later, "freed_mb": freed}


def keep_unneeded() -> list[str]:
    """«Оставить»: больше не предлагать удалить то, что сейчас не нужно."""
    keep = list(config.get("asr_keep_parts") or [])
    for item in models_state()["unneeded"]:
        if item["key"] not in keep:
            keep.append(item["key"])
    config.save({"asr_keep_parts": keep})
    return keep


def cleanup_pending() -> list[str]:
    """При запуске: удалить отложенное — то, что держала прежняя программа.

    Только если всё нужное выбору на месте и отложенное ему по-прежнему не
    нужно: иначе удалили бы то, на чём программа сейчас и поедет.
    """
    from . import asr

    pending = list(config.get("asr_pending_delete") or [])
    if not pending:
        return []
    need = asr.needed_parts()
    if any(not ready(k) for k in need):
        log.info("отложенное удаление ждёт: выбору не хватает частей")
        return []
    done, left = [], []
    for key in pending:
        if key in need or key not in MODEL_PARTS:
            continue
        res = remove(key)
        (left if res["busy"] else done).append(key)
    config.save({"asr_pending_delete": left})
    return done


def reset_models(note: Note | None = None) -> dict[str, Any]:
    """«Сбросить всё»: одна точная модель (asr.recommended_part) и
    рекомендованные настройки, остальные модели — удалить.

    Сначала на месте должна быть оставляемая модель: нет её и нет интернета —
    ошибка, и ничего не тронуто. Занятое работающей программой удаляется при
    следующем запуске.
    """
    from . import asr

    target = asr.recommended_part()
    if not ready(target):
        install(target, note)
    config.save(dict(asr.RECOMMENDED, asr_keep_parts=[], asr_pending_delete=[]))
    running = set(asr.running_parts())
    removed, later, freed = [], [], 0
    for key in MODEL_PARTS:
        if key == target or not on_disk(key):
            continue
        if key in running:
            later.append(key)
            continue
        res = remove(key)
        freed += res["freed_mb"]
        (later if res["busy"] else removed).append(key)
    config.save({"asr_pending_delete": later})
    _say(note, "Сброшено: осталась %s. Освобождено %d МБ%s." % (
        part_title(target).lower(), freed,
        (", остальное удалится после перезапуска программы" if later else "")))
    return {"removed": removed, "later": later, "freed_mb": freed, "kept": target}
