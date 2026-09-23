# -*- coding: utf-8 -*-
"""Проверка 110: удаление записи трогает только её снимки.

Путь снимка лежит в карточке записи, а рядом с заметками в сейфе лежат чужие
картинки. Поэтому розетка удаляет файл, только если он в папке снимков этой
записи — папку называет логика заметок, та же, куда снимки складывались.

Буфер обмена и микрофон не нужны: t47 проверяет съёмку целиком, а здесь —
только запрет. Запуск: .venv\\Scripts\\python.exe tests\\t110_shots_guard.py
"""
import shutil
import sys
import tempfile
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
sys.path.insert(0, str(PROJECT / "tests"))
import isolate  # noqa: E402
from harness import check, finish  # noqa: E402

isolate.voices()
TMP = Path(tempfile.mkdtemp(prefix="t110_"))
isolate.settings(vault_path=str(TMP / "vault"), vault_subfolder="Meetings")

from hagen import obsidian, platform, store  # noqa: E402

try:
    meta = store.create(title="Проверка 110", mode="online", source="live", category="Встречи")
    rid = meta["id"]
    folder = obsidian.shots_dir(store.get(rid))
    folder.mkdir(parents=True, exist_ok=True)
    own = folder / "снимок.png"
    own.write_bytes(b"png")
    alien = folder.parent / "чужая картинка.png"      # лежит рядом с заметками
    alien.write_bytes(b"png")
    note = folder / "заметка.md"                      # не картинка
    note.write_text("текст", encoding="utf-8")
    store.update(rid, {"screenshots": [{"file": own.name, "path": str(own)},
                                       {"file": alien.name, "path": str(alien)},
                                       {"file": note.name, "path": str(note)}]})

    removed = platform.system().delete_shots(rid, folder)
    check("удалён только свой снимок", removed == 1, removed)
    check("свой снимок убран", not own.exists())
    check("чужая картинка в сейфе цела", alien.exists())
    check("не картинку из своей же папки не трогаем", note.exists())

    store.delete(rid)
finally:
    shutil.rmtree(TMP, ignore_errors=True)

sys.exit(finish("t110"))
