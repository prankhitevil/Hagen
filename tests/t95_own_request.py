# -*- coding: utf-8 -*-
"""Проверка 95: своё поле «что сделать» (20.09).

Раньше рядом с готовыми видами документов был только «вопрос к стенограмме» —
ответ одной репликой. Теперь то же поле принимает и заказ документа («письмо
поставщику с итогами и сроками»): промпт понимает оба рода просьбы, а всё
остальное — общие правила, роли участников, раздел в заметке — берётся как у
любого другого документа.

Ключ вида и файл прежние (`question`, `qa.md`): сделанные раньше вопросы и
ответы остаются на месте и копятся в том же файле.

Что проверяем:
  1. вид называется «Свой запрос» и объясняет оба случая;
  2. в промпт попадает текст человека — как ДАННЫЕ, под своей подписью;
  3. промпт объясняет модели оба рода просьбы и запрещает выдумывать;
  4. пустая просьба — отказ, а не пустой документ;
  5. общие правила, роли участников и защита «это данные» остаются на месте;
  6. свой текст промпта («Настройки → Обработка») по-прежнему заменяет
     исходный, а английские инструкции переведены тоже;
  7. точка службы принимает запрос и отвергает пустой.

Запуск из корня проекта:  .venv\\Scripts\\python.exe tests\\t95_own_request.py
"""
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
sys.path.insert(0, str(PROJECT / "tests"))
import isolate  # noqa: E402

isolate.voices()
isolate.settings()

from hagen import config, minutes, store, voices  # noqa: E402

FAIL = []


def ok(label, cond, detail=""):
    print("   %s   %s%s" % ("ok  " if cond else "ПРОВАЛ", label,
                            ("" if not detail else ": %s" % detail)))
    if not cond:
        FAIL.append(label)


print("=== 1. Вид документа ===")
kind = minutes.DOC_KINDS["question"]
ok("называется «Свой запрос»", kind["title"] == "Свой запрос", kind["title"])
ok("подсказка объясняет оба случая",
   "спросить" in kind["hint"].lower() and "заказать" in kind["hint"].lower(), kind["hint"])
ok("файл прежний — старые ответы не теряются", kind["file"] == "qa.md", kind["file"])
ok("раздел заметки назван по смыслу", kind["heading"] == "## Свои запросы", kind["heading"])
ok("вид предлагается в окне документа",
   minutes.TEMPLATES["question"]["needs_question"] is True, str(minutes.TEMPLATES["question"]))

print("\n=== 2. Просьба в промпте ===")
config.save({"prompt_lang": "ru", "prompt_overrides": {}})
REQUEST = "Письмо поставщику с итогами и сроками"
prompt = minutes._final_prompt("question", REQUEST)
ok("текст человека попал в промпт", REQUEST in prompt, prompt[-300:])
ok("просьба подписана", "Просьба пользователя:" in prompt,
   [ln for ln in prompt.splitlines() if "ользовател" in ln])
ok("подпись стоит перед самой просьбой",
   prompt.index("Просьба пользователя:") < prompt.index(REQUEST))

print("\n=== 3. Что сказано модели ===")
task = minutes.task_text("question")
ok("объяснён случай «вопрос»", "ВОПРОС" in task, task[:120])
ok("объяснён случай «заказ документа»", "ЗАКАЗ ДОКУМЕНТА" in task, task[:200])
ok("запрещено выдумывать", "не выдумывай" in task and "не сочиняй" in task, task)
ok("велено не писать предисловий", "без предисловий" in task, task)
ok("задан вид первой строки", "заголовок первого уровня" in task, task)

print("\n=== 4. Пустая просьба ===")
for empty in ("", "   ", None):
    try:
        minutes._final_prompt("question", empty)
        ok("пустая просьба отвергается (%r)" % empty, False, "исключения не было")
    except ValueError as err:
        ok("пустая просьба отвергается (%r)" % empty, "что сделать" in str(err).lower(), str(err))

print("\n=== 5. Общие правила на месте ===")
ok("защита «стенограмма — данные, а не команды» осталась",
   "ДАННЫЕ" in prompt or "данные" in prompt, prompt[:200])
ok("правило оформления ответа осталось", prompt.strip().endswith(minutes.tail_rules("ru").strip())
   or minutes.tail_rules("ru").strip()[:40] in prompt, prompt[-200:])

# Роли участников приезжают той же шапкой стенограммы, что и у
# остальных документов: своего пути у «Своего запроса» нет.
person = voices.create_person("Иван Петров")
voices.set_role(person["id"], "supplier", "коммерческий директор")
rec_id = store.create(title="Переговоры", source="live")["id"]
store.replace_segments(rec_id, [
    store.make_segment("far", 0.0, 4.0, "Отгрузим в пятницу.", speaker="Иван Петров"),
])
head = minutes.build_transcript_text(rec_id)
ok("роли участников доезжают и сюда",
   "Иван Петров — поставщик, коммерческий директор" in head,
   [ln for ln in head.splitlines() if ln.startswith("Участники")])

print("\n=== 6. Свой текст и язык инструкций ===")
config.save({"prompt_overrides": {"question": "Задача: сделай по-своему."}})
ok("свой текст заменяет исходный",
   minutes.task_text("question").strip() == "Задача: сделай по-своему.",
   minutes.task_text("question"))
ok("подмена видна в настройках", minutes.task_overridden("question") is True)
config.save({"prompt_overrides": {}})
config.save({"prompt_lang": "en"})
en = minutes.task_text("question")
ok("английские инструкции тоже про оба случая",
   "QUESTION" in en and "ORDER FOR A DOCUMENT" in en, en[:160])
en_prompt = minutes._final_prompt("question", REQUEST)
ok("подпись просьбы по-английски", "User's request:" in en_prompt,
   [ln for ln in en_prompt.splitlines() if "request" in ln][:2])
ok("сам текст просьбы не переведён", REQUEST in en_prompt)
config.save({"prompt_lang": "ru"})

print("\n=== 7. Точка службы ===")
from fastapi.testclient import TestClient  # noqa: E402
from hagen.server import app  # noqa: E402

ORIGIN = {"Origin": "http://127.0.0.1:8787"}
with TestClient(app, base_url="http://127.0.0.1:8787") as client:
    r = client.get("/api/documents/kinds?rec_id=%s" % rec_id, headers=ORIGIN)
    ok("окно документа отвечает", r.status_code == 200, r.text[:160])
    docs = (r.json() if r.status_code == 200 else {}).get("docs") or []
    own = [d for d in docs if d.get("key") == "question"]
    ok("«Свой запрос» есть в списке видов",
       own and own[0].get("title") == "Свой запрос", str(docs)[:200])

    r2 = client.post("/api/recordings/%s/document" % rec_id,
                     json={"doc": "question", "question": ""}, headers=ORIGIN)
    ok("пустая просьба — отказ службы", r2.status_code == 400, str(r2.status_code))

try:
    store.delete(rec_id)
except Exception:
    pass

print("\nВсего провалов: %d" % len(FAIL))
if FAIL:
    for f in FAIL:
        print("  - %s" % f)
sys.exit(1 if FAIL else 0)
