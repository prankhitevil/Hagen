# -*- coding: utf-8 -*-
"""Проверка 80: вычёркиваемые пункты документа (17.09).

Идея 17.09: «он сделал саммари, пять блоков и пять решений протокола,
и чтобы я нерелевантные, которые считаю недостойными сохранения, сразу мог
вычеркнуть». Сделано так: щелчок по строке помечает её, в заметку она не уходит,
файл документа при этом не меняется и отметку всегда можно снять.

Отдельно — кнопка «Пересобрать без вычеркнутого»: вычеркнутое уходит модели
списком с запретом повторять, чтобы пересборка не была рулеткой.

Что проверяем:
  1. нормализация строки: маркеры списка, нумерация, разметка, регистр,
     точка в конце;
  2. какие строки вычёркивать можно, а какие нет (заголовки, разделители,
     подпись «Сформировано», разделитель таблицы);
  3. вырезание: помеченные строки уходят, остальное на месте, дыр не остаётся;
  4. заметка: вычеркнутого в ней нет, файл документа не тронут;
  5. документ, вычеркнутый целиком, не даёт пустого раздела в заметке;
  6. вставка в промпт при пересборке: список есть, запрет есть, и он подан
     как ДАННЫЕ;
  7. точка службы принимает отметки и обновляет заметку.

Служба поднимается в этом же процессе (Kaspersky не даёт плодить фоновые).
Запуск из корня проекта:  .venv\\Scripts\\python.exe tests\\t80_dropped_lines.py
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


ДОКУМЕНТ = """# Протокол совещания
17.09.2026, 00:12:00

## Принятые решения
- Смету пересматриваем к четвергу.
- Купить всем печенье.
- Нанимаем подрядчика на отделку.

## Задачи
| Задача | Ответственный | Срок |
| --- | --- | --- |
| Собрать смету | Пётр | к четвергу |
| Заказать печенье | Иван | не указан |

