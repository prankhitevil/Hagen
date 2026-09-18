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
    рассказом о ходе работы словами.

Чего здесь нет: torch. Он нужен не только точной модели, но и поиску речи
(silero-vad) при каждой записи, поэтому остаётся в сборке — иначе программа
без интернета не смогла бы записать ни одной встречи.
"""
from __future__ import annotations

import logging
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
    return config.MODELS_DIR / "gigaam"


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
    "precise": {
        "title": "Точная модель распознавания",
        "size_mb": 430,
        "why": "Нужна для «Перечитать точнее», обработки видео и набора текста голосом.",
        "ready": precise_ready,
        "install": precise_install,
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
