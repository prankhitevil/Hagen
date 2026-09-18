# -*- coding: utf-8 -*-
"""Проверка 83: поручения наружу, в Todoist (17.09).

Решение 17.09: «не заводить задачи у себя, а ОТПРАВЛЯТЬ». Своего
списка задач в программе нет и не будет.

Это единственное действие программы, которое уходит за пределы компьютера и
не отменяется кнопкой «отмена», поэтому заслоны стоят в СЛУЖБЕ, а не в
интерфейсе: интерфейс можно обойти, службу — нет.

Что проверяем:
  1. предпросмотр ничего не отправляет и честно говорит, задан ли токен;
  2. без явного confirmed служба отказывает;
  3. отправляется ровно выбранное, и в понятном виде: строка таблицы протокола
     превращается в «Задача (Ответственный, срок)», срок НЕ превращается в дату;
  4. отметка «уже отправлено» ставится и повтор не создаёт второй задачи —
     даже если формулировка слегка изменилась;
  5. объём за раз ограничен;
  6. сбой сети не молчит и не делает вид, что всё отправлено;
  7. токен не попадает ни в ответ службы, ни в текст ошибки;
  8. пустой выбор и отсутствующая запись отвергаются.

В сеть НЕ ходим: слой запросов подменён. Ни одна настоящая задача не создаётся.
Запуск из корня проекта:  .venv\\Scripts\\python.exe tests\\t83_tasks_out.py
"""
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
sys.path.insert(0, str(PROJECT / "tests"))
import isolate  # noqa: E402

isolate.voices()
isolate.settings()

from hagen import config, minutes, store, todoist  # noqa: E402

FAIL = []
ТОКЕН = "todoist-secret-0123456789abcdef"


def ok(label, cond, detail=""):
    # Печатаем через ascii-замену: консоль Windows бывает в cp1251, и на
    # стрелке «→» из текста задачи проверка падала UnicodeEncodeError — то есть
    # отказывала не по делу, а на печати (нашлось общим прогоном 17.09).
    line = "   %s   %s%s" % ("ok  " if cond else "ПРОВАЛ", label,
                             ("" if not detail else ": %s" % detail))
    try:
        print(line)
    except UnicodeEncodeError:
        print(line.encode("ascii", "replace").decode("ascii"))
    if not cond:
        FAIL.append(label)


ПРОТОКОЛ = """# Протокол совещания

## Задачи
| Задача | Ответственный | Срок |
| --- | --- | --- |
| Собрать смету | Пётр | к четвергу |
| Позвонить в банк | Иван | не указан |
| Прибраться | не определён | не указан |
"""

rec_id = store.create(title="Планёрка", source="live")["id"]
store.replace_segments(rec_id, [store.make_segment("mic", 0.0, 4.0, "Обсудили смету.")])
(store.rec_dir(rec_id) / minutes.DOC_KINDS["protocol"]["file"]).write_text(
    ПРОТОКОЛ, encoding="utf-8")
store.update(rec_id, {"has_minutes": True})

# ---- подмена сети ----------------------------------------------------------
ОТПРАВЛЕНО = []
СБОЙ = {"on": False}


def _fake_request(method, path, **kw):
    if СБОЙ["on"]:
        raise todoist.TodoistError("Не удалось связаться с Todoist. Проверьте интернет.")
    if method == "GET" and path == "/projects":
        return [{"id": "11", "name": "Работа"}, {"id": "22", "name": "Личное"}]
    if method == "POST" and path == "/tasks":
        ОТПРАВЛЕНО.append(kw.get("json") or {})
        return {"id": "t%d" % len(ОТПРАВЛЕНО),
                "url": "https://todoist.com/showTask?id=t%d" % len(ОТПРАВЛЕНО)}
    raise AssertionError("неожиданный запрос %s %s" % (method, path))


todoist._request = _fake_request

print("=== 1. Предпросмотр ===")
config.save({"todoist_token": ""})
pre = todoist.preview(rec_id)
ok("сказано, что токена нет", pre["configured"] is False)
ok("поручения всё равно показаны", len(pre["items"]) == 3, str(len(pre["items"])))
ok("предпросмотр ничего не отправил", not ОТПРАВЛЕНО)
config.save({"todoist_token": ТОКЕН})
pre = todoist.preview(rec_id)
ok("с токеном сказано, что можно", pre["configured"] is True)
ok("ничего ещё не отправлено", all(not i["sent"] for i in pre["items"]))

print("\n=== 2. Понятный вид задачи ===")
ok("строка таблицы разобрана",
   todoist.clean_task("| Собрать смету | Пётр | к четвергу |") == "Собрать смету (Пётр, к четвергу)")
ok("пустые «не определён» и «не указан» отброшены",
   todoist.clean_task("| Прибраться | не определён | не указан |") == "Прибраться")
ok("часть данных тоже работает",
   todoist.clean_task("| Позвонить в банк | Иван | не указан |") == "Позвонить в банк (Иван)")
ok("маркер списка снят", todoist.clean_task("- Иван пришлёт договор") == "Иван пришлёт договор")

print("\n=== 3. Без подтверждения не отправляем ===")
try:
    todoist.send(rec_id, ["| Собрать смету | Пётр | к четвергу |"], "11", confirmed=False)
    ok("отказ без confirmed", False, "отправил без подтверждения")
except todoist.TodoistError as err:
    ok("отказ без confirmed", "не подтверждена" in str(err), str(err))
ok("и правда ничего не ушло", not ОТПРАВЛЕНО)

print("\n=== 4. Отправка ===")
res = todoist.send(rec_id, ["| Собрать смету | Пётр | к четвергу |",
                            "| Позвонить в банк | Иван | не указан |"],
                   "11", confirmed=True)
