# -*- coding: utf-8 -*-
"""Проверка 78: проект и теги записи (17.09).

Решение 17.09: метаданные нужны, «псевдо-Obsidian» внутри программы —
нет. То есть программа только проставляет проект и теги и печатает их в шапку
заметки; ни графа, ни подборок, ни фильтров по ним внутри не строится — этим
занимается Obsidian.

Список проектов — свой, в настройках (решение 17.09: не папки сейфа и
не Todoist, чтобы ни от чего не зависеть и работать без сети). Пополняется сам.

Что проверяем:
  1. чистка проекта: пробелы по краям, ограничение длины;
  2. чистка тегов: решётка, пробелы внутри тега, повторы, пустые, предел числа;
  3. память подсказок: введённое попадает в список и не двоится;
  4. шапка заметки: project появляется, свои теги идут ПОСЛЕ служебных,
     пустой проект в шапку не пишется;
  5. точка службы PATCH принимает проект и теги, чистит их и обновляет заметку;
  6. /api/labels отдаёт подсказки;
  7. чужая категория и прочие поля записи от этого не страдают.

Служба поднимается в этом же процессе (Kaspersky не даёт плодить фоновые).
Запуск из корня проекта:  .venv\\Scripts\\python.exe tests\\t78_project_tags.py
"""
import sys
import tempfile
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
sys.path.insert(0, str(PROJECT / "tests"))
import isolate  # noqa: E402

isolate.voices()
isolate.settings()

from hagen import config, obsidian, store  # noqa: E402

FAIL = []


def ok(label, cond, detail=""):
    print("   %s   %s%s" % ("ok  " if cond else "ПРОВАЛ", label,
                            ("" if not detail else ": %s" % detail)))
    if not cond:
        FAIL.append(label)


print("=== 1. Чистка проекта ===")
ok("пробелы по краям срезаны", store.clean_project("  Hagen  ") == "Hagen")
ok("пусто остаётся пустым", store.clean_project(None) == "")
ok("длина ограничена", len(store.clean_project("я" * 200)) == store.MAX_PROJECT)

print("\n=== 2. Чистка тегов ===")
ok("решётка снимается", store.clean_tag("#встреча") == "встреча")
ok("пробел внутри становится дефисом", store.clean_tag("третий квартал") == "третий-квартал")
ok("точки и скобки тоже", store.clean_tag("отчёт (v2)") == "отчёт-v2")
ok("дефисы по краям срезаны", store.clean_tag("--важно--") == "важно")
ok("пустой тег отбрасывается", store.clean_tags(["", "  ", "#"]) == [])
ok("повторы без учёта регистра", store.clean_tags(["Смета", "смета"]) == ["Смета"])
ok("строка через запятую разбирается",
   store.clean_tags("смета, планёрка") == ["смета", "планёрка"])
много = store.clean_tags(["т%d" % i for i in range(30)])
ok("число тегов ограничено", len(много) == store.MAX_TAGS, "%d" % len(много))

print("\n=== 3. Память подсказок ===")
config.save({"projects": [], "rec_tags": []})
store.remember_project("Hagen")
store.remember_project("Hagen")
store.remember_project("АКП")
ok("проекты запомнились без повторов", store.known_projects() == ["Hagen", "АКП"],
   str(store.known_projects()))
store.remember_tags(["смета", "смета", "планёрка"])
ok("теги запомнились без повторов", store.known_tags() == ["смета", "планёрка"],
   str(store.known_tags()))
store.remember_project("")
ok("пустой проект не запоминается", store.known_projects() == ["Hagen", "АКП"])

print("\n=== 4. Шапка заметки ===")
vault = Path(tempfile.mkdtemp(prefix="vault_t78_"))
config.save({"vault_path": str(vault), "vault_subfolder": "Meetings",
             "vault_confirmed": True})
rec_id = store.create(title="планёрка по смете", source="live")["id"]
store.replace_segments(rec_id, [
    store.make_segment("mic", 0.0, 4.0, "Обсудили смету."),
    store.make_segment("far", 4.0, 8.0, "Согласовали срок.", speaker="Пётр"),
])
store.update(rec_id, {"project": "Hagen", "tags": ["смета", "планёрка"]})
res = obsidian.save_note(rec_id)
head = Path(res["path"]).read_text(encoding="utf-8").split("---")[1]
ok("project в шапке", "project: " in head and "Hagen" in head, head.strip().splitlines()[-3:])
ok("служебные теги на месте", "hagen" in head and "стенограмма" in head)
ok("свои теги в шапке", "смета" in head and "планёрка" in head)
tags_line = [ln for ln in head.splitlines() if ln.startswith("tags:")][0]
ok("свои теги ПОСЛЕ служебных",
   tags_line.index("стенограмма") < tags_line.index("смета"), tags_line)

store.update(rec_id, {"project": "", "tags": []})
res2 = obsidian.save_note(rec_id)
head2 = Path(res2["path"]).read_text(encoding="utf-8").split("---")[1]
ok("пустой проект в шапку не пишется", "project:" not in head2)
ok("служебные теги остались", "hagen" in head2)

print("\n=== 5. Точка службы ===")
from fastapi.testclient import TestClient  # noqa: E402
from hagen.server import app  # noqa: E402

# base_url задаёт заголовок Host: служба намеренно отвечает только своему же
# адресу, и по умолчанию тестовый клиент присылает чужой Host: testserver.
ORIGIN = {"Origin": "http://127.0.0.1:8787"}
with TestClient(app, base_url="http://127.0.0.1:8787") as client:
    r = client.patch("/api/recordings/%s" % rec_id,
                     json={"project": "  АКП Digital  ",
                           "tags": ["#Смета", "третий квартал", "смета"]},
                     headers=ORIGIN)
    ok("PATCH принят", r.status_code == 200, r.text[:200])
    meta = r.json() if r.status_code == 200 else {}
    ok("проект почищен", meta.get("project") == "АКП Digital", str(meta.get("project")))
    ok("теги почищены и без повторов",
       meta.get("tags") == ["Смета", "третий-квартал"], str(meta.get("tags")))
    ok("категория не пострадала", bool(meta.get("category")))
    ok("название не пострадало", meta.get("title") == "планёрка по смете")

    head3 = Path(res["path"]).read_text(encoding="utf-8").split("---")[1]
    ok("заметка обновилась сама", "АКП Digital" in head3, head3.strip().splitlines()[:6])

    r2 = client.get("/api/labels", headers=ORIGIN)
    ok("/api/labels отвечает", r2.status_code == 200)
    labels = r2.json() if r2.status_code == 200 else {}
    ok("новый проект попал в подсказки", "АКП Digital" in (labels.get("projects") or []),
       str(labels.get("projects")))
    ok("новые теги попали в подсказки", "третий-квартал" in (labels.get("tags") or []),
       str(labels.get("tags")))

    r3 = client.patch("/api/recordings/%s" % rec_id, json={"tags": []}, headers=ORIGIN)
    ok("пустой список тегов принимается", r3.status_code == 200 and r3.json().get("tags") == [])

try:
    store.delete(rec_id)
except Exception:
    pass

print("\nВсего провалов: %d" % len(FAIL))
if FAIL:
    for f in FAIL:
        print("  - %s" % f)
sys.exit(1 if FAIL else 0)
