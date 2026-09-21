# -*- coding: utf-8 -*-
"""Проверка 98: дорожки очереди задач (20.09).

До 20.09 очередь была одна на всё, и задачи ждали друг друга без причины: пока
распознавался часовой ролик, саммари соседней записи стояло в очереди — хотя
считает его языковая модель, а не наш процессор; второе видео не начинало
скачиваться, хотя скачивание это сеть.

Теперь очередей несколько, по роду занятия: ``heavy`` — счёт на нашем
процессоре (по одной), ``media`` — добыть файл, ``docs`` — документы, ``net`` —
качать части программы.

Главная тонкость — обработка видео. Она и качает, и распознаёт, поэтому идёт по
лёгкой дорожке, а на распознавании берёт общий ПРОПУСК НА ТЯЖЁЛЫЙ СЧЁТ — тот
же, что держит разметка голосов. Так скачиваний может идти несколько сразу, а
считает процессор всё равно одну запись за раз.

Что проверяем:
  1. род занятия по виду задачи; незнакомый вид — в тяжёлую, осторожности ради;
  2. разные дорожки идут одновременно;
  3. две тяжёлые — строго по очереди;
  4. пропуск: распознавание внутри скачивания ждёт разметку, и наоборот;
  5. скачивания при этом не ждут ничего;
  6. пропуск не залипает, если задача упала;
  7. порядок внутри дорожки сохраняется;
  8. отмена задачи, стоящей в очереди, по-прежнему работает.

Запуск из корня проекта:  .venv\\Scripts\\python.exe tests\\t98_job_lanes.py
"""
import sys
import threading
import time
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
sys.path.insert(0, str(PROJECT / "tests"))
import isolate  # noqa: E402

isolate.settings()

from hagen import jobs  # noqa: E402

FAIL = []


def ok(label, cond, detail=""):
    print("   %s   %s%s" % ("ok  " if cond else "ПРОВАЛ", label,
                            ("" if not detail else ": %s" % detail)))
    if not cond:
        FAIL.append(label)


