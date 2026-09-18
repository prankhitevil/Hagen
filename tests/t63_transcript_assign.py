# -*- coding: utf-8 -*-
"""Проверка 63: правка стенограммы — отдать слова любому говорящему (16.09).

Третий блок правки (решение 16.09): выделить слова, нажать Enter и
выбрать из списка, кому они принадлежат. Нужен, когда виноват не сосед сверху
или снизу, а человек, говоривший раньше.

Что проверяем:
  1. хвост уходит выбранному человеку: имя, время, слова, остаток — та же реплика;
  2. кусок из середины: реплика распадается на три, середина — у выбранного;
  3. если рядом уже есть реплика этого человека, кусок приклеивается к ней, а не
     висит обрывком; имя и id принимающей реплики не меняются;
  4. реплику целиком можно просто переподписать;
  5. отказы: свой же говорящий, неизвестный говорящий, слова за краем;
  6. подпись человека считается ручной (speaker_locked), чтобы разметка её не
     перебила;
  7. «Отменить» возвращает; точки службы: список говорящих, передача, отказы;
  8. на странице Enter открывает список и отдаёт выделенное.

База голосов и настройки — временные (isolate), записи проверка удаляет за собой.
Запуск из корня проекта:  .venv\\Scripts\\python.exe tests\\t63_transcript_assign.py
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
    if str(old.get("title") or "").startswith("Проверка 63"):
        store.delete(old["id"])

FIRST = "Ольга расскажи про сроки"
MID = "Сроки такие конец октября"
LAST = "Хорошо принято"


def words(text, start, step=1.0):
    out, at = [], float(start)
    for part in text.split():
        out.append({"text": part, "start": round(at, 3), "end": round(at + step, 3)})
        at += step
    return out


def record(title):
    """Три реплики трёх людей подряд — есть из кого выбирать."""
    rid = store.create(title=title, mode="online", source="live", category="Встречи")["id"]
    MADE.append(rid)
    segs = []
    for text, start, who, key in ((FIRST, 10, "Пётр", "SPEAKER_00"),
                                  (MID, 14, "Ольга", "SPEAKER_01"),
                                  (LAST, 19, "Пётр", "SPEAKER_00")):
        seg = store.make_segment("far", start, start + len(text.split()), text,
                                 speaker=who, speaker_key=key)
        seg["words"] = words(text, start)
        segs.append(seg)
    store.replace_segments(rid, segs)
    store.update(rid, {"diarized": True})
    store.refresh_participants(rid)
    return rid, [s["id"] for s in segs]


def rows(rid):
    return [(s["speaker"], s["text"]) for s in store.sorted_segments(rid)]


try:
    say("=== 1. Список говорящих записи ===")
    rid, ids = record("Проверка 63 список")
    who = edits.speakers_of(rid)
    check("в списке оба человека, без повторов",
          [(w["key"], w["name"]) for w in who]
          == [("SPEAKER_00", "Пётр"), ("SPEAKER_01", "Ольга")], who)

    say("")
    say("=== 2. Хвост уходит выбранному ===")
    rid2, ids2 = record("Проверка 63 хвост")
    res = edits.assign_speaker(rid2, ids2[1], 2, 3, "SPEAKER_00")  # «конец октября» — Петру
    check("сказано, кому отдали", res["to"] == "Пётр", res["to"])
    # Ниже уже идёт реплика Петра — кусок приклеился к ней, а не повис обрывком.
    check("кусок приклеился к реплике того же человека",
          rows(rid2) == [("Пётр", FIRST), ("Ольга", "Сроки такие"),
                         ("Пётр", "конец октября Хорошо принято")], rows(rid2))
    segs2 = store.sorted_segments(rid2)
    check("принимающая реплика осталась собой", segs2[2]["id"] == ids2[2], (segs2[2]["id"], ids2[2]))
    check("остаток — та же реплика", segs2[1]["id"] == ids2[1], (segs2[1]["id"], ids2[1]))
    check("время склеилось", segs2[2]["start"] == 16.0 and segs2[1]["end"] == 16.0
          and segs2[2]["end"] == 21.0, (segs2[1]["end"], segs2[2]["start"], segs2[2]["end"]))
    check("слова в правильном порядке",
          [w["text"] for w in segs2[2]["words"]] == ["конец", "октября", "Хорошо", "принято"],
          segs2[2]["words"])

    say("")
    say("=== 3. Кусок из середины ===")
    rid3, ids3 = record("Проверка 63 середина")
    edits.assign_speaker(rid3, ids3[0], 1, 2, "SPEAKER_01")   # «расскажи про» — Ольге
    check("реплика распалась на три части, середина у выбранного",
          rows(rid3)[:3] == [("Пётр", "Ольга"), ("Ольга", "расскажи про"), ("Пётр", "сроки")],
          rows(rid3))
    check("всего реплик стало пять", len(store.sorted_segments(rid3)) == 5, rows(rid3))
    check("подпись выбранного считается ручной",
          store.sorted_segments(rid3)[1]["speaker_locked"] is True,
          store.sorted_segments(rid3)[1])

    say("")
    say("=== 4. Реплика целиком ===")
    rid4, ids4 = record("Проверка 63 целиком")
    edits.assign_speaker(rid4, ids4[1], 0, 3, "SPEAKER_00")
    check("реплику целиком переподписали и склеили с соседями с обеих сторон",
          rows(rid4) == [("Пётр", FIRST + " " + MID + " " + LAST)], rows(rid4))
    check("склейка осталась первой репликой",
          store.sorted_segments(rid4)[0]["id"] == ids4[0], store.sorted_segments(rid4)[0]["id"])
    check("время склейки — от начала до конца",
          (store.sorted_segments(rid4)[0]["start"], store.sorted_segments(rid4)[0]["end"]) == (10.0, 21.0),
          store.sorted_segments(rid4)[0])

    say("")
    say("=== 5. Отказы объясняются ===")
    rid5, ids5 = record("Проверка 63 отказы")
    cases = [
        ("свой же говорящий", (ids5[0], 0, 1, "SPEAKER_00"), "и так за этим"),
        ("неизвестный говорящий", (ids5[0], 0, 1, "SPEAKER_77"), "нет"),
        ("слова за краем реплики", (ids5[0], 0, 99, "SPEAKER_01"), "выделите"),
    ]
    for name, args, needle in cases:
        try:
            edits.assign_speaker(rid5, *args)
            check(name, False, "передача прошла, хотя не должна")
        except edits.EditError as err:
            check(name, needle in str(err).lower(), str(err))
    check("после отказов стенограмма не изменилась",
          rows(rid5) == [("Пётр", FIRST), ("Ольга", MID), ("Пётр", LAST)], rows(rid5))
    check("снимок после отказа не оставлен", not edits.undo_info(rid5), edits.undo_info(rid5))

    say("")
    say("=== 6. Отмена ===")
    rid6, ids6 = record("Проверка 63 отмена")
    before = store.sorted_segments(rid6)
    edits.assign_speaker(rid6, ids6[0], 0, 0, "SPEAKER_01")
    check("отмена названа по-русски",
          edits.undo_info(rid6).get("what") == "передача слов другому", edits.undo_info(rid6))
    edits.undo(rid6)
    check("стенограмма вернулась целиком",
          [(s["id"], s["text"], s["speaker"]) for s in store.sorted_segments(rid6)]
          == [(s["id"], s["text"], s["speaker"]) for s in before], rows(rid6))

    say("")
    say("=== 7. Точки службы ===")
    from fastapi.testclient import TestClient  # noqa: E402

    from hagen import server  # noqa: E402

    ORIGIN = {"Origin": "http://127.0.0.1:8787"}
    rid7, ids7 = record("Проверка 63 служба")
    with TestClient(server.app, base_url="http://127.0.0.1:8787") as cli:
        r = cli.get(f"/api/recordings/{rid7}/transcript/speakers")
        check("служба отдаёт говорящих записи",
              r.status_code == 200 and [s["name"] for s in r.json().get("speakers", [])]
              == ["Пётр", "Ольга"], r.text[:200])
        r = cli.post(f"/api/recordings/{rid7}/transcript/assign",
                     json={"segment_id": ids7[1], "first": 2, "last": 3,
                           "speaker_key": "SPEAKER_00"}, headers=ORIGIN)
        check("служба отдаёт слова выбранному", r.status_code == 200, r.text[:200])
        check("в ответе имя и свежая стенограмма",
              r.status_code == 200 and r.json().get("to") == "Пётр"
              and len(r.json().get("segments", [])) == 3, r.text[:200])
        r = cli.post("/api/recordings/нет-такой/transcript/assign",
                     json={"segment_id": ids7[0], "first": 0, "last": 1,
                           "speaker_key": "SPEAKER_01"}, headers=ORIGIN)
        check("чужая запись — 404", r.status_code == 404, r.status_code)
        r = cli.get("/api/recordings/нет-такой/transcript/speakers")
        check("список говорящих чужой записи — 404", r.status_code == 404, r.status_code)
        r = cli.post(f"/api/recordings/{rid7}/transcript/assign",
                     json={"segment_id": ids7[0], "first": 0, "last": 1,
                           "speaker_key": "SPEAKER_00"}, headers=ORIGIN)
        check("свой же говорящий — понятный отказ службы",
              r.status_code == 400 and "и так за этим" in r.text, r.text[:200])
finally:
    for rid in MADE:
        try:
            store.delete(rid)
        except Exception:
            pass

say("")
say("=== 8. Страница ===")
html = io.open(PROJECT / "hagen" / "static" / "index.html", encoding="utf-8").read()
js = io.open(PROJECT / "hagen" / "static" / "app.js", encoding="utf-8").read()
css = io.open(PROJECT / "hagen" / "static" / "app.css", encoding="utf-8").read()
check("в подсказке сказано про Enter", "<b>Enter</b>" in html and "любому говорящему" in html)
check("место для списка говорящих есть", 'id="give-menu"' in html and ".give-menu" in css)
check("Enter открывает список", "openGiveMenu()" in js and "e.key !== 'Enter'" in js)
check("список берётся у службы", "/transcript/speakers" in js)
check("себя в списке не показываем", "s.key !== mine" in js)
check("выбор отдаёт слова", "/transcript/assign" in js and "function giveWords" in js)
check("Enter в полях ввода не мешает", "['INPUT', 'TEXTAREA', 'SELECT']" in js)
check("список закрывается щелчком мимо и Escape",
      "hideGiveMenu()" in js and "e.key === 'Escape'" in js)

say("")
say("ИТОГО провалов: %d" % len(FAIL))
for f in FAIL:
    say("   - " + f)
io.open(PROJECT / "tests" / "t63_result.txt", "w", encoding="utf-8").write("\n".join(LINES))
sys.exit(1 if FAIL else 0)
