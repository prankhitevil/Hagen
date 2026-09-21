# -*- coding: utf-8 -*-
"""Проверка 90: папка проекта в сейфе (20.09).

Решение 20.09: у проекта может быть связанная папка в сейфе Obsidian. Связка
необязательная — проект может быть просто названием, как было. Но если папка
привязана, заметки записей этого проекта ложатся в неё, а не в папку категории,
и уже сохранённая заметка переезжает туда при следующем сохранении.

Переезд делает тот же механизм, что переносил заметку при смене категории
(`_resolve_target`), — второго способа двигать файлы не заводили.

Что проверяем:
  1. чистка пути: косые черты, края, «..» и двоеточие диска;
  2. привязка и снятие, регистр имени проекта не важен;
  3. место заметки: с привязкой — папка проекта, без неё — папка категории;
  4. переезд: сменили проект — заметка уехала, старого файла не осталось;
  5. папка вне сейфа связкой не считается;
  6. точки службы: привязка, список папок, подсказки в /api/labels.

Запуск из корня проекта:  .venv\\Scripts\\python.exe tests\\t90_project_folder.py
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


print("=== 1. Чистка пути папки ===")
ok("обратные косые приводятся к прямым",
   store.clean_project_folder("Projects\\АКП") == "Projects/АКП")
ok("края чистятся", store.clean_project_folder("  /Projects/АКП/  ") == "Projects/АКП")
ok("двойные косые схлопываются",
   store.clean_project_folder("Projects//АКП") == "Projects/АКП")
ok("пусто остаётся пустым", store.clean_project_folder(None) == "")
ok("переход вверх отбрасывается", store.clean_project_folder("../../Windows") == "",
   store.clean_project_folder("../../Windows"))
ok("переход вверх в середине тоже",
   store.clean_project_folder("Projects/../../Windows") == "")
ok("путь с диском отбрасывается", store.clean_project_folder("C:\\Windows") == "")

print("\n=== 2. Привязка и снятие ===")
config.save({"projects": [], "rec_tags": [], "project_folders": {}})
store.set_project_folder("АКП Digital", "Projects/АКП")
ok("папка привязалась", store.project_folder("АКП Digital") == "Projects/АКП",
   store.project_folder("АКП Digital"))
ok("регистр имени не важен", store.project_folder("акп digital") == "Projects/АКП")
ok("проект попал в подсказки", "АКП Digital" in store.known_projects(),
   str(store.known_projects()))
ok("у чужого проекта папки нет", store.project_folder("Другой") == "")
store.set_project_folder("АКП Digital", "")
ok("пустая папка снимает связку", store.project_folder("АКП Digital") == "")
ok("проект в подсказках остался", "АКП Digital" in store.known_projects())
try:
    store.set_project_folder("", "Projects/АКП")
    ok("пустой проект не принимается", False, "исключения не было")
except ValueError:
    ok("пустой проект не принимается", True)

print("\n=== 3. Место заметки ===")
vault = Path(tempfile.mkdtemp(prefix="vault_t90_"))
(vault / "Projects" / "АКП").mkdir(parents=True, exist_ok=True)
(vault / "Projects" / "Смежный").mkdir(parents=True, exist_ok=True)
config.save({"vault_path": str(vault), "vault_subfolder": "Meetings",
             "vault_confirmed": True, "categories": ["Встречи"],
             "default_category": "Встречи", "project_folders": {}})

rec_id = store.create(title="планёрка по смете", source="live")["id"]
store.replace_segments(rec_id, [
    store.make_segment("mic", 0.0, 4.0, "Обсудили смету."),
    store.make_segment("far", 4.0, 8.0, "Согласовали срок.", speaker="Пётр"),
])
store.update(rec_id, {"category": "Встречи", "project": "АКП Digital"})

res = obsidian.save_note(rec_id)
by_cat = Path(res["path"])
ok("без связки заметка в папке категории", by_cat.parent.name == "Встречи",
   str(by_cat.parent))

store.set_project_folder("АКП Digital", "Projects/АКП")
meta = store.get(rec_id)
ok("папка проекта посчиталась",
   obsidian.project_dir(meta) == vault / "Projects" / "АКП",
   str(obsidian.project_dir(meta)))

print("\n=== 4. Переезд заметки ===")
res2 = obsidian.refresh_note(rec_id)
moved = Path(res2["path"])
ok("заметка уехала в папку проекта",
   moved.parent == vault / "Projects" / "АКП", str(moved.parent))
ok("в папке категории её больше нет", not by_cat.exists(), str(by_cat))
ok("текст заметки на месте", "Обсудили смету." in moved.read_text(encoding="utf-8"))
ok("путь записи обновился",
   str(store.get(rec_id).get("vault_path")) == str(moved))

# Смена проекта у записи: заметка должна уехать в папку нового проекта.
store.set_project_folder("Смежный", "Projects/Смежный")
store.update(rec_id, {"project": "Смежный"})
res3 = obsidian.refresh_note(rec_id)
moved2 = Path(res3["path"])
ok("смена проекта уводит заметку в его папку",
   moved2.parent == vault / "Projects" / "Смежный", str(moved2.parent))
ok("из папки прошлого проекта заметка ушла", not moved.exists())

# Связку сняли — заметка возвращается к категории.
store.set_project_folder("Смежный", "")
res4 = obsidian.refresh_note(rec_id)
back = Path(res4["path"])
ok("без связки заметка снова по категории", back.parent.name == "Встречи",
   str(back.parent))

print("\n=== 5. Папка вне сейфа ===")
store.set_project_folder("Смежный", "Projects/Смежный")
config.save({"project_folders": {"Смежный": "Projects/Смежный/../../../Users"}})
ok("путь с переходом вверх до настроек не доходит",
   store.project_folder("Смежный") == "", store.project_folder("Смежный"))

print("\n=== 6. Точки службы ===")
from fastapi.testclient import TestClient  # noqa: E402
from hagen.server import app  # noqa: E402

ORIGIN = {"Origin": "http://127.0.0.1:8787"}
with TestClient(app, base_url="http://127.0.0.1:8787") as client:
    r = client.post("/api/projects/folder",
                    json={"project": "АКП Digital", "folder": "Projects\\АКП"},
                    headers=ORIGIN)
    ok("привязка принята", r.status_code == 200, r.text[:200])
    folders = (r.json() if r.status_code == 200 else {}).get("folders") or {}
    ok("папка почищена и сохранена", folders.get("АКП Digital") == "Projects/АКП",
       str(folders))

    r2 = client.post("/api/projects/folder", json={"project": "", "folder": "X"},
                     headers=ORIGIN)
    ok("без проекта привязка отклоняется", r2.status_code == 400, str(r2.status_code))

    r3 = client.get("/api/vault/folders", headers=ORIGIN)
    ok("список папок отвечает", r3.status_code == 200)
    paths = [f["path"] for f in (r3.json() if r3.status_code == 200 else {}).get("folders", [])]
    ok("папки сейфа нашлись", "Projects/АКП" in paths, str(paths[:6]))

    r4 = client.get("/api/vault/folders?q=смеж", headers=ORIGIN)
    found = [f["path"] for f in (r4.json() if r4.status_code == 200 else {}).get("folders", [])]
    ok("отбор по названию работает", found == ["Projects/Смежный"], str(found))

    r5 = client.get("/api/labels", headers=ORIGIN)
    labels = r5.json() if r5.status_code == 200 else {}
    ok("/api/labels отдаёт папки проектов",
       (labels.get("folders") or {}).get("АКП Digital") == "Projects/АКП",
       str(labels.get("folders")))

try:
    store.delete(rec_id)
except Exception:
    pass

print("\nВсего провалов: %d" % len(FAIL))
if FAIL:
    for f in FAIL:
        print("  - %s" % f)
sys.exit(1 if FAIL else 0)
