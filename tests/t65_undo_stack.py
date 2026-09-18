# -*- coding: utf-8 -*-
"""Проверка 65: отмена правок стенограммы — несколько шагов назад (16.09).

Решение 16.09: отменять надо не одну правку, а несколько подряд, в
том числе после выхода из режима правки и возврата в него. Без предела копить
нельзя — снимок это вся стенограмма записи, поэтому есть глубина и срок
(«Настройки → Продвинутые»).

Что проверяем:
  1. правки отменяются по очереди, в обратном порядке, до самого начала;
  2. запас переживает выход из режима правки: снимки лежат на диске;
  3. глубина: лишние снимки выбрасываются, отменить можно столько, сколько
     разрешено, и стенограмма при этом не ломается;
  4. срок: просроченные снимки не предлагаются и не занимают место;
  5. 0 в настройке — без предела;
  6. снимок старого образца (одна ступень, transcript.undo.json) подхватывается;
  7. снимки уезжают вместе с записью при удалении;
  8. служба сообщает, сколько шагов в запасе и сколько осталось;
  9. на странице: Ctrl+Z только в режиме правки, счётчик на кнопке, поля
     глубины и срока в настройках.

База голосов и настройки — временные (isolate), записи проверка удаляет за собой.
Запуск из корня проекта:  .venv\\Scripts\\python.exe tests\\t65_undo_stack.py
"""
import io
import json
import os
import sys
import time
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


from hagen import config, edits, store  # noqa: E402

for old in store.list_all():
    if str(old.get("title") or "").startswith("Проверка 65"):
        store.delete(old["id"])

TEXT = "Раз два три четыре пять шесть семь восемь"


def limits(steps, minutes):
    """Глубина и срок — как из «Продвинутых», но во временных настройках."""
    config.save({"edit_undo_steps": steps, "edit_undo_minutes": minutes})


def record(title):
    rid = store.create(title=title, mode="online", source="live", category="Встречи")["id"]
    MADE.append(rid)
    seg = store.make_segment("far", 10, 18, TEXT, speaker="Участник", speaker_key="SPEAKER_00")
    seg["words"] = [{"text": w, "start": 10.0 + i, "end": 11.0 + i}
                    for i, w in enumerate(TEXT.split())]
    store.replace_segments(rid, [seg])
    store.update(rid, {"diarized": True})
    store.refresh_participants(rid)
    return rid, seg["id"]


def count(rid):
    return len(store.sorted_segments(rid))


def undo_dir(rid):
    return store.paths(rid)["dir"] / edits.UNDO_DIR


