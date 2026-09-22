# -*- coding: utf-8 -*-
"""Что это за выпуск: номер версии, движок разметки, откуда брать обновления.

Всё это лежит в `release.json` в корне программы:

    {"version": "0.9.2", "diarize": "onnx", "updates": "prankhitevil/Hagen"}

`version` — номер выпуска, его видно в «Настройки → О программе», с ним
сравнивается свежий релиз. `diarize` — какой движок разметки говорящих ставит
этот выпуск (см. `diarize.py`). `updates` — репозиторий GitHub, чьи релизы
программа смотрит по кнопке «Проверить обновления».

Модуль берёт только стандартную библиотеку Python: его читает установщик — до
того, как появится окружение с библиотеками, — и обновление на старте
программы, до загрузки всего остального.
"""
from __future__ import annotations

import io
import json
import re
from pathlib import Path
from typing import Any

PROJECT_DIR = Path(__file__).resolve().parent.parent
PATH = PROJECT_DIR / "release.json"

#: Движки разметки. Нет файла или в нём мусор — pyannote, как было до
#: появления второго движка.
ENGINES = ("pyannote", "onnx")
DEFAULT_ENGINE = "pyannote"


def read(path: str | Path | None = None) -> dict[str, Any]:
    """Содержимое release.json. Нет файла или мусор — пустой словарь."""
    try:
        with io.open(path or PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def version(path: str | Path | None = None) -> str:
    """Номер выпуска без «v»: «0.9.2». Пусто — номера нет."""
    return version_of(read(path))


def version_of(info: dict[str, Any]) -> str:
    """Номер выпуска из уже прочитанного release.json (например, из архива)."""
    raw = str((info or {}).get("version") or "").strip()
    return raw[1:] if raw[:1] in ("v", "V") else raw


def diarize_engine(path: str | Path | None = None) -> str:
    """Движок разметки, который назначил выпуск."""
    name = str(read(path).get("diarize") or "").strip().lower()
    return name if name in ENGINES else DEFAULT_ENGINE


def updates_repo(path: str | Path | None = None) -> str:
    """Репозиторий GitHub с релизами («владелец/имя»). Пусто — обновлений нет."""
    repo = str(read(path).get("updates") or "").strip().strip("/")
    return repo if re.fullmatch(r"[\w.-]+/[\w.-]+", repo) else ""


def parse_version(text: str) -> tuple[int, ...] | None:
    """«v0.9.10» → (0, 9, 10). Не номер — None.

    Сравниваем числами, а не строками: иначе 0.9.10 оказался бы старше 0.9.9.
    """
    raw = str(text or "").strip()
    if raw[:1] in ("v", "V"):
        raw = raw[1:]
    if not re.fullmatch(r"\d+(\.\d+)*", raw):
        return None
    return tuple(int(p) for p in raw.split("."))


def newer(candidate: str, current: str) -> bool:
    """Новее ли candidate, чем current. Незнакомый номер новее не бывает.

    Хвостовые нули не в счёт: 0.9 и 0.9.0 — одна версия.
    """
    a, b = parse_version(candidate), parse_version(current)
    if a is None:
        return False
    if b is None:
        return True
    width = max(len(a), len(b))
    return a + (0,) * (width - len(a)) > b + (0,) * (width - len(b))
