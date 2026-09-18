# -*- coding: utf-8 -*-
"""Проверка 84: кнопка «Пересчитать эхо» (решение 17.09).

Раньше эхо пересчитывалось только вместе с пересборкой стенограммы: после
«Стоп», после «Перечитать точнее» и после разметки говорящих. Чтобы почистить
уже готовую запись, приходилось перечитывать её целиком — минуты работы
процессора и потеря ручных правок текста. Сам пересчёт занимает доли секунды.

Что проверяем:
  1. пересчёт находит двойники в готовой стенограмме и прячет их;
  2. текст реплик при этом не трогается, ручные правки целы;
  3. пометка снимается, если реплику поправили и она перестала быть эхом;
  4. выключенный отсев эха отвечает понятным отказом, чужая запись — 404;
  5. по голосу считаем только у размеченных записей;
  6. работа идёт задачей, повторный запуск отклоняется;
  7. на странице есть кнопка, она прячется без дорожки собеседников и
     объясняет, чем отличается от «Перечитать точнее».

База голосов и настройки — временные (isolate), записи проверка удаляет за собой.
Запуск из корня проекта:  .venv\\Scripts\\python.exe tests\\t84_echo_recheck.py
"""
import io
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


from hagen import config, store  # noqa: E402

for old in store.list_all():
    if str(old.get("title") or "").startswith("Проверка 84"):
        store.delete(old["id"])

FAR_A = "У нас по безопасности инцидентов не зарегистрировано по производству"
FAR_B = "149 тонн зерна приняли 590 тонн надробили 2,5 часа простояли"
MIC_BOTH = ("У нас по безопасности инцидентов не зарегистрировано по производству "
            "149 тонн зерна приняли 590 тонн надробили 2,5 часа простояли")
MINE = "Наташ подскажи пожалуйста по отгрузкам за эту неделю"


def seg(track, start, end, text, **extra):
    s = store.make_segment(track, start, end, text)
    s.update(extra)
    return s


def record(title):
    """Готовая стенограмма с двойником — как её видели 17.09."""
    rid = store.create(title=title, mode="online", source="live", category="Встречи")["id"]
    MADE.append(rid)
    store.replace_segments(rid, [
        seg("far", 45.0, 52.0, FAR_A, speaker="Сергей", speaker_key="SPEAKER_00"),
        seg("far", 52.0, 60.0, FAR_B, speaker="Сергей", speaker_key="SPEAKER_00"),
        seg("mic", 45.5, 60.5, MIC_BOTH, speaker="Иван Петров", speaker_key="me"),
        seg("mic", 80.0, 86.0, MINE, speaker="Иван Петров", speaker_key="me"),
    ])
    return rid


def texts(rid):
    return [s["text"][:30] for s in store.sorted_segments(rid)]


def wait_job(cli, job_id, origin, seconds=20):
    for _ in range(int(seconds * 10)):
        jobs = cli.get("/api/jobs", headers=origin).json()
        row = next((j for j in (jobs if isinstance(jobs, list) else jobs.get("jobs", []))
                    if j.get("id") == job_id), None)
        if row and row.get("status") in ("done", "error", "cancelled"):
            return row
        time.sleep(0.1)
    return None


