# -*- coding: utf-8 -*-
"""Проверка 81: «Продолжение предыдущей записи» (17.09).

Решение 17.09: в новую встречу тянем НЕ весь
прошлый протокол, а только незакрытые пункты. Серию программа не угадывает,
связь выбирает человек: у еженедельной планёрки и случайной встречи с тем же
названием разная судьба, ошибиться дороже, чем не подсказать.

Признака «выполнено» у поручений в программе пока нет, и выдумывать его здесь
не стали: единственная отметка — вычёркивание (проверка 80), ею и пользуемся.
Поэтому раздел честно называется «С прошлого раза осталось».

Что проверяем:
  1. сбор поручений: из разделов «Задачи» и «Договорённости», шапка и
     разделитель таблицы не попадают, «нет» не попадает, повторы схлопываются;
  2. вычеркнутое в прошлой записи в следующую не тянется;
  3. раздел в заметке: название прошлой записи, дата, сами пункты;
  4. главных разделов в заметке не прибавляется (формат 15.09);
  5. прошлая запись без незакрытых пунктов даёт честную строчку, а не пустоту;
  6. служба не даёт сослаться на саму себя и на несуществующую запись;
  7. снятие связи убирает раздел.

Служба поднимается в этом же процессе (Kaspersky не даёт плодить фоновые).
Запуск из корня проекта:  .venv\\Scripts\\python.exe tests\\t81_carry_over.py
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

from hagen import config, minutes, obsidian, store  # noqa: E402

FAIL = []


def ok(label, cond, detail=""):
    print("   %s   %s%s" % ("ok  " if cond else "ПРОВАЛ", label,
                            ("" if not detail else ": %s" % detail)))
    if not cond:
        FAIL.append(label)


ПРОТОКОЛ = """# Протокол совещания
10.09.2026, 00:40:00

## Принятые решения
- Подрядчика берём.

## Задачи
| Задача | Ответственный | Срок |
| --- | --- | --- |
| Собрать смету | Пётр | к четвергу |
| Заказать печенье | Иван | не указан |
| Собрать смету | Пётр | к четвергу |

## Открытые вопросы
нет