def wait_done(job_ids, timeout=12.0):
    """Дождаться, пока все задачи закончатся."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        left = [i for i in job_ids
                if (jobs.get(i) or {}).get("status") in ("queued", "running")]
        if not left:
            return True
        time.sleep(0.05)
    return False


print("=== 1. Род занятия по виду задачи ===")
ok("разметка — тяжёлая", jobs.lane_of("diarize") == "heavy")
ok("«перечитать точнее» — тяжёлая", jobs.lane_of("retranscribe") == "heavy")
ok("обработка видео — своя дорожка", jobs.lane_of("media") == "media")
# У документов дорожку выбирает не вид задачи, а то, ЧЕМ его считают: по виду
# остаётся осторожное «по одному» на случай, если дорожку не указали вовсе.
ok("документы по умолчанию — по одному",
   jobs.lane_of("minutes") == "docs_one" and jobs.lane_of("summary") == "docs_one",
   str([jobs.lane_of("minutes"), jobs.lane_of("summary")]))
ok("части программы — сеть", jobs.lane_of("needs") == "net" and jobs.lane_of("ytdlp") == "net")
ok("незнакомый вид считается тяжёлым", jobs.lane_of("что-то новое") == "heavy")
ok("тяжёлых всегда по одной", jobs.LANES["heavy"] == 1, str(jobs.LANES))
ok("скачиваний — несколько", jobs.LANES["media"] > 1, str(jobs.LANES))

print("\n=== 2. Разные дорожки идут одновременно ===")
lock = threading.Lock()
started = []
T0 = time.time()


def slow(name, secs=0.5):
    def work(handle):
        with lock:
            started.append(name)
        time.sleep(secs)
        return name
    return work


ids = [
    jobs.submit("diarize", slow("разметка"), "разметка"),
    jobs.submit("minutes", slow("документ"), "документ"),
    jobs.submit("media", slow("скачивание-1"), "скачивание 1"),
    jobs.submit("media", slow("скачивание-2"), "скачивание 2"),
    jobs.submit("needs", slow("часть"), "часть программы"),
]
time.sleep(0.25)
with lock:
    now = sorted(started)
ok("работают сразу: разметка, документ, два скачивания и часть",
   now == ["документ", "разметка", "скачивание-1", "скачивание-2", "часть"], str(now))
ok("все закончились", wait_done(ids))

print("\n=== 3. Две тяжёлые — по очереди ===")
started.clear()
marks = []


def heavy_work(name, secs=0.4):
    def work(handle):
        with lock:
            marks.append(("начал", name, round(time.time() - t0, 2)))
        time.sleep(secs)
        with lock:
            marks.append(("кончил", name, round(time.time() - t0, 2)))
    return work


t0 = time.time()
ids = [jobs.submit("diarize", heavy_work("первая"), "первая"),
       jobs.submit("diarize", heavy_work("вторая"), "вторая")]
ok("обе прошли", wait_done(ids))
order = [m[0] + ":" + m[1] for m in marks]
ok("вторая началась только после первой",
   order == ["начал:первая", "кончил:первая", "начал:вторая", "кончил:вторая"], str(order))

print("\n=== 4. Пропуск на тяжёлый счёт ===")
marks.clear()
t0 = time.time()


def media_with_asr(handle):
    with lock:
        marks.append(("качаю", "", round(time.time() - t0, 2)))
    time.sleep(0.25)
    with handle.heavy():
        with lock:
            marks.append(("распознаю", "", round(time.time() - t0, 2)))
        time.sleep(0.4)


def diarize_now(handle):
    with lock:
        marks.append(("размечаю", "", round(time.time() - t0, 2)))
    time.sleep(0.4)
    with lock:
        marks.append(("разметка готова", "", round(time.time() - t0, 2)))


def just_download(handle):
    with lock:
        marks.append(("второе скачивание", "", round(time.time() - t0, 2)))


ids = [jobs.submit("media", media_with_asr, "видео 1")]
time.sleep(0.1)
ids.append(jobs.submit("diarize", diarize_now, "разметка"))
ids.append(jobs.submit("media", just_download, "видео 2"))
ok("всё прошло", wait_done(ids))
names = [m[0] for m in marks]
at = {m[0]: m[2] for m in marks}
ok("скачивание не ждало разметку",
   names.index("качаю") < names.index("размечаю"), str(names))
ok("второе скачивание пошло, пока идёт разметка",
   "второе скачивание" in names and at["второе скачивание"] < at["разметка готова"],
   str(marks))
ok("распознавание дождалось разметки",
   at["распознаю"] >= at["разметка готова"] - 0.05, str(marks))

print("\n=== 5. Пропуск не залипает после ошибки ===")


def broken(handle):
    with handle.heavy():
        raise RuntimeError("подменный сбой")


bad = jobs.submit("media", broken, "сломанная")
ok("упавшая задача закончилась", wait_done([bad]))
ok("она отмечена ошибкой", (jobs.get(bad) or {}).get("status") == "error",
   str((jobs.get(bad) or {}).get("status")))
after = jobs.submit("diarize", slow("после сбоя", 0.1), "после сбоя")
ok("следующая тяжёлая пошла как ни в чём не бывало", wait_done([after], timeout=4.0))

print("\n=== 6. Порядок внутри дорожки ===")
seq = []


def numbered(n):
    def work(handle):
        with lock:
            seq.append(n)
        time.sleep(0.05)
    return work


ids = [jobs.submit("diarize", numbered(i), "по очереди %d" % i) for i in range(4)]
ok("прошли все", wait_done(ids))
ok("кто первым встал, тот первым и пошёл", seq == [0, 1, 2, 3], str(seq))

print("\n=== 7. Отмена ===")
gate = threading.Event()


def blocker(handle):
    gate.wait(3.0)


busy = jobs.submit("diarize", blocker, "занимаю дорожку")
waiting = jobs.submit("diarize", slow("не должна пойти", 0.1), "ждёт в очереди")
time.sleep(0.15)
ok("вторая ждёт в очереди", (jobs.get(waiting) or {}).get("status") == "queued",
   str((jobs.get(waiting) or {}).get("status")))
ok("отмена принята", jobs.cancel(waiting) is True)
gate.set()
ok("обе закончились", wait_done([busy, waiting]))
ok("отменённая не выполнялась",
   (jobs.get(waiting) or {}).get("status") == "cancelled",
   str((jobs.get(waiting) or {}).get("status")))
ok("вид задачи виден в карточке", (jobs.get(busy) or {}).get("lane") == "heavy",
   str((jobs.get(busy) or {}).get("lane")))

print("\n=== 8. Документы: пачкой только облачные (20.09) ===")
# Решение 20.09: несколько документов разом — только когда считает облако.
# Claude CLI это свой процесс и общая подписка с лимитами, поэтому по одному.
from hagen import minutes  # noqa: E402

ok("облачный сервис — параллельная дорожка", minutes.document_lane("api") == "docs")
ok("Claude CLI — по одному", minutes.document_lane("claude_cli") == "docs_one")
ok("незнакомый движок — по одному", minutes.document_lane("что-то своё") == "docs_one")
ok("облачных документов — несколько сразу", jobs.LANES["docs"] > 1, str(jobs.LANES))
ok("остальных — строго по одному", jobs.LANES["docs_one"] == 1, str(jobs.LANES))

marks.clear()
t0 = time.time()
ids = [jobs.submit("minutes", heavy_work("облако-1", 0.3), "протокол 1",
                   lane=minutes.document_lane("api")),
       jobs.submit("minutes", heavy_work("облако-2", 0.3), "протокол 2",
                   lane=minutes.document_lane("api"))]
ok("оба облачных прошли", wait_done(ids))
pair = [m[1] for m in marks if m[0] == "начал"]
both_at_once = marks[0][0] == "начал" and marks[1][0] == "начал"
ok("два облачных документа пошли разом", both_at_once, str(marks))

marks.clear()
t0 = time.time()
ids = [jobs.submit("minutes", heavy_work("cli-1", 0.3), "протокол 1",
                   lane=minutes.document_lane("claude_cli")),
       jobs.submit("minutes", heavy_work("cli-2", 0.3), "протокол 2",
                   lane=minutes.document_lane("claude_cli"))]
ok("оба прошли", wait_done(ids))
order = [m[0] + ":" + m[1] for m in marks]
ok("документы Claude CLI шли по очереди",
   order == ["начал:cli-1", "кончил:cli-1", "начал:cli-2", "кончил:cli-2"], str(order))

marks.clear()
t0 = time.time()
ids = [jobs.submit("diarize", heavy_work("разметка", 0.3), "разметка"),
       jobs.submit("minutes", heavy_work("документ", 0.3), "документ",
                   lane=minutes.document_lane("claude_cli"))]
ok("оба прошли", wait_done(ids))
ok("документ Claude CLI не ждёт разметку — это разные дорожки",
   marks[0][0] == "начал" and marks[1][0] == "начал", str(marks))

print("\nВсего провалов: %d" % len(FAIL))
if FAIL:
    for f in FAIL:
        print("  - %s" % f)
sys.exit(1 if FAIL else 0)
