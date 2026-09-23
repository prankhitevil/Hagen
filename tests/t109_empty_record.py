# -*- coding: utf-8 -*-
"""Проверка 109: запись без звука — отдельной строкой списка (решение 22.09).

Запись с нулевой длительностью попадала в список как обычная и открывалась как
обычная — пустая. Теперь признак «не состоялась» один на список и на открытие:
его считает хранилище, в файл записи он не пишется. Строка такой записи — с
причиной и одним действием «Удалить»; уже существующие такие записи программа
не удаляет сама.

Что проверяем:
  1. признак: живая остановленная запись короче секунды — пустая; длинная,
     идущая, видео и запись со стёртым звуком — нет;
  2. признак приходит со списком, с чтением записи и после изменения, а в
     meta.json не попадает;
  3. страница: у пустой записи своя строка, она не открывается щелчком, у
     открытой пустой записи — тот же вид и та же кнопка.
"""
import io
import json
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
sys.path.insert(0, str(PROJECT / "tests"))
import isolate  # noqa: E402
from harness import LINES, FAIL, say, check, finish  # noqa: E402

isolate.voices()
isolate.settings()


from hagen import store  # noqa: E402

say("=== 1. Признак ===")
base = {"id": "x", "source": "live", "status": "recorded"}
check("живая, остановлена, 0 с — пустая", store.is_empty(dict(base, duration_s=0.0)))
check("0,5 с — тоже пустая", store.is_empty(dict(base, duration_s=0.5)))
check("5 с — обычная", not store.is_empty(dict(base, duration_s=5.0)))
check("идущая запись — не пустая", not store.is_empty(dict(base, status="recording", duration_s=0)))
check("видео без звука (субтитры) — не пустая",
      not store.is_empty(dict(base, source="file", duration_s=0)))
check("звук стёрт руками — не пустая, стенограмма осталась",
      not store.is_empty(dict(base, duration_s=0, media_removed=True)))

say("")
say("=== 2. Признак приходит с записью и не хранится ===")
made = []
try:
    empty = store.create(title="Проверка 109 — пустая", source="live")
    made.append(empty["id"])
    store.update(empty["id"], {"status": "recorded", "duration_s": 0.2})
    full = store.create(title="Проверка 109 — обычная", source="live")
    made.append(full["id"])
    store.update(full["id"], {"status": "recorded", "duration_s": 42.0})

    check("чтение записи: пустая", store.get(empty["id"]).get("empty") is True)
    check("чтение записи: обычная", store.get(full["id"]).get("empty") is False)
    listed = {m["id"]: m for m in store.list_all()}
    check("список: у пустой признак есть", listed[empty["id"]].get("empty") is True)
    check("список: у обычной — нет", listed[full["id"]].get("empty") is False)
    after = store.update(empty["id"], {"title": "Проверка 109 — пустая, переименована"})
    check("после изменения признак на месте", after.get("empty") is True)
    raw = json.loads(io.open(store.paths(empty["id"])["meta"], encoding="utf-8").read())
    check("в meta.json признак не пишется", "empty" not in raw, sorted(raw))
    # Даже если кто-то передаст запись целиком, с признаком, — в файл он не уйдёт.
    store.update(empty["id"], dict(store.get(empty["id"])))
    raw = json.loads(io.open(store.paths(empty["id"])["meta"], encoding="utf-8").read())
    check("и не пишется, если запись отдали обратно целиком", "empty" not in raw)
finally:
    for rid in made:
        store.delete(rid)

say("")
say("=== 3. Страница ===")
js = io.open(PROJECT / "hagen" / "static" / "app.js", encoding="utf-8").read()
css = io.open(PROJECT / "hagen" / "static" / "app.css", encoding="utf-8").read()
check("у пустой записи своя строка с причиной",
      "if (m.empty)" in js and "не записалось — звука нет" in js)
check("строка не открывается щелчком", "if (el.dataset.empty) return;" in js)
check("одно действие — «Удалить»", js.count("js-empty-del") >= 3 and "function deleteEmpty(" in js)
check("открытая пустая запись показана тем же признаком",
      "(S.current.meta || {}).empty" in js and "Не записалось — звука нет." in js)
check("строка выглядит иначе", ".rec-item.empty-rec" in css)
check("решение о пустоте — не на странице (нет своего порога)",
      "duration_s < 1" not in js and "EMPTY_MIN_S" not in js)

sys.exit(finish("t109"))