---
_Сформировано 10.09.2026 01:00, движок: claude_cli._
"""

САММАРИ = """# Саммари
## Задачи и договорённости
- Иван пришлёт договор.
- нет
"""

vault = Path(tempfile.mkdtemp(prefix="vault_t81_"))
config.save({"vault_path": str(vault), "vault_subfolder": "Meetings",
             "vault_confirmed": True})

prev_id = store.create(title="Планёрка 10.09", source="live")["id"]
store.replace_segments(prev_id, [store.make_segment("mic", 0.0, 4.0, "Обсудили смету.")])
(store.rec_dir(prev_id) / minutes.DOC_KINDS["protocol"]["file"]).write_text(
    ПРОТОКОЛ, encoding="utf-8")
(store.rec_dir(prev_id) / minutes.DOC_KINDS["meeting"]["file"]).write_text(
    САММАРИ, encoding="utf-8")
store.update(prev_id, {"has_minutes": True})

print("=== 1. Сбор поручений ===")
items = minutes.open_items(prev_id)
ok("поручения из таблицы собраны",
   any("Собрать смету" in i for i in items), str(items))
ok("поручения из саммари собраны", any("пришлёт договор" in i for i in items), str(items))
ok("шапка таблицы не попала", not any(i.lower().startswith("| задача") for i in items))
ok("разделитель таблицы не попал", not any(set(i.strip()) <= set("|-: ") for i in items))
ok("«нет» не попало", not any(store.norm_line(i) == "нет" for i in items))
ok("решения не попали", not any("Подрядчика берём" in i for i in items), str(items))
ok("повтор схлопнулся",
   len([i for i in items if "Собрать смету" in i]) == 1, str(items))

print("\n=== 2. Вычеркнутое не тянется ===")
store.update(prev_id, {"dropped": {"protocol": ["| Заказать печенье | Иван | не указан |"]}})
items2 = minutes.open_items(prev_id)
ok("вычеркнутое поручение ушло", not any("печенье" in i.lower() for i in items2), str(items2))
ok("остальные на месте", any("Собрать смету" in i for i in items2))

print("\n=== 3. Раздел в заметке ===")
new_id = store.create(title="Планёрка 17.09", source="live")["id"]
store.replace_segments(new_id, [store.make_segment("mic", 0.0, 4.0, "Продолжаем.")])
store.update(new_id, {"continues": prev_id})
res = obsidian.save_note(new_id)
note = Path(res["path"]).read_text(encoding="utf-8")
ok("раздел появился", obsidian.H_CARRY in note)
ok("названа прошлая запись", "Планёрка 10.09" in note)
дата_прошлой = obsidian._meta_dt(store.get(prev_id)).strftime("%d.%m.%Y")
ok("названа её дата", дата_прошлой in note, дата_прошлой)
ok("пункт перенесён", "Собрать смету" in note)
ok("вычеркнутое не перенеслось", "печенье" not in note.lower())
ok("весь прошлый протокол НЕ утянут", "Подрядчика берём" not in note)
ok("раздел до стенограммы", note.index(obsidian.H_CARRY) < note.index("# Стенограмма"))

print("\n=== 4. Формат заметки ===")
главные = [ln for ln in note.splitlines() if ln.startswith("# ")]
ok("главных разделов не прибавилось", главные == ["# О записи", "# Стенограмма"], str(главные))

print("\n=== 5. Прошлая запись без пунктов ===")
пустая = store.create(title="Разговор ни о чём", source="live")["id"]
store.replace_segments(пустая, [store.make_segment("mic", 0.0, 2.0, "Привет.")])
store.update(new_id, {"continues": пустая})
note2 = Path(obsidian.save_note(new_id)["path"]).read_text(encoding="utf-8")
ok("раздел есть", obsidian.H_CARRY in note2)
ok("сказано честно, что пунктов нет", "Незакрытых пунктов" in note2)

print("\n=== 6. Служба ===")
from fastapi.testclient import TestClient  # noqa: E402
from hagen.server import app  # noqa: E402

ORIGIN = {"Origin": "http://127.0.0.1:8787"}
with TestClient(app, base_url="http://127.0.0.1:8787") as client:
    r = client.patch("/api/recordings/%s" % new_id, headers=ORIGIN,
                     json={"continues": new_id})
    ok("на саму себя сослаться нельзя", r.status_code == 400, "код %d" % r.status_code)
    r2 = client.patch("/api/recordings/%s" % new_id, headers=ORIGIN,
                      json={"continues": "20200101-000000-0000"})
    ok("на несуществующую нельзя", r2.status_code == 400, "код %d" % r2.status_code)
    r3 = client.patch("/api/recordings/%s" % new_id, headers=ORIGIN,
                      json={"continues": prev_id})
    ok("на настоящую можно", r3.status_code == 200, r3.text[:160])
    note3 = Path(res["path"]).read_text(encoding="utf-8")
    ok("заметка обновилась сама", "Планёрка 10.09" in note3)

    print("\n=== 7. Снятие связи ===")
    r4 = client.patch("/api/recordings/%s" % new_id, headers=ORIGIN, json={"continues": ""})
    ok("связь снимается", r4.status_code == 200 and not r4.json().get("continues"))
    note4 = Path(res["path"]).read_text(encoding="utf-8")
    ok("раздел из заметки ушёл", obsidian.H_CARRY not in note4)

for rid in (prev_id, new_id, пустая):
    try:
        store.delete(rid)
    except Exception:
        pass

print("\nВсего провалов: %d" % len(FAIL))
if FAIL:
    for f in FAIL:
        print("  - %s" % f)
sys.exit(1 if FAIL else 0)
