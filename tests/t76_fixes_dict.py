# -*- coding: utf-8 -*-
"""Проверка 76: словарь из ручных правок.

Поправив «гранулятор» на «грануллятор» третий раз, человек вправе ожидать,
что дальше программа напишет так сама. Пары «было → стало» копятся в журнале
рядом с записями, а повторяющиеся предлагается сделать постоянной заменой.

Что проверяем:
  1. из правки достаются именно изменившиеся куски, а не вся фраза;
  2. переписанная целиком фраза в словарь не идёт;
  3. журнал считает повторы и не растёт бесконечно;
  4. предложение появляется после стольких повторов, сколько в настройках;
  5. «не предлагать» убирает предложение, но правку в журнале оставляет;
  6. постоянная замена применяется к тексту и к словам со временем;
  7. точки службы: список, добавить, забыть, не предлагать;
  8. правка текста реплики попадает в журнал сама;
  9. замены применяются к новому тексту (эфир и «Перечитать точнее»);
 10. журнал лежит рядом с записями и в сборку не попадает.

Настройки и база голосов — временные (isolate); журнал правок тоже временный.
Запуск из корня проекта:  .venv\\Scripts\\python.exe tests\\t76_fixes_dict.py
"""
import io
import shutil
import sys
import tempfile
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
sys.path.insert(0, str(PROJECT / "tests"))
import isolate  # noqa: E402
from harness import LINES, FAIL, say, check, finish  # noqa: E402

isolate.voices()
isolate.settings()

MADE = []


from hagen import config, edits, fixes, store  # noqa: E402

# Журнал правок — во временную папку: настоящий data/fixes.json не трогаем.
TMP = Path(tempfile.mkdtemp(prefix="fixes_test_"))
fixes.path = lambda: TMP / "fixes.json"

for old in store.list_all():
    if str(old.get("title") or "").startswith("Проверка 76"):
        store.delete(old["id"])