try:
    say("=== 1. Несколько шагов назад ===")
    limits(20, 30)
    rid, sid = record("Проверка 65 стопка")
    start = store.sorted_segments(rid)
    edits.split_segment(rid, sid, 4)                 # 2 реплики
    edits.split_segment(rid, sid, 2)                 # 3
    edits.edit_text(rid, sid, "Раз ДВА")             # текст первой
    check("в запасе три шага", edits.undo_info(rid).get("steps") == 3, edits.undo_info(rid))
    check("сверху лежит последняя правка",
          edits.undo_info(rid).get("what") == "правка текста", edits.undo_info(rid))
    r1 = edits.undo(rid)
    check("первая отмена вернула текст",
          store.sorted_segments(rid)[0]["text"] == "Раз два", store.sorted_segments(rid)[0]["text"])
    check("служба знает, сколько осталось", r1["steps_left"] == 2, r1)
    edits.undo(rid)
    check("вторая отмена вернула второй разрез", count(rid) == 2, count(rid))
    r3 = edits.undo(rid)
    check("третья отмена вернула первый разрез", count(rid) == 1, count(rid))
    check("стенограмма вернулась в исходный вид",
          [(s["id"], s["text"]) for s in store.sorted_segments(rid)]
          == [(s["id"], s["text"]) for s in start], store.sorted_segments(rid))
    check("запас кончился", r3["steps_left"] == 0 and not edits.undo_info(rid), r3)
    check("папка снимков не мусорит",
          not undo_dir(rid).exists() or not list(undo_dir(rid).glob("*.json")),
          list(undo_dir(rid).glob("*.json")) if undo_dir(rid).exists() else [])
    try:
        edits.undo(rid)
        check("лишняя отмена отказана", False, "отмена прошла")
    except edits.EditError as err:
        check("лишняя отмена отказана", "нечего" in str(err), str(err))

    say("")
    say("=== 1б. Отмена одинаково работает для всех видов правки ===")
    rid1b, sid1b = record("Проверка 65 все виды")
    # Нужен второй человек: иначе «передать другому» некому.
    other = store.make_segment("far", 20, 22, "Понял", speaker="Ольга", speaker_key="SPEAKER_01")
    store.replace_segments(rid1b, store.sorted_segments(rid1b) + [other])
    was = [(s["speaker"], s["text"]) for s in store.sorted_segments(rid1b)]
    edits.split_segment(rid1b, sid1b, 4)
    second = [s["id"] for s in store.sorted_segments(rid1b)][1]
    edits.move_words(rid1b, sid1b, 3, 3, "next")            # перенос соседу
    edits.assign_speaker(rid1b, second, 0, 1, "SPEAKER_01")  # передача другому
    edits.edit_text(rid1b, sid1b, "Раз два ТРИ")             # правка текста
    kinds = []
    while edits.undo_info(rid1b):
        kinds.append(edits.undo_info(rid1b)["what"])
        edits.undo(rid1b)
    check("отменились все четыре вида, с конца",
          kinds == ["правка текста", "передача слов другому",
                    "перенос слов соседу", "разрез реплики"], kinds)
    check("стенограмма вернулась к исходной",
          [(s["speaker"], s["text"]) for s in store.sorted_segments(rid1b)] == was,
          store.sorted_segments(rid1b))

    say("")
    say("=== 2. Глубина ===")
    limits(2, 30)
    rid2, sid2 = record("Проверка 65 глубина")
    edits.split_segment(rid2, sid2, 4)
    edits.split_segment(rid2, sid2, 2)
    edits.split_segment(rid2, sid2, 1)
    check("хранится только разрешённое число снимков",
          edits.undo_info(rid2).get("steps") == 2, edits.undo_info(rid2))
    check("лишний снимок удалён с диска", len(list(undo_dir(rid2).glob("*.json"))) == 2,
          list(undo_dir(rid2).glob("*.json")))
    edits.undo(rid2)
    edits.undo(rid2)
    check("отменилось ровно два шага, дальше — нечего",
          count(rid2) == 2 and not edits.undo_info(rid2), (count(rid2), edits.undo_info(rid2)))
    check("стенограмма цела после упора в глубину",
          " ".join(s["text"] for s in store.sorted_segments(rid2)) == TEXT,
          [s["text"] for s in store.sorted_segments(rid2)])

    say("")
    say("=== 3. Срок ===")
    limits(20, 30)
    rid3, sid3 = record("Проверка 65 срок")
    edits.split_segment(rid3, sid3, 4)
    edits.split_segment(rid3, sid3, 2)
    files = sorted(undo_dir(rid3).glob("*.json"))
    old_time = time.time() - 31 * 60
    os.utime(files[0], (old_time, old_time))         # первый снимок «состарился»
    check("просроченный снимок не предлагается",
          edits.undo_info(rid3).get("steps") == 1, edits.undo_info(rid3))
    check("просроченный снимок удалён с диска", len(list(undo_dir(rid3).glob("*.json"))) == 1,
          list(undo_dir(rid3).glob("*.json")))
    edits.undo(rid3)
    check("свежая правка всё равно отменяется", count(rid3) == 2, count(rid3))

    say("")
    say("=== 4. Без предела ===")
    limits(0, 0)
    rid4, sid4 = record("Проверка 65 без предела")
    for cut in (7, 6, 5, 4, 3, 2, 1):
        edits.split_segment(rid4, sid4, cut)
    check("хранятся все семь снимков", edits.undo_info(rid4).get("steps") == 7, edits.undo_info(rid4))
    ancient = time.time() - 10 * 24 * 3600
    for p in undo_dir(rid4).glob("*.json"):
        os.utime(p, (ancient, ancient))
    check("при нуле в сроке старые снимки не выбрасываются",
          edits.undo_info(rid4).get("steps") == 7, edits.undo_info(rid4))
    for _ in range(7):
        edits.undo(rid4)
    check("отмотали до самого начала", count(rid4) == 1 and not edits.undo_info(rid4), count(rid4))
    limits(20, 30)

    say("")
    say("=== 5. Снимок старого образца ===")
    rid5, sid5 = record("Проверка 65 старый снимок")
    was = store.sorted_segments(rid5)
    (store.paths(rid5)["dir"] / edits.UNDO_NAME).write_text(
        json.dumps({"what": "старая правка", "segments": was}, ensure_ascii=False), encoding="utf-8")
    check("старый снимок виден как шаг назад",
          edits.undo_info(rid5).get("steps") == 1
          and edits.undo_info(rid5).get("what") == "старая правка", edits.undo_info(rid5))
    check("старый файл перенесён в стопку",
          not (store.paths(rid5)["dir"] / edits.UNDO_NAME).exists())
    edits.undo(rid5)
    check("старый снимок отменяется как обычный",
          [s["text"] for s in store.sorted_segments(rid5)] == [s["text"] for s in was])

    say("")
    say("=== 6. Удаление записи уносит снимки ===")
    rid6, sid6 = record("Проверка 65 удаление")
    edits.split_segment(rid6, sid6, 3)
    folder = undo_dir(rid6)
    check("снимок лежит рядом с записью", folder.exists() and list(folder.glob("*.json")))
    store.delete(rid6)
    MADE.remove(rid6)
    check("после удаления записи снимков не осталось", not folder.exists(), folder)

    say("")
    say("=== 7. Точки службы ===")
    from fastapi.testclient import TestClient  # noqa: E402

    from hagen import server  # noqa: E402

    ORIGIN = {"Origin": "http://127.0.0.1:8787"}
    rid7, sid7 = record("Проверка 65 служба")
    with TestClient(server.app, base_url="http://127.0.0.1:8787") as cli:
        cli.post(f"/api/recordings/{rid7}/transcript/split",
                 json={"segment_id": sid7, "word_index": 4}, headers=ORIGIN)
        cli.post(f"/api/recordings/{rid7}/transcript/split",
                 json={"segment_id": sid7, "word_index": 2}, headers=ORIGIN)
        r = cli.get(f"/api/recordings/{rid7}/transcript/undo")
        check("служба говорит, сколько шагов в запасе",
              r.status_code == 200 and r.json().get("steps") == 2, r.text[:200])
        r = cli.post(f"/api/recordings/{rid7}/transcript/undo", headers=ORIGIN)
        check("служба сообщает остаток шагов",
              r.status_code == 200 and r.json().get("steps_left") == 1, r.text[:200])
        r = cli.post(f"/api/recordings/{rid7}/transcript/undo", headers=ORIGIN)
        check("последняя отмена оставляет ноль",
              r.status_code == 200 and r.json().get("steps_left") == 0, r.text[:200])
        r = cli.post(f"/api/recordings/{rid7}/transcript/undo", headers=ORIGIN)
        check("дальше — понятный отказ", r.status_code == 400 and "нечего" in r.text, r.text[:200])
