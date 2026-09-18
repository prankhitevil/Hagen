# -*- coding: utf-8 -*-
"""Проверка 70: ручные решения переживают «Перечитать точнее» (17.09).

Владелец разметил стенограмму руками, нажал «Перечитать точнее» — и всё
сбросилось: переразбор собирает реплики заново. Текст правок вернуть нельзя
(он распознаётся заново), а вот решения «чьи это слова» можно: они привязаны
ко времени, а звук тот же.

Что проверяем:
  1. счётчик ручных правок растёт и переживает выброс снимков;
  2. ручные решения запоминаются отрезками времени (by_hand);
  3. они ложатся на пересобранную стенограмму, даже когда границы реплик
     сдвинулись и число реплик другое;
  4. чужая дорожка и куски вне отрезков не трогаются;
  5. точка службы отвечает числами, 404 на чужую запись;
  6. на странице предупреждение показывается и спрашивает счётчик.

База голосов и настройки — временные (isolate), записи проверка удаляет за собой.
Запуск из корня проекта:  .venv\\Scripts\\python.exe tests\\t70_keep_hands.py
"""
import io
import re
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
    if str(old.get("title") or "").startswith("Проверка 70"):
        store.delete(old["id"])

TOP = "Ну давайте да по зерну приняли сто пятьдесят"
BOTTOM = "Недели две крахмала сто двадцать тонн уехало"


def words(text, start, step=1.0):
    out, at = [], float(start)
    for part in text.split():
        out.append({"text": part, "start": round(at, 3), "end": round(at + step, 3)})
        at += step
    return out


def record(title):
    """Две реплики: внизу — эхо колонок, приписанное владельцу."""
    rid = store.create(title=title, mode="online", source="live", category="Встречи")["id"]
    MADE.append(rid)
    top = store.make_segment("far", 10, 18, TOP, speaker="Саша Пушкин", speaker_key="SPEAKER_00")
    bot = store.make_segment("mic", 20, 27, BOTTOM, speaker="Иван Петров", speaker_key="me")
    top["words"] = words(TOP, 10)
    bot["words"] = words(BOTTOM, 20)
    store.replace_segments(rid, [top, bot])
    store.update(rid, {"diarized": True})
    store.refresh_participants(rid)
    return rid, top["id"], bot["id"]


def remake(rid):
    """Как после переразбора: те же секунды, но реплики нарезаны иначе."""
    fresh = [
        store.make_segment("far", 10.1, 14.0, "Ну давайте да", speaker="Саша Пушкин",
                           speaker_key="SPEAKER_00"),
        store.make_segment("far", 14.0, 17.9, "по зерну приняли сто пятьдесят",
                           speaker="Саша Пушкин", speaker_key="SPEAKER_00"),
        store.make_segment("mic", 20.2, 23.5, "Недели две крахмала",
                           speaker="Иван Петров", speaker_key="me"),
        store.make_segment("mic", 23.5, 26.8, "сто двадцать тонн уехало",
                           speaker="Иван Петров", speaker_key="me"),
        store.make_segment("mic", 40.0, 44.0, "Наташ подскажи пожалуйста",
                           speaker="Иван Петров", speaker_key="me"),
    ]
    store.replace_segments(rid, fresh)
    return fresh


