# -*- coding: utf-8 -*-
"""Проверка 64: правка стенограммы — правка текста реплики (16.09).

Четвёртый блок правки (решение 16.09): двойной щелчок по реплике — и
её текст можно поправить руками: опечатки, фамилии, обрывки слов.

Что проверяем:
  1. текст меняется, время реплики не трогается, правка помечена;
  2. слов столько же — время слов сохраняется (только текст слов новый);
  3. слов стало другое число — время слов стирается, дальше границы «на глаз»;
  4. пустая реплика запрещена, лишние пробелы убираются, тот же текст —
     не правка (снимок для отмены не тратится);
  5. «Отменить» возвращает прежний текст;
  6. точка службы: правка, 404, понятный отказ;
  7. на странице двойной щелчок открывает ввод, Enter сохраняет, Escape
     отменяет, и одиночный щелчок при этом не режет реплику.

База голосов и настройки — временные (isolate), записи проверка удаляет за собой.
Запуск из корня проекта:  .venv\\Scripts\\python.exe tests\\t64_transcript_text.py
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
    if str(old.get("title") or "").startswith("Проверка 64"):
        store.delete(old["id"])

TEXT = "Передаю слово Орлову"


def record(title):
    rid = store.create(title=title, mode="online", source="live", category="Встречи")["id"]
    MADE.append(rid)
    seg = store.make_segment("far", 10, 13, TEXT, speaker="Ольга", speaker_key="SPEAKER_01")
    seg["words"] = [{"text": "Передаю", "start": 10.0, "end": 11.0},
                    {"text": "слово", "start": 11.0, "end": 12.0},
                    {"text": "Орлову", "start": 12.0, "end": 13.0}]
    other = store.make_segment("far", 14, 16, "Слушаю", speaker="Пётр", speaker_key="SPEAKER_00")
    store.replace_segments(rid, [seg, other])
    store.update(rid, {"diarized": True})
    store.refresh_participants(rid)
    return rid, seg["id"]


def first(rid):
    return store.sorted_segments(rid)[0]


try:
    say("=== 1. Слов столько же — время слов остаётся ===")
    rid, sid = record("Проверка 64 правка")
    res = edits.edit_text(rid, sid, "Передаю слово Орлову П.")
    seg = first(rid)
    check("правка засчитана", res["changed"] is True, res)
    check("текст поменялся", seg["text"] == "Передаю слово Орлову П.", seg["text"])
    check("сказано, что время слов стёрто", res["kept_times"] is False, res)
    check("время слов стёрто, потому что слов стало больше", seg["words"] == [], seg["words"])
    check("время реплики не тронуто", (seg["start"], seg["end"]) == (10.0, 13.0),
          (seg["start"], seg["end"]))
    check("правка помечена", seg.get("edited") is True, seg.get("edited"))
    check("соседняя реплика цела", store.sorted_segments(rid)[1]["text"] == "Слушаю")

    rid2, sid2 = record("Проверка 64 слов столько же")
    res2 = edits.edit_text(rid2, sid2, "Передаю слово Орлову")   # тот же текст
    check("тот же текст — не правка", res2["changed"] is False, res2)
    check("снимок на пустую правку не потрачен", not edits.undo_info(rid2), edits.undo_info(rid2))
    res2 = edits.edit_text(rid2, sid2, "Передаю слово  Петрову  ")
    seg2 = first(rid2)
    check("лишние пробелы убраны", seg2["text"] == "Передаю слово Петрову", seg2["text"])
    check("время слов сохранено: слов столько же", res2["kept_times"] is True, res2)
    check("время слов на месте, текст слова новый",
          [(w["text"], w["start"], w["end"]) for w in seg2["words"]]
          == [("Передаю", 10.0, 11.0), ("слово", 11.0, 12.0), ("Петрову", 12.0, 13.0)],
          seg2["words"])

    say("")
    say("=== 2. Отказы ===")
    rid3, sid3 = record("Проверка 64 отказы")
    for name, text in (("пустая реплика запрещена", "   "), ("пустая строка запрещена", "")):
        try:
            edits.edit_text(rid3, sid3, text)
            check(name, False, "правка прошла, хотя не должна")
        except edits.EditError as err:
            check(name, "пустой" in str(err).lower(), str(err))
    try:
        edits.edit_text(rid3, "нет-такой", "Что-то")
        check("чужая реплика не правится", False, "правка прошла")
    except edits.EditError as err:
        check("чужая реплика не правится", "не найдена" in str(err), str(err))
    check("после отказов текст прежний", first(rid3)["text"] == TEXT, first(rid3)["text"])
    check("снимок после отказа не оставлен", not edits.undo_info(rid3), edits.undo_info(rid3))

    say("")
    say("=== 3. Отмена ===")
    rid4, sid4 = record("Проверка 64 отмена")
    before = store.sorted_segments(rid4)
    edits.edit_text(rid4, sid4, "Совсем другой текст реплики")
    check("отмена названа по-русски",
          edits.undo_info(rid4).get("what") == "правка текста", edits.undo_info(rid4))
    edits.undo(rid4)
    after = store.sorted_segments(rid4)
    check("текст и время слов вернулись",
          [(s["id"], s["text"], s.get("words")) for s in after]
          == [(s["id"], s["text"], s.get("words")) for s in before],
          [(s["text"], s.get("words")) for s in after])
    check("пометка правки снята вместе с текстом", not after[0].get("edited"), after[0].get("edited"))

    say("")
    say("=== 4. Точка службы ===")
    from fastapi.testclient import TestClient  # noqa: E402

    from hagen import server  # noqa: E402

    ORIGIN = {"Origin": "http://127.0.0.1:8787"}
    rid5, sid5 = record("Проверка 64 служба")
    with TestClient(server.app, base_url="http://127.0.0.1:8787") as cli:
        r = cli.post(f"/api/recordings/{rid5}/transcript/text",
                     json={"segment_id": sid5, "text": "Передаю слово Петрову"}, headers=ORIGIN)
        body = r.json() if r.status_code == 200 else {}
        check("служба правит текст", r.status_code == 200, r.text[:200])
        check("в ответе свежая стенограмма и судьба времени слов",
              body.get("changed") is True and body.get("kept_times") is True
              and body["segments"][0]["text"] == "Передаю слово Петрову", r.text[:200])
        r = cli.post("/api/recordings/нет-такой/transcript/text",
                     json={"segment_id": sid5, "text": "Что-то"}, headers=ORIGIN)
        check("чужая запись — 404", r.status_code == 404, r.status_code)
        r = cli.post(f"/api/recordings/{rid5}/transcript/text",
                     json={"segment_id": sid5, "text": "  "}, headers=ORIGIN)
        check("пустая реплика — понятный отказ службы",
              r.status_code == 400 and "пустой" in r.text, r.text[:200])
        r = cli.post(f"/api/recordings/{rid5}/transcript/undo", headers=ORIGIN)
        check("правку текста можно отменить через службу",
              r.status_code == 200 and r.json()["segments"][0]["text"] == TEXT, r.text[:200])
finally:
    for rid in MADE:
        try:
            store.delete(rid)
        except Exception:
            pass

say("")
say("=== 5. Страница ===")
html = io.open(PROJECT / "hagen" / "static" / "index.html", encoding="utf-8").read()
js = io.open(PROJECT / "hagen" / "static" / "app.js", encoding="utf-8").read()
css = io.open(PROJECT / "hagen" / "static" / "app.css", encoding="utf-8").read()
check("в подсказке сказано про двойной щелчок и клавиши",
      "Двойной щелчок" in html and "Escape" in html)
check("двойной щелчок открывает правку текста",
      "line.ondblclick" in js and "editPhrase(line)" in js)
check("Enter сохраняет, Escape отменяет",
      "e.key === 'Enter' && !e.shiftKey" in js and "e.key === 'Escape'" in js)
check("правка уходит службе", "/transcript/text" in js and "function savePhrase" in js)
check("двойной щелчок не режет реплику: разрез требует Alt",
      "S.splitTimer" not in js and "if (!e.altKey" in js)
check("про стёртое время слов сказано словами", "время слов стёрто" in js)
check("правленая реплика помечена", "s.edited" in js and ".txt.edited" in css)
check("строка ввода оформлена", ".txt-edit" in css and "ta.className = 'txt-edit'" in js)

say("")
say("ИТОГО провалов: %d" % len(FAIL))
for f in FAIL:
    say("   - " + f)
io.open(PROJECT / "tests" / "t64_result.txt", "w", encoding="utf-8").write("\n".join(LINES))
sys.exit(1 if FAIL else 0)