finally:
    for rid in MADE:
        try:
            store.delete(rid)
        except Exception:
            pass

say("")
say("=== 8. Настройки и страница ===")
check("заводская глубина — 20 правок", config.DEFAULTS["edit_undo_steps"] == 20,
      config.DEFAULTS["edit_undo_steps"])
check("заводской срок — 30 минут", config.DEFAULTS["edit_undo_minutes"] == 30,
      config.DEFAULTS["edit_undo_minutes"])
html = io.open(PROJECT / "hagen" / "static" / "index.html", encoding="utf-8").read()
js = io.open(PROJECT / "hagen" / "static" / "app.js", encoding="utf-8").read()
pane = html[html.find('id="t-advanced"'):]
check("поле глубины на вкладке «Продвинутые»", 'id="set-undo-steps"' in pane)
check("поле срока на вкладке «Продвинутые»", 'id="set-undo-minutes"' in pane)
check("объяснено, почему запас не бесконечный", "копию всей стенограммы" in pane)
check("у полей есть значок «?» с полной подсказкой",
      pane.count('class="q"') >= 7 and "Пусто — как у модели" in pane)
check("сказано, что выход из режима правки запас не сбрасывает",
      "«Готово», вернулись" in pane)
check("поля подключены к настройкам",
      "'edit_undo_steps'" in js and "'edit_undo_minutes'" in js and "UNDO_FIELDS" in js)
check("пустое поле — заводское значение, а не «без предела»",
      "out[key] = (raw === '' || !Number.isFinite(n) || n < 0) ? def : n;" in js)
check("Ctrl+Z работает только в режиме правки",
      "if (!S.editMode || !e.ctrlKey" in js and "'z'" in js and "undoEdit()" in js)
check("Ctrl+Z не мешает в полях ввода", js.count("['INPUT', 'TEXTAREA', 'SELECT']") >= 2)
check("на кнопке видно, сколько шагов в запасе", "Отменить правку (${info.steps})" in js)
check("в подсказке названо Ctrl+Z", "Ctrl+Z" in html)

say("")
say("=== 9. Вкладка «Сочетания» ===")
check("вкладка есть", 'data-tab="t-keys"' in html and 'id="t-keys"' in html)
keys = html[html.find('id="t-keys"'):html.find('id="t-advanced"')]
for combo in ("Alt</b> + щелчок", "<b>Alt</b> + <b>↑</b>", "<b>Alt</b> + <b>↓</b>",
              "<b>Enter</b>", "Двойной щелчок", "<b>Escape</b>", "<b>Ctrl</b> + <b>Z</b>"):
    check("в списке есть «%s»" % combo.replace("<b>", "").replace("</b>", ""), combo in keys)
check("сказано, что клавиши работают в режиме правки", "«Править»" in keys)
check("сказано, что Ctrl+Z отменяет любое действие правки",
      "разрез, перенос" in keys and "правку текста" in keys)
check("сочетание набора текста продублировано", 'id="btn-keys-hotkey"' in keys
      and keys.count('class="ghost small js-hk"') == 4)
check("сказано, что сочетание одно на две вкладки", "поменялось и там" in keys)
check("кнопка сочетания подключена", "$('btn-keys-hotkey').onclick = captureHotkey" in js)
check("сочетание хранится одно на обе вкладки",
      "function setHotkey" in js and "'btn-dictate-hotkey', 'btn-keys-hotkey'" in js)
check("готовые сочетания работают на обеих вкладках",
      "document.querySelectorAll('.js-hk')" in js and "#t-dictate .js-hk" not in js)
check("обе кнопки показывают одно и то же",
      "hotkeyButtons().forEach((b) => { b.textContent = text; });" in js)

say("")
say("ИТОГО провалов: %d" % len(FAIL))
for f in FAIL:
    say("   - " + f)
io.open(PROJECT / "tests" / "t65_result.txt", "w", encoding="utf-8").write("\n".join(LINES))
sys.exit(1 if FAIL else 0)