ok("создано две задачи", len(res["created"]) == 2, str(res))
ok("ушло ровно две", len(ОТПРАВЛЕНО) == 2)
ok("текст задачи человеческий", ОТПРАВЛЕНО[0]["content"] == "Собрать смету (Пётр, к четвергу)",
   str(ОТПРАВЛЕНО[0]))
ok("проект передан", ОТПРАВЛЕНО[0].get("project_id") == "11")
ok("в описании видно, откуда задача", "Планёрка" in ОТПРАВЛЕНО[0].get("description", ""))
ok("срок НЕ превращён в дату задачи", "due_string" not in ОТПРАВЛЕНО[0]
   and "due_date" not in ОТПРАВЛЕНО[0], str(ОТПРАВЛЕНО[0]))
ok("третья задача не отправлена", all("Прибраться" not in t["content"] for t in ОТПРАВЛЕНО))

print("\n=== 5. Отметка «уже отправлено» ===")
pre2 = todoist.preview(rec_id)
sent = [i for i in pre2["items"] if i["sent"]]
ok("две отмечены отправленными", len(sent) == 2, str([i["clean"] for i in sent]))
ok("ссылка сохранена", all(i["url"] for i in sent))
before = len(ОТПРАВЛЕНО)
res2 = todoist.send(rec_id, ["| Собрать смету | Пётр | к четвергу |"], "11", confirmed=True)
ok("повтор не создал задачу", len(ОТПРАВЛЕНО) == before, "ушло ещё %d" % (len(ОТПРАВЛЕНО) - before))
ok("повтор назван пропущенным", len(res2["skipped"]) == 1 and not res2["created"])
res3 = todoist.send(rec_id, ["Собрать смету (слегка иначе)"], "11", confirmed=True)
ok("другая формулировка — это другая задача", len(ОТПРАВЛЕНО) == before + 1)
res4 = todoist.send(rec_id, ["- собрать смету, Пётр, к четвергу."], "11", confirmed=True)
ok("та же по смыслу строка повтором не уходит",
   len(ОТПРАВЛЕНО) == before + 2, "ушло %d" % (len(ОТПРАВЛЕНО) - before))

print("\n=== 6. Границы ===")
try:
    todoist.send(rec_id, [], "11", confirmed=True)
    ok("пустой выбор отвергнут", False)
except todoist.TodoistError as err:
    ok("пустой выбор отвергнут", "ни одного" in str(err), str(err))
try:
    todoist.send(rec_id, ["т%d" % i for i in range(todoist.MAX_BATCH + 1)], "", confirmed=True)
    ok("слишком много за раз отвергнуто", False)
except todoist.TodoistError as err:
    ok("слишком много за раз отвергнуто", "не больше" in str(err), str(err))
try:
    todoist.send("20200101-000000-0000", ["что-то"], "", confirmed=True)
    ok("несуществующая запись отвергнута", False)
except todoist.TodoistError as err:
    ok("несуществующая запись отвергнута", "не найдена" in str(err), str(err))

print("\n=== 7. Сбой сети ===")
СБОЙ["on"] = True
res5 = todoist.send(rec_id, ["Совсем новое поручение"], "11", confirmed=True)
ok("сбой не выдан за успех", not res5["created"] and res5["failed"], str(res5))
ok("ошибка по-русски", "интернет" in res5["failed"][0]["error"].lower(),
   res5["failed"][0]["error"])
СБОЙ["on"] = False
pre3 = todoist.preview(rec_id)
ok("несостоявшаяся задача не помечена отправленной",
   not any("Совсем новое" in i["clean"] and i["sent"] for i in pre3["items"]))

print("\n=== 8. Токен наружу не течёт ===")
весь_ответ = repr(pre3) + repr(res) + repr(res5)
ok("токена нет в ответах службы", ТОКЕН not in весь_ответ)
ok("токена нет в сохранённой записи", ТОКЕН not in repr(store.get(rec_id)))
config.save({"todoist_token": ""})
for что, зов in (("список проектов", lambda: todoist.projects()),
                 ("отправка", lambda: todoist.send(rec_id, ["новое"], "", confirmed=True)),
                 ("сборка заголовков", lambda: todoist._headers())):
    try:
        зов()
        ok("без токена отказ: %s" % что, False, "прошло без токена")
    except todoist.TodoistError as err:
        ok("без токена отказ: %s" % что, "не задан" in str(err), str(err))

print("\n=== 9. Точка службы ===")
config.save({"todoist_token": ТОКЕН})
from fastapi.testclient import TestClient  # noqa: E402
from hagen.server import app  # noqa: E402

ORIGIN = {"Origin": "http://127.0.0.1:8787"}
with TestClient(app, base_url="http://127.0.0.1:8787") as client:
    r = client.get("/api/tasks/%s" % rec_id, headers=ORIGIN)
    ok("предпросмотр отвечает", r.status_code == 200, r.text[:160])
    ok("токена в ответе нет", ТОКЕН not in r.text)
    r2 = client.post("/api/tasks/%s/send" % rec_id, headers=ORIGIN,
                     json={"texts": ["| Прибраться | не определён | не указан |"]})
    ok("служба отказывает без подтверждения", r2.status_code == 400, "код %d" % r2.status_code)
    ok("причина названа", "не подтверждена" in r2.text, r2.text[:120])
    r3 = client.get("/api/prompts", headers=ORIGIN)
    ok("в настройках только признак токена",
       r3.json().get("todoist_set") is True and ТОКЕН not in r3.text)

config.save({"todoist_token": ""})
try:
    store.delete(rec_id)
except Exception:
    pass

print("\nВсего провалов: %d" % len(FAIL))
if FAIL:
    for f in FAIL:
        print("  - %s" % f)
sys.exit(1 if FAIL else 0)