try:
    config.save({"transcript_replacements": [], "fix_suggest_after": 3})

    say("=== 1. Что достаётся из правки ===")
    got = fixes.changed_pairs("на заводе стоит гранулятор новый",
                              "на заводе стоит грануллятор новый")
    check("одно слово", got == [("гранулятор", "грануллятор")], got)
    got2 = fixes.changed_pairs("позвони наташ по отгрузкам",
                               "позвони Наташе по отгрузкам")
    check("регистр и окончание тоже правка", got2 == [("наташ", "Наташе")], got2)
    got3 = fixes.changed_pairs("сегодня отгрузили сто пятьдесят",
                               "сегодня отгрузили 150")
    check("числа словами", got3 == [("сто пятьдесят", "150")], got3)
    check("добавленное слово в словарь не идёт",
          fixes.changed_pairs("поставка из Европы", "поставка из Европы точно") == [])
    check("удалённое слово тоже", fixes.changed_pairs("ну вот смотри", "вот смотри") == [])

    say("")
    say("=== 2. Переписанная фраза не в счёт ===")
    long_pair = fixes.changed_pairs(
        "это надо будет отдельно обсудить с коллегами",
        "давайте вынесем этот вопрос на следующую встречу")
    check("длинный кусок отброшен", long_pair == [], long_pair)

    say("")
    say("=== 3. Журнал и повторы ===")
    check("первая правка записана",
          fixes.note_edit("стоит гранулятор", "стоит грануллятор") == 1)
    fixes.note_edit("новый гранулятор куплен", "новый грануллятор куплен")
    data = fixes._read()["pairs"]
    row = next(r for r in data if r["from"] == "гранулятор")
    check("повтор посчитан", row["count"] == 2, row)
    check("в журнале одна строка на пару", len(data) == 1, data)

    say("")
    say("=== 4. Когда предлагать ===")
    check("двух раз мало", fixes.suggestions() == [], fixes.suggestions())
    fixes.note_edit("гранулятор стоит", "грануллятор стоит")
    sug = fixes.suggestions()
    check("после третьего предлагаем", len(sug) == 1 and sug[0]["count"] == 3, sug)
    config.save({"fix_suggest_after": 5})
    check("порог из настроек слушается", fixes.suggestions() == [], fixes.suggestions())
    config.save({"fix_suggest_after": 3})

    say("")
    say("=== 5. «Не предлагать» ===")
    check("пара скрыта", fixes.dismiss("гранулятор", "грануллятор") is True)
    check("предложения больше нет", fixes.suggestions() == [], fixes.suggestions())
    check("но правка в журнале осталась",
          any(r["from"] == "гранулятор" for r in fixes._read()["pairs"]))

    say("")
    say("=== 6. Постоянная замена ===")
    fixes.add_rule("гранулятор", "грануллятор")
    # Словарь один на программу, и у замены помечено, где она действует
    # (решение 18.09): по умолчанию — везде.
    check("замена сохранена",
          fixes.rules() == [{"from": "гранулятор", "to": "грануллятор", "where": "both"}],
          fixes.rules())
    fixes.set_scope("гранулятор", "dictation")
    check("область действия меняется", fixes.rules("transcript") == []
          and len(fixes.rules("dictation")) == 1, fixes.rules("all"))
    fixes.set_scope("гранулятор", "both")
    check("применяется к тексту",
          fixes.apply("сегодня гранулятор стоит") == "сегодня грануллятор стоит",
          fixes.apply("сегодня гранулятор стоит"))
    check("целым словом, внутри других слов не лезет",
          fixes.apply("грануляторный цех") == "грануляторный цех",
          fixes.apply("грануляторный цех"))
    check("регистр начала предложения сохраняется",
          fixes.apply("Гранулятор стоит") == "Грануллятор стоит",
          fixes.apply("Гранулятор стоит"))
    words = [{"text": "Сегодня", "start": 1.0, "end": 2.0},
             {"text": "гранулятор,", "start": 2.0, "end": 3.0},
             {"text": "стоит", "start": 3.0, "end": 4.0}]
    out = fixes.apply_words(words)
    check("слова со временем тоже поправлены",
          [w["text"] for w in out] == ["Сегодня", "грануллятор,", "стоит"], out)
    check("время слов не съехало",
          [w["start"] for w in out] == [1.0, 2.0, 3.0], out)

    fixes.add_rule("сто пятьдесят", "150")
    many = fixes.apply_words([{"text": "приняли", "start": 0.0, "end": 1.0},
                              {"text": "сто", "start": 1.0, "end": 2.0},
                              {"text": "пятьдесят", "start": 2.0, "end": 4.0}])
    check("замена из двух слов в одно",
          [w["text"] for w in many] == ["приняли", "150"], many)
    check("время склеенного слова накрывает оба",
          many[1]["start"] == 1.0 and many[1]["end"] == 4.0, many)
    check("забыть замену", len(fixes.forget_rule("сто пятьдесят")) == 1, fixes.rules())

    say("")
    say("=== 7. Точки службы ===")
    from fastapi.testclient import TestClient  # noqa: E402

    from hagen import server  # noqa: E402

    ORIGIN = {"Origin": "http://127.0.0.1:8787"}
    with TestClient(server.app, base_url="http://127.0.0.1:8787") as cli:
        r = cli.get("/api/fixes", headers=ORIGIN)
        check("список отдаётся", r.status_code == 200 and r.json()["rules"], r.text)
        r2 = cli.post("/api/fixes", json={"action": "add", "from": "джира", "to": "Jira"},
                      headers=ORIGIN)
        check("замена добавлена",
              any(x["to"] == "Jira" for x in r2.json()["rules"]), r2.text)
        r3 = cli.post("/api/fixes", json={"action": "forget", "from": "джира"}, headers=ORIGIN)
        check("замена забыта", not any(x["to"] == "Jira" for x in r3.json()["rules"]), r3.text)
        r4 = cli.post("/api/fixes", json={"action": "add", "from": "", "to": ""}, headers=ORIGIN)
        check("пустая замена не принимается", r4.status_code == 400, r4.status_code)

    say("")
    say("=== 8. Правка реплики попадает в журнал сама ===")
    rid = store.create(title="Проверка 76 правка", mode="online", source="live",
                       category="Встречи")["id"]
    MADE.append(rid)
    seg = store.make_segment("far", 10, 14, "у нас стоит экструдер новый",
                             speaker="Саша", speaker_key="SPEAKER_00")
    store.replace_segments(rid, [seg])
    before = len(fixes._read()["pairs"])
    edits.edit_text(rid, seg["id"], "у нас стоит экструдэр новый")
    after = fixes._read()["pairs"]
    check("пара добавилась", len(after) == before + 1, [r["from"] for r in after])
    check("пара та самая",
          any(r["from"] == "экструдер" and r["to"] == "экструдэр" for r in after), after)
    check("разрез реплики в словарь не идёт",
          fixes.changed_pairs("у нас стоит", "у нас стоит") == [])

    say("")
    say("=== 9. Где замены применяются ===")
    live = io.open(PROJECT / "hagen" / "live.py", encoding="utf-8").read()
    # «Перечитать точнее» — задача ядра (reread.py), маршрут её только ставит.
    srv = io.open(PROJECT / "hagen" / "reread.py", encoding="utf-8").read()
    check("в живой записи", "fixes.apply(text)" in live)
    check("в «Перечитать точнее»", "fixes.apply(p[\"text\"])" in srv)
    check("вместе со словами", "fixes.apply_words(p.get(\"words\")" in srv)
    check("готовые стенограммы не переписываются",
          "готовые стенограммы не переписываются" in io.open(
              PROJECT / "hagen" / "fixes.py", encoding="utf-8").read().lower()
          or "Уже готовые стенограммы не переписываются" in io.open(
              PROJECT / "hagen" / "fixes.py", encoding="utf-8").read())

    say("")
    say("=== 10. Личное не уезжает в сборку ===")
    check("журнал лежит рядом с записями",
          "DATA_DIR" in io.open(PROJECT / "hagen" / "fixes.py", encoding="utf-8").read())
    portable = io.open(PROJECT / "tools" / "make_portable.py", encoding="utf-8").read()
    check("data не копируется в сборку", "data\\\\" in portable or "data\\" in portable)
    html = io.open(PROJECT / "hagen" / "static" / "index.html", encoding="utf-8").read()
    js = io.open(PROJECT / "hagen" / "static" / "app.js", encoding="utf-8").read()
    check("на странице есть список замен", 'id="fix-rules"' in html)
    check("и место для предложений", 'id="fix-suggest"' in html)
    check("порог правится в настройках", 'id="set-fix-after"' in html)
    check("предложение можно принять и отклонить",
          "fix-yes" in js and "fix-no" in js)
    check("сказано, что журнал не уезжает", "в сборку для других людей не попадает" in html)

except Exception as err:                       # noqa: BLE001
    FAIL.append("проверка оборвалась")
    say("ОБОРВАЛОСЬ: %s: %s" % (type(err).__name__, err))
finally:
    for rid in MADE:
        try:
            store.delete(rid)
        except Exception:
            pass
    shutil.rmtree(TMP, ignore_errors=True)

sys.exit(finish("t76"))
