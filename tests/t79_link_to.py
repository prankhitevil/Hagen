# -*- coding: utf-8 -*-
"""Проверка 79: «Связать с» — ссылки на заметки сейфа (17.09).

Решение 17.09: ссылка ставится на ЗАМЕТКУ, а не на папку. У папки нет
вики-ссылки: в заметке остался бы текст пути, по которому Obsidian не перейдёт,
графа не построит и в обратные ссылки запись не попадёт.

Связи живут в данных записи, а не в файле. Это следствие решения
15.09: заметка целиком перерисовывается из программы, и всё, что дописано
руками в Obsidian, пропало бы при следующем сохранении. Поэтому механизм
«защиты раздела от затирания» не нужен вовсе.

Что проверяем:
  1. поиск по сейфу: от двух букв, совпадение с начала названия выше прочих,
     служебные папки Obsidian не попадают в выдачу;
  2. чистка связей: пустые, повторы, предел числа;
  3. строка «Связано» в блоке «О записи» — главных разделов не прибавляется
     (формат 15.09: их ровно столько, сколько документов);
  4. тёзки: две заметки с одинаковым именем дают ссылку с путём;
  5. пересохранение заметки связи не теряет и не двоит;
  6. точка службы принимает связи и обновляет заметку;
  7. поиск по пустому запросу не вываливает весь сейф.

Служба поднимается в этом же процессе (Kaspersky не даёт плодить фоновые).
Запуск из корня проекта:  .venv\\Scripts\\python.exe tests\\t79_link_to.py
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


# ---- сейф с заметками -------------------------------------------------------
vault = Path(tempfile.mkdtemp(prefix="vault_t79_"))
(vault / "Projects").mkdir()
(vault / "Wiki").mkdir()
(vault / ".obsidian").mkdir()
(vault / ".trash").mkdir()
for rel in ("Смета.md", "Projects/Смета третьего квартала.md",
            "Projects/Пересмотр сметы.md", "Wiki/Смета.md",
            "Wiki/Интеграционная шина.md"):
    (vault / rel).write_text("# заметка\n", encoding="utf-8")
(vault / ".obsidian/workspace.md").write_text("служебное\n", encoding="utf-8")
(vault / ".trash/Смета старая.md").write_text("удалённое\n", encoding="utf-8")

config.save({"vault_path": str(vault), "vault_subfolder": "Meetings",
             "vault_confirmed": True})
obsidian._notes_cache.update({"root": "", "stamp": 0.0, "items": []})

print("=== 1. Поиск по сейфу ===")
ok("одна буква ничего не даёт", obsidian.find_notes("С") == [])
ok("пустой запрос ничего не даёт", obsidian.find_notes("") == [])
hits = obsidian.find_notes("смет")
titles = [h["title"] for h in hits]
ok("нашлись все сметы", len(hits) == 4, str(titles))
ok("совпадение с начала названия выше прочих",
   titles[0].lower().startswith("смет") and titles[-1] == "Пересмотр сметы", str(titles))
ok("служебная папка Obsidian не в выдаче",
   not any("obsidian" in h["path"] for h in obsidian.find_notes("workspace")))
ok("корзина не в выдаче",
   not any(".trash" in h["path"] for h in hits), str([h["path"] for h in hits]))
ok("поиск по пути тоже работает", bool(obsidian.find_notes("wiki")))
ok("путь относительный, через косую",
   all("\\" not in h["path"] for h in hits), str([h["path"] for h in hits]))

print("\n=== 2. Чистка связей ===")
ok("пустой заголовок отбрасывается",
   store.clean_links([{"title": "  ", "path": "a.md"}]) == [])
ok("строка превращается в связь",
   store.clean_links(["Смета"]) == [{"title": "Смета", "path": ""}])
ok("повтор по пути схлопывается",
   len(store.clean_links([{"title": "А", "path": "x.md"},
                          {"title": "Б", "path": "X.MD"}])) == 1)
много = store.clean_links([{"title": "т%d" % i, "path": "%d.md" % i} for i in range(50)])
ok("число связей ограничено", len(много) == store.MAX_LINKS, "%d" % len(много))

print("\n=== 3. Раздел «Связано» в заметке ===")
rec_id = store.create(title="планёрка", source="live")["id"]
store.replace_segments(rec_id, [store.make_segment("mic", 0.0, 4.0, "Обсудили смету.")])
store.update(rec_id, {"links": [
    {"title": "Интеграционная шина", "path": "Wiki/Интеграционная шина.md"},
    {"title": "Пересмотр сметы", "path": "Projects/Пересмотр сметы.md"},
]})
res = obsidian.save_note(rec_id)
text = Path(res["path"]).read_text(encoding="utf-8")
ok("строка связей появилась", "**%s:**" % obsidian.H_LINKED in text)
ok("ссылки вики-формата", "[[Интеграционная шина]]" in text and "[[Пересмотр сметы]]" in text)
ok("связи в блоке «О записи», до стенограммы",
   text.index(obsidian.H_LINKED) < text.index("# Стенограмма"))
ok("главных разделов не прибавилось",
   [ln for ln in text.splitlines() if ln.startswith("# ")] == ["# О записи", "# Стенограмма"],
   str([ln for ln in text.splitlines() if ln.startswith("# ")]))
ok("порядок связей сохранён",
   text.index("Интеграционная шина") < text.index("Пересмотр сметы"))

print("\n=== 4. Тёзки различаются путём ===")
store.update(rec_id, {"links": [{"title": "Смета", "path": "Wiki/Смета.md"}]})
res = obsidian.save_note(rec_id)
text = Path(res["path"]).read_text(encoding="utf-8")
ok("у тёзки ссылка с путём", "[[Wiki/Смета|Смета]]" in text, text.split(obsidian.H_LINKED)[1][:80])
store.update(rec_id, {"links": [{"title": "Интеграционная шина",
                                 "path": "Wiki/Интеграционная шина.md"}]})
res = obsidian.save_note(rec_id)
text = Path(res["path"]).read_text(encoding="utf-8")
ok("у единственной заметки ссылка короткая", "[[Интеграционная шина]]" in text)

print("\n=== 5. Пересохранение ===")
before = Path(res["path"]).read_text(encoding="utf-8")
obsidian.save_note(rec_id)
after = Path(res["path"]).read_text(encoding="utf-8")
ok("связи на месте после пересохранения", obsidian.H_LINKED in after)
ok("строка не удвоилась", after.count("**%s:**" % obsidian.H_LINKED) == 1)
ok("заметка не поехала", before == after)
store.update(rec_id, {"links": []})
obsidian.save_note(rec_id)
пусто = Path(res["path"]).read_text(encoding="utf-8")
ok("без связей строки нет", obsidian.H_LINKED not in пусто)

print("\n=== 6. Точка службы ===")
from fastapi.testclient import TestClient  # noqa: E402
from hagen.server import app  # noqa: E402

ORIGIN = {"Origin": "http://127.0.0.1:8787"}
with TestClient(app, base_url="http://127.0.0.1:8787") as client:
    r = client.get("/api/vault/notes?q=смет", headers=ORIGIN)
    ok("поиск отвечает", r.status_code == 200, r.text[:160])
    ok("отдаёт заголовок и путь",
       all({"title", "path"} <= set(n) for n in (r.json().get("notes") or [])))
    r0 = client.get("/api/vault/notes?q=с", headers=ORIGIN)
    ok("однобуквенный запрос — пустой список", r0.json().get("notes") == [])

    r2 = client.patch("/api/recordings/%s" % rec_id, headers=ORIGIN, json={"links": [
        {"title": "Пересмотр сметы", "path": "Projects/Пересмотр сметы.md"},
        {"title": "Пересмотр сметы", "path": "Projects/Пересмотр сметы.md"},
    ]})
    ok("PATCH принят", r2.status_code == 200, r2.text[:160])
    meta = r2.json() if r2.status_code == 200 else {}
    ok("повтор схлопнулся", len(meta.get("links") or []) == 1, str(meta.get("links")))
    text = Path(res["path"]).read_text(encoding="utf-8")
    ok("заметка обновилась сама", "[[Пересмотр сметы]]" in text)

try:
    store.delete(rec_id)
except Exception:
    pass

print("\nВсего провалов: %d" % len(FAIL))
if FAIL:
    for f in FAIL:
        print("  - %s" % f)
sys.exit(1 if FAIL else 0)
