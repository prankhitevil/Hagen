# -*- coding: utf-8 -*-
"""Проверка 92: роли участников в документах (20.09).

Раньше программа роли УГАДЫВАЛА: протокол просил модель назвать участников «и,
если видно из разговора, их роли». Теперь роль известна заранее и у неё две оси:

  * СТОРОНА — кем человек приходится владельцу записи (наша сторона, покупатель,
    поставщик...). «Коллега-юрист» и «юрист поставщика» — разные люди для
    протокола, хотя должность одна. Сторона даёт документу то, чего у него не
    было вовсе: чьи это обязательства;
  * ДОЛЖНОСТЬ — кем человек работает. Свободный текст.

Роль живёт у человека в базе голосов и действует во всех записях; подрядчик по
одному проекту бывает партнёром по другому — тогда роль переопределяется в
самой записи. Своя роль («Я») — в настройках рядом с именем владельца.

В промпты роли уходят ОДНИМ местом — шапкой стенограммы (`participants_block`),
чтобы каждый вид документа получал их одинаково.

Что проверяем:
  1. чистка стороны и должности, роль одной строкой;
  2. роль у человека в базе: ставится, меняется, снимается;
  3. старшинство: роль записи перебивает постоянную, своя берётся из настроек;
  4. шапка стенограммы: участники с ролями, пояснение сторон, без ролей — как было;
  5. чистка ролей записи: пустые и безымянные отбрасываются;
  6. точки службы: роль человека, роль в записи, список сторон.

Запуск из корня проекта:  .venv\\Scripts\\python.exe tests\\t92_roles.py
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


print("=== 1. Чистка роли ===")
ok("сторона из списка принимается", voices.clean_side("supplier") == "supplier")
ok("регистр не важен", voices.clean_side("SUPPLIER") == "supplier")
ok("чужая сторона отбрасывается", voices.clean_side("начальник") == "")
ok("пусто остаётся пустым", voices.clean_side(None) == "")
ok("должность чистится от лишних пробелов",
   voices.clean_position("  коммерческий   директор ") == "коммерческий директор")
ok("длина должности ограничена",
   len(voices.clean_position("д" * 200)) == voices.MAX_POSITION)
ok("роль одной строкой",
   voices.role_line("supplier", "коммерческий директор") == "поставщик, коммерческий директор")
ok("одна должность без стороны", voices.role_line("", "юрист") == "юрист")
ok("роли нет — пустая строка", voices.role_line("", "") == "")

print("\n=== 2. Роль у человека в базе ===")
person = voices.create_person("Иван Петров")
pid = person["id"]
ok("у нового человека роли нет",
   person.get("side") == "" and person.get("position") == "", str(person.get("side")))
res = voices.set_role(pid, "supplier", "коммерческий директор")
ok("роль поставилась",
   res["side"] == "supplier" and res["position"] == "коммерческий директор", str(res))
ok("роль видна в карточке человека",
   (voices.get_person(pid) or {}).get("side") == "supplier")
voices.set_role(pid, "partner", "")
ok("роль меняется", (voices.get_person(pid) or {}).get("side") == "partner")
voices.set_role(pid, "", "")
ok("роль снимается",
   (voices.get_person(pid) or {}).get("side") == ""
   and (voices.get_person(pid) or {}).get("position") == "")
voices.set_role(pid, "supplier", "коммерческий директор")
try:
    voices.set_role("нет-такого", "supplier", "")
    ok("роль несуществующему не ставится", False, "исключения не было")
except ValueError:
    ok("роль несуществующему не ставится", True)

print("\n=== 3. Старшинство ролей ===")
config.save({"owner_name": "Орлов", "owner_side": "buyer",
             "owner_position": "руководитель закупок"})
ok("своя роль берётся из настроек",
   minutes._role_of("Орлов", {}) == ("buyer", "руководитель закупок"),
   str(minutes._role_of("Орлов", {})))
ok("роль человека берётся из базы",
   minutes._role_of("Иван Петров", {}) == ("supplier", "коммерческий директор"),
   str(minutes._role_of("Иван Петров", {})))
here = {"roles": {"Иван Петров": {"side": "partner", "position": "директор по развитию"}}}
ok("роль записи перебивает постоянную",
   minutes._role_of("Иван Петров", here) == ("partner", "директор по развитию"),
   str(minutes._role_of("Иван Петров", here)))
ok("постоянная роль при этом не тронута",
   (voices.get_person(pid) or {}).get("side") == "supplier")
mine = {"roles": {"Я": {"side": "seller", "position": ""}}}
ok("свою роль в записи тоже можно переопределить — и по «Я»",
   minutes._role_of("Орлов", mine) == ("seller", ""), str(minutes._role_of("Орлов", mine)))
ok("незнакомый человек остаётся без роли",
   minutes._role_of("Белова", {}) == ("", ""))

print("\n=== 4. Шапка стенограммы ===")
block = minutes.participants_block(["Орлов", "Иван Петров", "Белова"], {})
text = "\n".join(block)
ok("участники с ролями в одной строке",
   "Орлов — покупатель, руководитель закупок" in text
   and "Иван Петров — поставщик, коммерческий директор" in text, block[0])
ok("человек без роли остаётся просто именем", "; Белова" in block[0], block[0])
ok("пояснение сторон приложено", "Кто есть кто:" in text, text)
ok("пояснены только встреченные стороны",
   "покупатель:" in text and "поставщик:" in text and "подрядчик" not in text, text)
ok("каждая сторона своей строкой", text.count("\n- ") == 2, repr(text))
ok("сказано, что роли заданы человеком", "заданы человеком" in text)

config.save({"owner_side": "", "owner_position": ""})
voices.set_role(pid, "", "")
plain = minutes.participants_block(["Орлов", "Иван Петров"], {})
ok("без ролей шапка как была",
   plain == ["Участники: Орлов; Иван Петров"], str(plain))
ok("совсем без участников — «не определены»",
   minutes.participants_block([], {}) == ["Участники: не определены"])
voices.set_role(pid, "supplier", "коммерческий директор")

print("\n=== 5. Роли записи: чистка ===")
ok("роль без имени отбрасывается",
   store.clean_roles({"": {"side": "supplier"}}) == {})
ok("роль без стороны и должности отбрасывается",
   store.clean_roles({"Иван": {"side": "", "position": ""}}) == {})
ok("чужая сторона вычищается, должность остаётся",
   store.clean_roles({"Иван": {"side": "царь", "position": "юрист"}})
   == {"Иван": {"side": "", "position": "юрист"}},
   str(store.clean_roles({"Иван": {"side": "царь", "position": "юрист"}})))
ok("не словарь — пустые роли", store.clean_roles(["Иван"]) == {})

print("\n=== 6. Точки службы ===")
from fastapi.testclient import TestClient  # noqa: E402
from hagen.server import app  # noqa: E402

ORIGIN = {"Origin": "http://127.0.0.1:8787"}
rec_id = store.create(title="переговоры о поставке", source="live")["id"]
store.replace_segments(rec_id, [
    store.make_segment("mic", 0.0, 4.0, "Нужны сроки."),
    store.make_segment("far", 4.0, 8.0, "Отгрузим в пятницу.", speaker="Иван Петров"),
])

with TestClient(app, base_url="http://127.0.0.1:8787") as client:
    r = client.post("/api/voices/%s/role" % pid,
                    json={"side": "contractor", "position": "  прораб "}, headers=ORIGIN)
    ok("роль человека принята", r.status_code == 200, r.text[:200])
    got = (r.json() if r.status_code == 200 else {}).get("result") or {}
    ok("должность почищена", got.get("position") == "прораб", str(got))
    ok("сторона сохранена", got.get("side") == "contractor", str(got))

    r2 = client.patch("/api/recordings/%s" % rec_id,
                      json={"roles": {"Иван Петров": {"side": "partner", "position": "юрист"},
                                      "Пустой": {"side": "", "position": ""}}},
                      headers=ORIGIN)
    ok("роли записи приняты", r2.status_code == 200, r2.text[:200])
    roles = (r2.json() if r2.status_code == 200 else {}).get("roles") or {}
    ok("пустая роль не сохранилась", list(roles) == ["Иван Петров"], str(roles))

    head = minutes.build_transcript_text(rec_id).splitlines()
    line = [x for x in head if x.startswith("Участники:")][0]
    ok("в шапке записи видна роль этой записи, а не постоянная",
       "Иван Петров — партнёр, юрист" in line, line)

    r3 = client.get("/api/prompts", headers=ORIGIN)
    prompts = r3.json() if r3.status_code == 200 else {}
    ok("список сторон отдаётся интерфейсу",
       [s["key"] for s in prompts.get("sides") or []] == list(voices.SIDES),
       str(prompts.get("sides"))[:120])

    r4 = client.post("/api/prompts",
                     json={"owner_side": "buyer", "owner_position": "директор"},
                     headers=ORIGIN)
    ok("своя роль сохраняется", r4.status_code == 200, r4.text[:160])
    ok("своя роль дошла до настроек",
       config.get("owner_side") == "buyer" and config.get("owner_position") == "директор",
       str(config.get("owner_side")))

try:
    store.delete(rec_id)
except Exception:
    pass

print("\nВсего провалов: %d" % len(FAIL))
if FAIL:
    for f in FAIL:
        print("  - %s" % f)
sys.exit(1 if FAIL else 0)