try:
    say("=== 1. Счётчик ручных правок ===")
    rid, top, bot = record("Проверка 70 счёт")
    check("сначала правок нет", edits.counts(rid)["edits"] == 0, edits.counts(rid))
    edits.assign_speaker(rid, bot, 0, 6, "SPEAKER_00")   # вся реплика — Пушкину
    c = edits.counts(rid)
    check("правка посчитана", c["edits"] == 1, c)
    check("решение о говорящем посчитано", c["speakers"] == 1, c)
    edits.edit_text(rid, store.sorted_segments(rid)[0]["id"], TOP + " тонн")
    c = edits.counts(rid)
    check("вторая правка посчитана", c["edits"] == 2, c)
    check("правка текста посчитана отдельно", c["texts"] == 1, c)
    for p in (store.paths(rid)["dir"] / edits.UNDO_DIR).glob("*.json"):
        p.unlink()                                        # снимки выброшены по сроку
    check("счётчик пережил выброс снимков", edits.counts(rid)["edits"] == 2, edits.counts(rid))

    say("")
    say("=== 2. Решения помнятся отрезками времени ===")
    rid2, top2, bot2 = record("Проверка 70 отрезки")
    edits.assign_speaker(rid2, bot2, 0, 6, "SPEAKER_00")
    marks = edits.hand_marks(rid2)
    check("один отрезок", len(marks) == 1, marks)
    check("отрезок — про эхо в микрофоне",
          marks[0]["track"] == "mic" and marks[0]["start"] == 20.0 and marks[0]["end"] == 27.0,
          marks[0])
    check("в отрезке записан выбранный человек",
          marks[0]["speaker_key"] == "SPEAKER_00" and marks[0]["speaker"] == "Саша Пушкин",
          marks[0])

    say("")
    say("=== 3. Решения ложатся на пересобранную стенограмму ===")
    remake(rid2)
    check("после переразбора эхо снова на владельце",
          [s["speaker"] for s in store.sorted_segments(rid2)][2:4] == ["Иван Петров", "Иван Петров"],
          [s["speaker"] for s in store.sorted_segments(rid2)])
    back = edits.apply_hand_marks(rid2, marks)
    segs = store.sorted_segments(rid2)
    check("две новые реплики забрал Пушкин", back == 2, back)
    check("имена расставлены верно",
          [s["speaker"] for s in segs]
          == ["Саша Пушкин", "Саша Пушкин", "Саша Пушкин", "Саша Пушкин", "Иван Петров"],
          [s["speaker"] for s in segs])
    check("решение помечено как ручное",
          segs[2].get("by_hand") is True and segs[2].get("speaker_locked") is True, segs[2])
    check("реплика вне отрезка не тронута",
          not segs[4].get("by_hand") and segs[4]["speaker"] == "Иван Петров", segs[4])
    check("чужая дорожка не помечена ручной",
          not segs[0].get("by_hand") and not segs[1].get("by_hand"), segs[0])
    check("текст правок не возвращается",
          [s["text"] for s in segs][2] == "Недели две крахмала", segs[2]["text"])

    say("")
    say("=== 4. Отрезок не налезает на чужую дорожку ===")
    rid4, top4, bot4 = record("Проверка 70 дорожки")
    mark_far = [{"track": "far", "start": 20.0, "end": 27.0,
                 "speaker": "Саша Пушкин", "speaker_key": "SPEAKER_00"}]
    check("на другой дорожке не применилось", edits.apply_hand_marks(rid4, mark_far) == 0)
    check("пустой список ничего не делает", edits.apply_hand_marks(rid4, []) == 0)
    check("стенограмма цела", [s["text"] for s in store.sorted_segments(rid4)] == [TOP, BOTTOM],
          [s["text"] for s in store.sorted_segments(rid4)])

    say("")
    say("=== 5. Точка службы ===")
    from fastapi.testclient import TestClient  # noqa: E402

    from hagen import server  # noqa: E402

    ORIGIN = {"Origin": "http://127.0.0.1:8787"}
    with TestClient(server.app, base_url="http://127.0.0.1:8787") as client:
        r = client.get("/api/recordings/%s/transcript/edits" % rid, headers=ORIGIN)
        check("служба отвечает числами",
              r.status_code == 200 and r.json()["edits"] == 2 and r.json()["texts"] == 1, r.text)
        r404 = client.get("/api/recordings/нет-такой/transcript/edits", headers=ORIGIN)
        check("чужая запись — 404", r404.status_code == 404, r404.status_code)

    say("")
    say("=== 6. Страница ===")
    js = io.open(PROJECT / "hagen" / "static" / "app.js", encoding="utf-8").read()
    html = io.open(PROJECT / "hagen" / "static" / "index.html", encoding="utf-8").read()
    check("предупреждение есть в окне", 'id="repass-edits"' in html)
    check("оно спрашивает счётчик", "transcript/edits" in js)
    check("оно показывается при открытии окна", re.search(r"warnAboutEdits\(room\)", js) is not None)
    check("правки текста названы пропадающими", "распознается заново" in js)
    check("решения о говорящих названы сохраняющимися", "сохранятся" in js)
    check("склонение чисел на месте", "ручных правок" in js and "function plural" in js)

except Exception as err:                       # noqa: BLE001
    FAIL.append("проверка оборвалась")
    say("ОБОРВАЛОСЬ: %s: %s" % (type(err).__name__, err))
finally:
    for rid in MADE:
        try:
            store.delete(rid)
        except Exception:
            pass

say("")
say("Всего замечаний: %d" % len(FAIL))
for name in FAIL:
    say("   — " + name)
io.open(PROJECT / "tests" / "t70_result.txt", "w", encoding="utf-8").write("\n".join(LINES))
sys.exit(1 if FAIL else 0)
