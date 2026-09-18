# -*- coding: utf-8 -*-
"""Проверка 61: правка стенограммы руками — разрез реплики по слову (16.09).

Разметка голосов ошибается предсказуемо: хвост фразы уезжает к соседу, а тот,
кто сказал два слова, приклеивается к похожему голосу. Настройками это чинится
не всегда, поэтому решено (16.09) дать правку руками. Первый блок —
разрез: в режиме правки щелчок по слову разрезает реплику перед этим словом,
дальше половину можно передать другому.

Что проверяем:
  1. разрез по слову: две реплики, тексты и время на месте, первая половина
     осталась той же репликой (её id не изменился), порядок не сбился;
  2. когда времени слов нет (живой текст), границу считаем по длине слов и
     честно говорим об этом (exact = false), не вылезая за края реплики;
  3. отказы объясняются по-русски: первое слово, слово за краем, чужая реплика;
  4. «Отменить» возвращает стенограмму целиком, и только на один шаг;
  5. точки службы: разрез, «есть что отменять», отмена, 404 на чужую запись;
  6. на странице есть кнопка «Править», подсказка и разрез по щелчку.

База голосов и настройки — временные (isolate), записи проверка создаёт и
удаляет за собой.
Запуск из корня проекта:  .venv\\Scripts\\python.exe tests\\t61_transcript_edit.py
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
    if str(old.get("title") or "").startswith("Проверка 61"):
        store.delete(old["id"])


def words(text, start, step):
    """Слова со временем: по step секунд на слово, как у точной модели."""
    out = []
    at = float(start)
    for part in text.split():
        out.append({"text": part, "start": round(at, 3), "end": round(at + step, 3)})
        at += step
    return out


def record(title, timed=True):
    rid = store.create(title=title, mode="online", source="live", category="Встречи")["id"]
    MADE.append(rid)
    text = "Добрый день коллеги а теперь передаю слово Ольге"
    seg = store.make_segment("far", 10, 18, text, speaker="Участник", speaker_key="SPEAKER_00")
    if timed:
        seg["words"] = words(text, 10, 1.0)
    other = store.make_segment("mic", 20, 22, "Хорошо.", speaker="Я", speaker_key="me")
    store.replace_segments(rid, [seg, other])
    store.update(rid, {"diarized": True})
    store.refresh_participants(rid)
    return rid, seg["id"]


try:
    say("=== 1. Разрез по слову ===")
    rid, sid = record("Проверка 61 разрез")
    res = edits.split_segment(rid, sid, 4)        # перед словом «теперь»
    segs = store.sorted_segments(rid)
    far = [s for s in segs if s["track"] == "far"]
    check("стало две реплики вместо одной", len(far) == 2, [s["text"] for s in far])
    check("время слов было настоящим", res["exact"] is True, res["exact"])
    if len(far) == 2:
        left, right = far
        check("текст первой половины", left["text"] == "Добрый день коллеги а", left["text"])
        check("текст второй половины", right["text"] == "теперь передаю слово Ольге", right["text"])
        check("первая половина осталась той же репликой", left["id"] == sid, (left["id"], sid))
        check("вторая половина — новая реплика", right["id"] != sid and len(right["id"]) >= 6, right["id"])
        check("время подхвачено по словам", left["start"] == 10.0 and left["end"] == 14.0
              and right["start"] == 14.0 and right["end"] == 18.0,
              (left["start"], left["end"], right["start"], right["end"]))
        check("слова разошлись по половинам",
              [w["text"] for w in left["words"]] == ["Добрый", "день", "коллеги", "а"]
              and [w["text"] for w in right["words"]] == ["теперь", "передаю", "слово", "Ольге"],
              (left["words"], right["words"]))
        check("говорящий у обеих прежний",
              left["speaker_key"] == right["speaker_key"] == "SPEAKER_00")
    check("соседняя реплика не тронута",
          [s["text"] for s in segs if s["track"] == "mic"] == ["Хорошо."])
    check("порядок по времени не сбился",
          [round(s["start"], 3) for s in segs] == sorted(round(s["start"], 3) for s in segs),
          [s["start"] for s in segs])
    # Разрезать можно и половину: правка идёт от того, что сейчас на экране.
    edits.split_segment(rid, sid, 2)
    check("вторая правка тоже прошла", len(store.sorted_segments(rid)) == 4,
          [s["text"] for s in store.sorted_segments(rid)])

    say("")
    say("=== 2. Времени слов нет — граница на глаз ===")
    rid2, sid2 = record("Проверка 61 без времени", timed=False)
    res2 = edits.split_segment(rid2, sid2, 4)
    far2 = [s for s in store.sorted_segments(rid2) if s["track"] == "far"]
    check("сказано, что время неточное", res2["exact"] is False, res2["exact"])
    check("текст всё равно поделён верно",
          [s["text"] for s in far2] == ["Добрый день коллеги а", "теперь передаю слово Ольге"],
          [s["text"] for s in far2])
    if len(far2) == 2:
        l2, r2 = far2
        check("половины не вылезли за края реплики",
              l2["start"] == 10.0 and r2["end"] == 18.0 and 10.0 < l2["end"] < 18.0,
              (l2["start"], l2["end"], r2["start"], r2["end"]))
        check("стык без дырки", l2["end"] == r2["start"], (l2["end"], r2["start"]))
        # Придуманное время слов не храним: иначе потом не отличить его от
        # настоящего, пришедшего от точной модели.
        check("выдуманные слова не записаны", not l2["words"] and not r2["words"],
              (l2["words"], r2["words"]))

    say("")
    say("=== 3. Отказы объясняются ===")
    rid3, sid3 = record("Проверка 61 отказы")
    for name, index in (("перед первым словом резать нечего", 0), ("слово за краем реплики", 99)):
        try:
            edits.split_segment(rid3, sid3, index)
            check(name, False, "разрез прошёл, хотя не должен")
        except edits.EditError as err:
            check(name, "выберите слово" in str(err).lower() or "нечего" in str(err), str(err))
    try:
        edits.split_segment(rid3, "нет-такой", 2)
        check("чужая реплика не режется", False, "разрез прошёл")
    except edits.EditError as err:
        check("чужая реплика не режется", "не найдена" in str(err), str(err))
    check("после отказов стенограмма не изменилась",
          len(store.sorted_segments(rid3)) == 2, store.sorted_segments(rid3))
    check("снимок после отказа не оставлен", not edits.undo_info(rid3), edits.undo_info(rid3))

    say("")
    say("=== 4. Отмена возвращает разрез ===")
    rid4, sid4 = record("Проверка 61 отмена")
    before = store.sorted_segments(rid4)
    edits.split_segment(rid4, sid4, 3)
    check("отмена предлагается и названа по-русски",
          edits.undo_info(rid4).get("what") == "разрез реплики", edits.undo_info(rid4))
    edits.undo(rid4)
    after = store.sorted_segments(rid4)
    check("стенограмма вернулась целиком",
          [(s["id"], s["text"], s["start"], s["end"]) for s in after]
          == [(s["id"], s["text"], s["start"], s["end"]) for s in before], after)
    check("участники пересчитаны после отмены",
          isinstance(store.get(rid4).get("participants"), list), store.get(rid4).get("participants"))
    check("больше отменять нечего", not edits.undo_info(rid4), edits.undo_info(rid4))
    try:
        edits.undo(rid4)
        check("вторая отмена отказана", False, "отмена прошла второй раз")
    except edits.EditError as err:
        check("вторая отмена отказана: снимков больше нет", "нечего" in str(err), str(err))

    say("")
    say("=== 5. Точки службы ===")
    from fastapi.testclient import TestClient  # noqa: E402

    from hagen import server  # noqa: E402

    ORIGIN = {"Origin": "http://127.0.0.1:8787"}
    rid5, sid5 = record("Проверка 61 служба")
    with TestClient(server.app, base_url="http://127.0.0.1:8787") as cli:
        r = cli.post(f"/api/recordings/{rid5}/transcript/split",
                     json={"segment_id": sid5, "word_index": 4}, headers=ORIGIN)
        body = r.json() if r.status_code == 200 else {}
        check("служба режет реплику", r.status_code == 200, r.text[:200])
        check("в ответе свежая стенограмма",
              [s["text"] for s in body.get("segments", []) if s["track"] == "far"]
              == ["Добрый день коллеги а", "теперь передаю слово Ольге"], r.text[:200])
        check("сказано, точное ли время", body.get("exact") is True, body.get("exact"))
        r = cli.get(f"/api/recordings/{rid5}/transcript/undo")
        check("служба говорит, что можно отменить",
              r.status_code == 200 and r.json().get("what") == "разрез реплики", r.text[:120])
        r = cli.post(f"/api/recordings/{rid5}/transcript/undo", headers=ORIGIN)
        check("служба отменяет правку", r.status_code == 200, r.text[:200])
        check("после отмены снова одна реплика",
              len([s for s in r.json().get("segments", []) if s["track"] == "far"]) == 1, r.text[:200])
        r = cli.get(f"/api/recordings/{rid5}/transcript/undo")
        check("отменять больше нечего", r.status_code == 200 and not r.json().get("what"), r.text[:120])
        r = cli.post(f"/api/recordings/{rid5}/transcript/undo", headers=ORIGIN)
        check("повторная отмена объяснена, а не сломала службу",
              r.status_code == 400 and "нечего" in r.text, r.text[:200])
        r = cli.post("/api/recordings/нет-такой/transcript/split",
                     json={"segment_id": "x", "word_index": 1}, headers=ORIGIN)
        check("чужая запись — 404", r.status_code == 404, r.status_code)
        r = cli.post(f"/api/recordings/{rid5}/transcript/split",
                     json={"segment_id": "нет-такой", "word_index": 1}, headers=ORIGIN)
        check("чужая реплика — понятный отказ службы",
              r.status_code == 400 and "не найдена" in r.text, r.text[:200])
finally:
    for rid in MADE:
        try:
            store.delete(rid)
        except Exception:
            pass

say("")
say("=== 6. Страница ===")
html = io.open(PROJECT / "hagen" / "static" / "index.html", encoding="utf-8").read()
js = io.open(PROJECT / "hagen" / "static" / "app.js", encoding="utf-8").read()
css = io.open(PROJECT / "hagen" / "static" / "app.css", encoding="utf-8").read()
check("кнопка «Править» рядом со стенограммой",
      'id="btn-edit-transcript"' in html and "Править" in html)
check("кнопка «Отменить правку» есть", 'id="btn-undo-edit"' in html)
check("объяснено, что делает Alt+щелчок по слову",
      "Alt+щелчок" in html and ("разрежется перед ним" in html or "разрезает реплику перед" in html))
check("режим включается и выключается", "setEditMode(!S.editMode)" in js)
check("в режиме правки слова становятся кнопками",
      "function editWords" in js and "class=\"w\"" in js)
check("разрез — по Alt+щелчку, простой щелчок не режет",
      "el.onclick" in js and "if (!e.altKey" in js
      and "splitPhrase(" in js and "/transcript/split" in js)
check("первое слово не режется", "w-first" in js and "w-first" in css)
check("отмена подключена", "$('btn-undo-edit').onclick = undoEdit" in js
      and "/transcript/undo" in js)
check("неточную границу показываем словами", "на глаз" in js)
check("выход из записи выключает правку", "if (S.editMode) setEditMode(false)" in js)

say("")
say("ИТОГО провалов: %d" % len(FAIL))
for f in FAIL:
    say("   - " + f)
io.open(PROJECT / "tests" / "t61_result.txt", "w", encoding="utf-8").write("\n".join(LINES))
sys.exit(1 if FAIL else 0)
