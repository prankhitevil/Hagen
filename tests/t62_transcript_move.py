# -*- coding: utf-8 -*-
"""Проверка 62: правка стенограммы — передать слова соседу (16.09).

Второй блок правки (решение 16.09): выделить слова мышью и нажать
Alt+↑ или Alt+↓ — кусок уходит реплике выше или ниже. Это главный случай ошибки
разметки: хвост фразы приписан соседу или наоборот.

Что проверяем:
  1. хвост уходит вниз: тексты, время и слова склеиваются, остаток — та же
     реплика (её id не меняется);
  2. начало уходит вверх — так же, но в другую сторону;
  3. отказы объясняются: кусок из середины, соседа нет;
  3а. выделена вся реплика — она уходит соседу целиком и исчезает (17.09:
     эхо колонок приписало владельцу чужую фразу от первого слова до
     последнего, и отдать её было нечем);
  4. когда время слов ненастоящее, склеенные слова не выдумываются;
  5. «Отменить» возвращает стенограмму;
  6. точка службы: перенос, 404 на чужую запись, понятный отказ;
  7. на странице Alt+↑ / Alt+↓ подключены и объяснены.

База голосов и настройки — временные (isolate), записи проверка удаляет за собой.
Запуск из корня проекта:  .venv\\Scripts\\python.exe tests\\t62_transcript_move.py
"""
import io
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
sys.path.insert(0, str(PROJECT / "tests"))
import isolate  # noqa: E402

isolate.voices()
isolate.settings()

LINES = []
FAIL = []
MADE = []


def say(msg):
    LINES.append(str(msg))
    try:
        print(str(msg), flush=True)
    except Exception:
        # Консоль не знает этих букв (бывает cp1251) — печатаем без них.
        try:
            print(str(msg).encode("ascii", "replace").decode("ascii"), flush=True)
        except Exception:
            pass


def check(name, ok, detail=""):
    if not ok:
        FAIL.append(name)
    say(("   ok    " if ok else "   ПЛОХО ") + name + (": " + str(detail)[:300] if detail != "" else ""))


from hagen import edits, store  # noqa: E402

for old in store.list_all():
    if str(old.get("title") or "").startswith("Проверка 62"):
        store.delete(old["id"])

TOP = "Добрый день коллеги а теперь передаю слово"
BOTTOM = "Спасибо я готова"


def words(text, start, step=1.0):
    out, at = [], float(start)
    for part in text.split():
        out.append({"text": part, "start": round(at, 3), "end": round(at + step, 3)})
        at += step
    return out


def record(title, timed=True):
    """Две соседние реплики разных людей: верхняя длинная, нижняя короткая."""
    rid = store.create(title=title, mode="online", source="live", category="Встречи")["id"]
    MADE.append(rid)
    top = store.make_segment("far", 10, 17, TOP, speaker="Участник 1", speaker_key="SPEAKER_00")
    bot = store.make_segment("far", 17, 20, BOTTOM, speaker="Ольга", speaker_key="SPEAKER_01")
    if timed:
        top["words"] = words(TOP, 10)
        bot["words"] = words(BOTTOM, 17)
    store.replace_segments(rid, [top, bot])
    store.update(rid, {"diarized": True})
    store.refresh_participants(rid)
    return rid, top["id"], bot["id"]


def texts(rid):
    return [s["text"] for s in store.sorted_segments(rid)]