---
_Сформировано 17.09.2026 00:30, движок: claude_cli._
"""

print("=== 1. Нормализация строки ===")
ok("маркер списка не влияет",
   store.norm_line("- Купить всем печенье.") == store.norm_line("Купить всем печенье."))
ok("нумерация не влияет",
   store.norm_line("2) Купить печенье") == store.norm_line("Купить печенье"))
ok("жирный не влияет",
   store.norm_line("**Решение:** брать") == store.norm_line("Решение: брать"))
ok("регистр не влияет", store.norm_line("ПЕЧЕНЬЕ") == store.norm_line("печенье"))
ok("лишние пробелы не влияют", store.norm_line("а   б") == "а б")
ok("пустая строка даёт пусто", store.norm_line("   ") == "")
ok("точка в конце не влияет",
   store.norm_line("Купить печенье.") == store.norm_line("Купить печенье"))

print("\n=== 2. Что вычёркивать можно ===")
строки = minutes.droppable_lines(ДОКУМЕНТ)
тексты = [s.strip() for s in строки]
ok("заголовок не вычёркивается", not any(t.startswith("#") for t in тексты))
ok("разделитель не вычёркивается", "---" not in тексты)
ok("подпись «Сформировано» не вычёркивается",
   not any("Сформировано" in t for t in тексты))
ok("разделитель таблицы не вычёркивается", "| --- | --- | --- |" not in тексты)
ok("пункт списка вычёркивается", "- Купить всем печенье." in тексты)
ok("строка таблицы вычёркивается",
   any(t.startswith("| Заказать печенье") for t in тексты))

print("\n=== 3. Вырезание ===")
без = minutes.strip_dropped(ДОКУМЕНТ, ["- Купить всем печенье.",
                                       "| Заказать печенье | Иван | не указан |"])
ok("вычеркнутый пункт ушёл", "печенье" not in без.lower())
ok("соседние пункты на месте", "Смету пересматриваем" in без and "Собрать смету" in без)
ok("заголовки на месте", "## Задачи" in без and "## Принятые решения" in без)
ok("подпись на месте", "Сформировано" in без)
ok("дыр из пустых строк не осталось", "\n\n\n" not in без)
ok("пустой список ничего не меняет", minutes.strip_dropped(ДОКУМЕНТ, []) == ДОКУМЕНТ)
ok("вычёркивание по смыслу, а не по виду строки",
   "Купить всем печенье" not in minutes.strip_dropped(ДОКУМЕНТ, ["Купить всем печенье"]))

print("\n=== 4. Заметка и файл документа ===")
vault = Path(tempfile.mkdtemp(prefix="vault_t80_"))
config.save({"vault_path": str(vault), "vault_subfolder": "Meetings",
             "vault_confirmed": True})
rec_id = store.create(title="планёрка", source="live")["id"]
store.replace_segments(rec_id, [store.make_segment("mic", 0.0, 4.0, "Обсудили смету.")])
doc_file = store.rec_dir(rec_id) / minutes.DOC_KINDS["protocol"]["file"]
doc_file.write_text(ДОКУМЕНТ, encoding="utf-8")
store.update(rec_id, {"has_minutes": True, "dropped": {"protocol": ["- Купить всем печенье."]}})

res = obsidian.save_note(rec_id)
note = Path(res["path"]).read_text(encoding="utf-8")
ok("вычеркнутый пункт из заметки ушёл", "Купить всем печенье" not in note)
ok("однофамилец-строка таблицы осталась", "Заказать печенье" in note)
ok("остальное в заметке есть", "Смету пересматриваем" in note)
ok("файл документа не тронут", "Купить всем печенье" in doc_file.read_text(encoding="utf-8"))

store.update(rec_id, {"dropped": {}})
note2 = Path(obsidian.save_note(rec_id)["path"]).read_text(encoding="utf-8")
ok("отметку можно снять — пункт вернулся", "Купить всем печенье" in note2)

print("\n=== 5. Документ, вычеркнутый целиком ===")
store.update(rec_id, {"dropped": {"protocol": [s.strip() for s in minutes.droppable_lines(ДОКУМЕНТ)]}})
note3 = Path(obsidian.save_note(rec_id)["path"]).read_text(encoding="utf-8")
ok("пустого раздела протокола в заметке нет", "# Протокол" not in note3,
   str([ln for ln in note3.splitlines() if ln.startswith("# ")]))
ok("стенограмма на месте", "# Стенограмма" in note3)

print("\n=== 6. Вставка в промпт при пересборке ===")
meta = store.get(rec_id)
store.update(rec_id, {"dropped": {"protocol": ["- Купить всем печенье."]}})
note_txt = minutes._dropped_note(store.get(rec_id), "protocol")
ok("список вычеркнутого есть", "Купить всем печенье" in note_txt)
ok("запрет повторять есть", "Не включай их снова" in note_txt)
ok("запрет и на пересказ", "пересказом" in note_txt)
ok("подано как данные", "ДАННЫЕ" in note_txt)
ok("для другого документа вставки нет", minutes._dropped_note(store.get(rec_id), "meeting") == "")
ok("без вычеркнутого вставки нет", minutes._dropped_note({}, "protocol") == "")

print("\n=== 7. Точка службы ===")
from fastapi.testclient import TestClient  # noqa: E402
from hagen.server import app  # noqa: E402

ORIGIN = {"Origin": "http://127.0.0.1:8787"}
with TestClient(app, base_url="http://127.0.0.1:8787") as client:
    r = client.patch("/api/recordings/%s" % rec_id, headers=ORIGIN,
                     json={"dropped": {"protocol": ["- Купить всем печенье.",
                                                    "Купить всем печенье",
                                                    "   "]}})
    ok("PATCH принят", r.status_code == 200, r.text[:160])
    got = (r.json().get("dropped") or {}).get("protocol") or []
    ok("повторы схлопнулись и пустые убраны", len(got) == 1, str(got))
    note4 = Path(res["path"]).read_text(encoding="utf-8")
    ok("заметка обновилась сама", "Купить всем печенье" not in note4)

try:
    store.delete(rec_id)
except Exception:
    pass

print("\nВсего провалов: %d" % len(FAIL))
if FAIL:
    for f in FAIL:
        print("  - %s" % f)
sys.exit(1 if FAIL else 0)