try:
    config.save({"echo_filter": True, "echo_match_threshold": 0.6, "echo_by_voice": False})

    from fastapi.testclient import TestClient  # noqa: E402

    from hagen import echo, server  # noqa: E402

    ORIGIN = {"Origin": "http://127.0.0.1:8787"}
    rid = record("Проверка 84 пересчёт")
    check("двойник в стенограмме виден", len(store.sorted_segments(rid)) == 4, texts(rid))

    with TestClient(server.app, base_url="http://127.0.0.1:8787") as cli:
        say("=== 1. Пересчёт убирает двойник ===")
        r = cli.post("/api/recordings/%s/echo" % rid, headers=ORIGIN)
        check("задача поставлена", r.status_code == 200 and r.json().get("job_id"), r.text)
        row = wait_job(cli, r.json()["job_id"], ORIGIN)
        check("задача завершилась", row is not None and row["status"] == "done", row)
        check("двойник спрятан", len(store.sorted_segments(rid)) == 3, texts(rid))
        check("своя речь на месте",
              any(s["text"] == MINE for s in store.sorted_segments(rid)), texts(rid))
        check("чужие фразы целы",
              len([s for s in store.sorted_segments(rid) if s["track"] == "far"]) == 2)
        got = row.get("result") or {}
        check("сказано, сколько отсеяно", got.get("by_text") == 1, got)

        say("")
        say("=== 2. Текст не переписывается ===")
        all_segs = store.sorted_segments(rid, include_echo=True)
        check("текст реплик прежний",
              [s["text"] for s in all_segs] == [FAR_A, MIC_BOTH, FAR_B, MINE],
              [s["text"][:24] for s in all_segs])
        check("эхо не удалено, а помечено",
              sum(1 for s in all_segs if s.get("echo")) == 1, all_segs)

        say("")
        say("=== 3. Пометка снимается, если реплику поправили ===")
        fixed = store.sorted_segments(rid, include_echo=True)
        for s in fixed:
            if s.get("echo"):
                s["text"] = "А я спрошу у Алексея про поставку в пятницу"
        store.replace_segments(rid, fixed)
        r3 = cli.post("/api/recordings/%s/echo" % rid, headers=ORIGIN)
        wait_job(cli, r3.json()["job_id"], ORIGIN)
        check("реплика вернулась в стенограмму", len(store.sorted_segments(rid)) == 4,
              texts(rid))

        say("")
        say("=== 4. Отказы ===")
        r404 = cli.post("/api/recordings/нет-такой/echo", headers=ORIGIN)
        check("чужая запись — 404", r404.status_code == 404, r404.status_code)
        config.save({"echo_filter": False})
        roff = cli.post("/api/recordings/%s/echo" % rid, headers=ORIGIN)
        check("выключенный отсев — понятный отказ",
              roff.status_code == 409 and "настройках" in roff.json().get("detail", ""),
              roff.text)
        config.save({"echo_filter": True})

        say("")
        say("=== 5. По голосу — только у размеченных ===")
        seen = {"n": 0}
        real_voice = echo.mark_by_voice
        echo.mark_by_voice = lambda *a, **k: (seen.__setitem__("n", seen["n"] + 1), 0)[1]
        try:
            config.save({"echo_by_voice": True})
            rid5 = record("Проверка 84 без разметки")
            r5 = cli.post("/api/recordings/%s/echo" % rid5, headers=ORIGIN)
            wait_job(cli, r5.json()["job_id"], ORIGIN)
            check("неразмеченную по голосу не считаем", seen["n"] == 0, seen)
            store.update(rid5, {"diarized": True})
            r6 = cli.post("/api/recordings/%s/echo" % rid5, headers=ORIGIN)
            wait_job(cli, r6.json()["job_id"], ORIGIN)
            check("размеченную считаем", seen["n"] == 1, seen)
        finally:
            echo.mark_by_voice = real_voice
            config.save({"echo_by_voice": False})

    say("")
    say("=== 6. Работа идёт задачей ===")
    srv = io.open(PROJECT / "hagen" / "server.py", encoding="utf-8").read()
    # Маршруты правки стенограммы и пересчёта эха переехали в роутер
    # — смотрим туда, где код живёт теперь.
    api = io.open(PROJECT / "hagen" / "api" / "transcript.py", encoding="utf-8").read()
    check("задача, а не ожидание в запросе", 'jobs.submit("echo"' in api)
    check("повторный запуск отклоняется", 'jobs.busy_with("echo", rec_id)' in api)
    check("запись считается занятой", '"media", "echo")' in srv)

    say("")
    say("=== 7. Страница ===")
    html = io.open(PROJECT / "hagen" / "static" / "index.html", encoding="utf-8").read()
    js = io.open(PROJECT / "hagen" / "static" / "app.js", encoding="utf-8").read()
    check("кнопка есть", 'id="btn-echo"' in html)
    check("объяснено отличие от «Перечитать точнее»",
          "стирает ручные правки" in html and "секунды" in html)
    check("кнопка подключена", "/echo`, { method: 'POST' }" in js)
    check("прячется без дорожки собеседников", "twoTracks" in js)
    check("во время работы недоступна", "'btn-echo'" in js)

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
io.open(PROJECT / "tests" / "t84_result.txt", "w", encoding="utf-8").write("\n".join(LINES))
sys.exit(1 if FAIL else 0)