try:
    say("=== 1. Хвост уходит вниз (Alt+↓) ===")
    rid, top, bot = record("Проверка 62 вниз")
    res = edits.move_words(rid, top, 5, 6, "next")   # «передаю слово» — Ольге
    segs = store.sorted_segments(rid)
    check("реплик по-прежнему две", len(segs) == 2, texts(rid))
    check("хвост ушёл вниз",
          texts(rid) == ["Добрый день коллеги а теперь", "передаю слово Спасибо я готова"], texts(rid))
    check("сказано, кому передали", res["to"] == "Ольга", res["to"])
    check("остаток — та же реплика", segs[0]["id"] == top, (segs[0]["id"], top))
    check("нижняя реплика — та же", segs[1]["id"] == bot, (segs[1]["id"], bot))
    check("говорящие не перепутались",
          segs[0]["speaker"] == "Участник 1" and segs[1]["speaker"] == "Ольга",
          [s["speaker"] for s in segs])
    check("время подвинулось за словами",
          segs[0]["end"] == 15.0 and segs[1]["start"] == 15.0,
          (segs[0]["start"], segs[0]["end"], segs[1]["start"], segs[1]["end"]))
    check("слова склеились в правильном порядке",
          [w["text"] for w in segs[1]["words"]] == ["передаю", "слово", "Спасибо", "я", "готова"],
          segs[1]["words"])
    check("время слов не сбилось",
          [w["start"] for w in segs[1]["words"]] == [15.0, 16.0, 17.0, 18.0, 19.0],
          segs[1]["words"])

    say("")
    say("=== 2. Начало уходит вверх (Alt+↑) ===")
    rid2, top2, bot2 = record("Проверка 62 вверх")
    res2 = edits.move_words(rid2, bot2, 0, 0, "prev")    # «Спасибо» — наверх
    segs2 = store.sorted_segments(rid2)
    check("начало ушло вверх",
          texts(rid2) == ["Добрый день коллеги а теперь передаю слово Спасибо", "я готова"], texts(rid2))
    check("сказано, кому передали", res2["to"] == "Участник 1", res2["to"])
    check("остаток — та же реплика", segs2[1]["id"] == bot2, (segs2[1]["id"], bot2))
    check("время верхней доросло, нижняя начинается позже",
          segs2[0]["end"] == 18.0 and segs2[1]["start"] == 18.0,
          (segs2[0]["end"], segs2[1]["start"]))
    check("слова склеились в правильном порядке",
          [w["text"] for w in segs2[0]["words"]][-2:] == ["слово", "Спасибо"], segs2[0]["words"])

    say("")
    say("=== 3. Отказы объясняются ===")
    rid3, top3, bot3 = record("Проверка 62 отказы")
    cases = [
        ("кусок из середины вниз не отдать", (top3, 2, 3, "next"), "хвост"),
        ("кусок из середины вверх не отдать", (top3, 2, 3, "prev"), "начало"),
        ("выше первой реплики соседа нет", (top3, 0, 1, "prev"), "некому"),
        ("ниже последней реплики соседа нет", (bot3, 1, 2, "next"), "некому"),
        ("слова за краем реплики", (top3, 5, 99, "next"), "выделите"),
        ("сторона не понята", (top3, 5, 6, "вбок"), "непонятно"),
    ]
    for name, args, needle in cases:
        try:
            edits.move_words(rid3, *args)
            check(name, False, "перенос прошёл, хотя не должен")
        except edits.EditError as err:
            check(name, needle in str(err).lower(), str(err))
    try:
        edits.move_words(rid3, "нет-такой", 0, 1, "next")
        check("чужая реплика не переносится", False, "перенос прошёл")
    except edits.EditError as err:
        check("чужая реплика не переносится", "не найдена" in str(err), str(err))
    check("после отказов стенограмма не изменилась", texts(rid3) == [TOP, BOTTOM], texts(rid3))
    check("снимок после отказа не оставлен", not edits.undo_info(rid3), edits.undo_info(rid3))

    say("")
    say("=== 3а. Реплика целиком уходит соседу ===")
    rid3a, top3a, bot3a = record("Проверка 62 целиком")
    res3a = edits.move_words(rid3a, bot3a, 0, 2, "prev")   # вся нижняя — наверх
    segs3a = store.sorted_segments(rid3a)
    check("осталась одна реплика", len(segs3a) == 1, texts(rid3a))
    check("текст склеился целиком", texts(rid3a) == [TOP + " " + BOTTOM], texts(rid3a))
    check("говорящий — принимающей стороны", segs3a[0]["speaker"] == "Участник 1",
          segs3a[0]["speaker"])
    check("id принимающей реплики сохранён", segs3a[0]["id"] == top3a, (segs3a[0]["id"], top3a))
    check("время выросло до конца отданной", segs3a[0]["end"] == 20.0, segs3a[0]["end"])
    check("слова все на месте",
          [w["text"] for w in segs3a[0]["words"]] == (TOP + " " + BOTTOM).split(),
          segs3a[0]["words"])
    check("сказано, кому передали", res3a["to"] == "Участник 1", res3a["to"])
    edits.undo(rid3a)
    check("отмена вернула обе реплики", texts(rid3a) == [TOP, BOTTOM], texts(rid3a))

    rid3b, top3b, bot3b = record("Проверка 62 целиком вниз")
    edits.move_words(rid3b, top3b, 0, 6, "next")          # вся верхняя — вниз
    segs3b = store.sorted_segments(rid3b)
    check("вниз тоже уходит целиком",
          len(segs3b) == 1 and texts(rid3b) == [TOP + " " + BOTTOM], texts(rid3b))
    check("говорящий — нижней реплики", segs3b[0]["speaker"] == "Ольга", segs3b[0]["speaker"])
    check("время начала отъехало назад", segs3b[0]["start"] == 10.0, segs3b[0]["start"])

    say("")
    say("=== 4. Времени слов нет ===")
    rid4, top4, bot4 = record("Проверка 62 без времени", timed=False)
    res4 = edits.move_words(rid4, top4, 5, 6, "next")
    segs4 = store.sorted_segments(rid4)
    check("сказано, что время неточное", res4["exact"] is False, res4["exact"])
    check("текст всё равно перенесён",
          texts(rid4) == ["Добрый день коллеги а теперь", "передаю слово Спасибо я готова"], texts(rid4))
    check("выдуманные слова не записаны", not segs4[0]["words"] and not segs4[1]["words"],
          (segs4[0]["words"], segs4[1]["words"]))
    check("границы реплик не разошлись", segs4[0]["end"] == segs4[1]["start"],
          (segs4[0]["end"], segs4[1]["start"]))

    say("")
    say("=== 5. Отмена ===")
    rid5, top5, bot5 = record("Проверка 62 отмена")
    before = store.sorted_segments(rid5)
    edits.move_words(rid5, top5, 6, 6, "next")
    check("отмена названа по-русски",
          edits.undo_info(rid5).get("what") == "перенос слов соседу", edits.undo_info(rid5))
    edits.undo(rid5)
    after = store.sorted_segments(rid5)
    check("стенограмма вернулась целиком",
          [(s["id"], s["text"], s["start"], s["end"]) for s in after]
          == [(s["id"], s["text"], s["start"], s["end"]) for s in before], texts(rid5))

    say("")
    say("=== 6. Точка службы ===")
    from fastapi.testclient import TestClient  # noqa: E402

    from hagen import server  # noqa: E402

    ORIGIN = {"Origin": "http://127.0.0.1:8787"}
    rid6, top6, bot6 = record("Проверка 62 служба")
    with TestClient(server.app, base_url="http://127.0.0.1:8787") as cli:
        r = cli.post(f"/api/recordings/{rid6}/transcript/move",
                     json={"segment_id": top6, "first": 5, "last": 6, "where": "next"},
                     headers=ORIGIN)
        body = r.json() if r.status_code == 200 else {}
        check("служба переносит слова", r.status_code == 200, r.text[:200])
        check("в ответе свежая стенограмма",
              [s["text"] for s in body.get("segments", [])]
              == ["Добрый день коллеги а теперь", "передаю слово Спасибо я готова"], r.text[:200])
        check("сказано, кому передали и точное ли время",
              body.get("to") == "Ольга" and body.get("exact") is True, r.text[:200])
        r = cli.post("/api/recordings/нет-такой/transcript/move",
                     json={"segment_id": top6, "first": 0, "last": 1, "where": "next"}, headers=ORIGIN)
        check("чужая запись — 404", r.status_code == 404, r.status_code)
        r = cli.post(f"/api/recordings/{rid6}/transcript/move",
                     json={"segment_id": top6, "first": 1, "last": 2, "where": "next"}, headers=ORIGIN)
        check("непонятный кусок — понятный отказ службы",
              r.status_code == 400 and "хвост" in r.text, r.text[:200])
        r = cli.post(f"/api/recordings/{rid6}/transcript/undo", headers=ORIGIN)
        check("правку можно отменить через службу",
              r.status_code == 200 and [s["text"] for s in r.json().get("segments", [])] == [TOP, BOTTOM],
              r.text[:200])
