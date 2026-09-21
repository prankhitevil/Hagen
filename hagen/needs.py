# -*- coding: utf-8 -*-
"""Тяжёлые части, которые качаются по требованию, а не лежат в сборке (16.09).

Переносимая папка весит гигабайты, и большая часть веса — то, чем пользуются не
все и не сразу: точная модель распознавания, английская модель, браузер для
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

Чего здесь нет: torch. Он нужен не только точной модели, но и поиску речи
(silero-vad) при каждой записи, поэтому остаётся в сборке — иначе программа
без интернета не смогла бы записать ни одной встречи.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Callable

from . import config

log = logging.getLogger("hagen.needs")

Note = Callable[[str], None]


class NeedError(RuntimeError):
    """Скачать не вышло, и человеку надо сказать почему."""


def _say(note: Note | None, msg: str) -> None:
    log.info(msg)
    if note:
        try:
            note(msg)
        except Exception:
            pass


# --------------------------------------------------------------- точная модель


def _precise_dir() -> Path:
    """Папка весов GigaAM — та же, откуда их читает распознавание."""
    from . import asr

    return asr.CKPT_DIR


def precise_ready() -> bool:
    """Точная модель распознавания: веса и токенизатор рядом с ними."""
    name = str(config.get("offline_model") or "v3_e2e_rnnt")
    d = _precise_dir()
    return (d / (name + ".ckpt")).exists() and (d / (name + "_tokenizer.model")).exists()


def precise_install(note: Note | None = None) -> dict[str, Any]:
    """Скачать веса точной модели. Модель при этом в память не поднимается."""
    import gigaam

    name = str(config.get("offline_model") or "v3_e2e_rnnt")
    d = _precise_dir()
    d.mkdir(parents=True, exist_ok=True)
    _say(note, "Скачиваю точную модель распознавания (около 430 МБ)…")
    try:
        real_name, path = gigaam._download_model(name.replace("v3_", ""), str(d))
        gigaam._download_tokenizer(real_name, str(d))
    except Exception as err:
        raise NeedError(
            "Точную модель скачать не вышло: %s\n"
            "Проверьте интернет; в корпоративной сети скачивание могут закрывать." % err
        ) from err
    _say(note, "Точная модель на месте: %s" % path)
    return {"ready": precise_ready(), "path": str(path)}


# -------------------------------------------------------------- быстрая модель


def fast_ready() -> bool:
    """Быстрая модель: переведённый в ONNX файл, его описание и словарь."""
    from . import asr

    try:
        return bool(asr.engine_ready("fast")[0])
    except Exception:
        return False


def fast_install(note: Note | None = None) -> dict[str, Any]:
    """Скачать быструю модель и перевести её в ONNX на этом компьютере.

    В готовом ONNX её никто не выкладывает: авторы дают только веса для torch.
    После перевода веса не нужны — они удаляются, остаются ONNX и словарь.
    """
    import gigaam

    from . import asr

    name = str(config.get("live_model") or "v3_e2e_ctc")
    d = _precise_dir()
    d.mkdir(parents=True, exist_ok=True)
    _say(note, "Скачиваю быструю модель распознавания (около 440 МБ)…")
    try:
        real_name, path = gigaam._download_model(name.replace("v3_", ""), str(d))
        gigaam._download_tokenizer(real_name, str(d))
    except Exception as err:
        raise NeedError(
            "Быструю модель скачать не вышло: %s\n"
            "Проверьте интернет; в корпоративной сети скачивание могут закрывать." % err
        ) from err
    _say(note, "Перевожу быструю модель в формат ONNX — это несколько минут…")
    try:
        asr.export_onnx(name, force=True)
    except Exception as err:
        raise NeedError("Быструю модель перевести в ONNX не вышло: %s" % err) from err
    try:
        Path(path).unlink()
    except OSError:
        log.info("веса быстрой модели после перевода не удалились: %s", path)
    _say(note, "Быстрая модель на месте.")
    return {"ready": fast_ready()}


# ------------------------------------------- точная модель для движка onnx-asr


def _ox_ready(quant: str | None) -> bool:
    from . import asr

    try:
        return bool(asr.ox_available(quant)[0])
    except Exception:
        return False


def _ox_install(quant: str | None, size_mb: int) -> Callable[..., dict[str, Any]]:
    """Скачать точную модель для onnx-asr: полные или сжатые веса."""
    def run(note: Note | None = None) -> dict[str, Any]:
        from huggingface_hub import snapshot_download

        from . import asr

        _say(note, "Скачиваю точную модель для onnx-asr, %s (около %d МБ)…"
             % ("сжатые веса" if quant else "полные веса", size_mb))
        try:
            with config.hf_online():
                snapshot_download(repo_id=asr.OX_REPO, local_dir=str(asr.OX_DIR),
                                  allow_patterns=asr.ox_files(quant))
        except Exception as err:
            raise NeedError(
                "Точную модель для onnx-asr скачать не вышло: %s\n"
                "Нужен интернет и доступ к huggingface.co." % err
            ) from err
        _say(note, "Точная модель для onnx-asr на месте.")
        return {"ready": _ox_ready(quant)}
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

    from . import asr

    _say(note, "Скачиваю английскую модель Parakeet (около 630 МБ)…")
    try:
        snapshot_download(
            repo_id="istupakov/parakeet-tdt-0.6b-v2-onnx",
            local_dir=str(asr.EN_DIR),
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
        "size_mb": 440,
        "why": "Нужна, если звонки или голосовой ввод идут быстрой моделью. Скачивается "
               "и переводится в формат ONNX на этом компьютере; на диске — около 890 МБ.",
        "ready": lambda: fast_ready(),
        "install": lambda note=None: fast_install(note),
    },
    "precise": {
        "title": "Точная модель распознавания",
        "size_mb": 430,
        "why": "Точная модель на движке torch — нужна, если в «Моделях распознавания» "
               "выбран движок torch.",
        "ready": lambda: precise_ready(),
        "install": lambda note=None: precise_install(note),
    },
    "precise_ox_int8": {
        "title": "Точная модель для onnx-asr, сжатые веса",
        "size_mb": 230,
        "why": "Точная модель на движке onnx-asr со сжатыми весами: вчетверо меньше "
               "места и втрое меньше памяти, чем полные, текст чуть хуже.",
        "ready": lambda: _ox_ready("int8"),
        "install": _ox_install("int8", 230),
    },
    "precise_ox_fp32": {
        "title": "Точная модель для onnx-asr, полные веса",
        "size_mb": 890,
        "why": "Точная модель на движке onnx-asr с полными весами — рекомендованный "
               "вариант: текст слово в слово как у torch.",
        "ready": lambda: _ox_ready(None),
        "install": _ox_install(None, 890),
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
    },
}


def state() -> list[dict[str, Any]]:
    """Что из тяжёлого уже на месте, а что придётся скачать."""
    out = []
    for key, part in PARTS.items():
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
MODEL_PARTS = ("fast", "precise", "precise_ox_int8", "precise_ox_fp32", "english", "unused")
UNUSED_TITLE = "Неиспользуемые файлы моделей"
UNUSED_WHY = ("Точная модель, переведённая в ONNX, и запасные веса быстрой модели: "
              "программа их не читает.")
#: Общие для сжатых и полных весов onnx-asr файлы: удаляются только вместе с
#: последним из двух вариантов.
_OX_SHARED = ("config.json", "v3_e2e_rnnt_vocab.txt")


def part_title(key: str) -> str:
    return UNUSED_TITLE if key == "unused" else PARTS[key]["title"]


def part_files(key: str) -> list[Path]:
    """Файлы части на диске: по ним считается вес и они же удаляются."""
    from . import asr

    fast = str(config.get("live_model") or "v3_e2e_ctc")
    precise = str(config.get("offline_model") or "v3_e2e_rnnt")
    if key == "fast":
        return [asr.ONNX_DIR / (fast + ".onnx"), asr.ONNX_DIR / (fast + ".yaml"),
                asr.CKPT_DIR / (fast + "_tokenizer.model")]
    if key == "precise":
        return [asr.CKPT_DIR / (precise + ".ckpt"),
                asr.CKPT_DIR / (precise + "_tokenizer.model")]
    if key in ("precise_ox_int8", "precise_ox_fp32"):
        return [asr.OX_DIR / n for n in asr.ox_files("int8" if key.endswith("int8") else None)]
    if key == "english":
        return [asr.EN_DIR / n for n in asr.english_files()]
    if key == "unused":
        return ([asr.ONNX_DIR / (precise + s)
                 for s in ("_encoder.onnx", "_decoder.onnx", "_joint.onnx", ".yaml")]
                + [asr.CKPT_DIR / (fast + ".ckpt")])
    raise NeedError("Неизвестная часть: %s" % key)


def _own_files(key: str) -> list[Path]:
    """Файлы, которые принадлежат только этой части (без общих для onnx-asr,
    пока жив второй вариант весов)."""
    files = part_files(key)
    if key in ("precise_ox_int8", "precise_ox_fp32"):
        other = "precise_ox_fp32" if key.endswith("int8") else "precise_ox_int8"
        if any(p.exists() for p in part_files(other) if p.name not in _OX_SHARED):
            files = [p for p in files if p.name not in _OX_SHARED]
    return files


def on_disk(key: str) -> bool:
    """Лежит ли на диске хоть что-то от части (у onnx-asr — не считая общих файлов)."""
    return any(p.exists() for p in part_files(key)
               if key not in ("precise_ox_int8", "precise_ox_fp32") or p.name not in _OX_SHARED)


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

    return [asr.CKPT_DIR, asr.ONNX_DIR, asr.EN_DIR, asr.OX_DIR]


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
                     "разметка говорящих отдаёт фразу целиком тому, кто говорил дольше.")
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