finally:
    for rid in MADE:
        try:
            store.delete(rid)
        except Exception:
            pass

say("")
say("=== 7. Страница ===")
html = io.open(PROJECT / "hagen" / "static" / "index.html", encoding="utf-8").read()
js = io.open(PROJECT / "hagen" / "static" / "app.js", encoding="utf-8").read()
check("в подсказке названы Alt+↑ и Alt+↓", "Alt+↑" in html and "Alt+↓" in html)
check("объяснено, что вверх уходит начало, а вниз хвост",
      "начало — вверх" in html and "хвост — вниз" in html)
check("выделенные слова считываются", "function selectedWords" in js
      and "containsNode" in js)
check("выделение берётся только внутри одной реплики", "line !== to" in js)
check("Alt со стрелками подключён",
      "e.key === 'ArrowUp' ? 'prev' : 'next'" in js and "if (!S.editMode || !e.altKey" in js)
check("перенос шлёт запрос службе", "/transcript/move" in js)
check("без выделения объясняем, что делать", "Сначала выделите слова" in js)
check("простой щелчок при выделении не режет реплику: разрез требует Alt",
      "if (!e.altKey || !Number(el.dataset.i)) return;" in js)

say("")
say("ИТОГО провалов: %d" % len(FAIL))
for f in FAIL:
    say("   - " + f)
io.open(PROJECT / "tests" / "t62_result.txt", "w", encoding="utf-8").write("\n".join(LINES))
sys.exit(1 if FAIL else 0)
