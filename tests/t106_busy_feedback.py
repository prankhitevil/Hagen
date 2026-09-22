# -*- coding: utf-8 -*-
"""Проверка 106: пока запись занята, человек это видит.

На полуторачасовой встрече разметка говорящих две минуты ищет речь по всей
записи, и всё это время полоска стояла на нуле — казалось, что разметка
зависла. А «+ Документ», выключенная на время разметки, выглядела как обычная
и молча не нажималась.

Что проверяем (модель разметки не грузится: поиск речи подставной):
  1. поиск речи двигает полоску, передаёт ход раз на процент, а не на каждое
     окно в 32 мс, и занимает своё место в начале шкалы;
  2. шаги самой разметки продолжают шкалу, а не начинают её с нуля;
  3. отмену во время поиска речи не глотает запасной путь «размечаю целиком»;
  4. страница: выключенная вкладка выглядит выключенной, у «+ Документ» —
     подсказка, чем занята запись;
  5. карточка записи сохраняется, даже если в этот миг её кто-то читает
     (22.09 под нагрузкой Windows отказала в подмене файла, и остановка
     звонка не записала ни карточку, ни заметку);
  6. пока идёт запись, тяжёлый счёт уступает ей (решение 22.09): начатая
     задача встаёт на ближайшем отчёте о ходе и продолжает сама, новая не
     начинается, отмена на паузе работает, документы паузу не ждут, время
     паузы в оценку остатка не входит; правило ставит служба, и его можно
     выключить в «Основном» («не останавливать»).

Запуск из корня проекта:  .venv\\Scripts\\python.exe tests\\t106_busy_feedback.py
"""
import io
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

LINES = []
FAIL = []


def say(msg=""):
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


import numpy as np  # noqa: E402

from hagen import diarize, vad  # noqa: E402


class Handle:
    """Задача очереди: запоминает, что ей сообщили."""

    def __init__(self, cancel_at=None):
        self.values = []
        self.notes = []
        self.cancel_at = cancel_at
        self.cancelled = False

    def progress(self, value, note=""):
        self.values.append(float(value))
        self.notes.append(note)

    def eta(self, seconds):
        pass

    def log(self, msg):
        pass


SHARE = dict((n, w) for n, w, _ in diarize._STEPS)["speech"] / sum(w for _, w, _ in diarize._STEPS)
WINDOWS = 20000               # как у поиска речи: ход на каждое окно
real_ts = vad.speech_timestamps
asked = {}


def fake_ts(pcm, progress=None, handle=None, **_kw):
    asked["progress"] = progress
    for i in range(1, WINDOWS + 1):
        if progress is not None:
            progress(100.0 * i / WINDOWS)
        if handle is not None and handle.cancel_at is not None and 100.0 * i / WINDOWS >= handle.cancel_at:
            handle.cancelled = True
    return []                 # речи нет — разметка на этом и кончается, модель не нужна


pcm = np.zeros(60 * diarize.SR, dtype=np.float32)
try:
    say("=== 1. Поиск речи двигает полоску ===")
    h = Handle()
    vad.speech_timestamps = lambda arr, **kw: fake_ts(arr, **kw)
    diarize.diarize_pcm(pcm, handle=h)
    check("поиску речи передан ход", asked.get("progress") is not None)
    check("ход передаётся раз на процент, а не на каждое окно", 90 <= len(h.values) <= 105, len(h.values))
    check("полоска не откатывается", all(b >= a for a, b in zip(h.values, h.values[1:])))
    check("поиск речи — своя доля в начале шкалы (%.0f %%)" % (SHARE * 100),
          abs(h.values[-1] - SHARE) < 1e-6, (h.values[-1], SHARE))
    check("подпись — «поиск речи»", set(h.notes) == {"поиск речи"}, set(h.notes))
    check("доля правдоподобная: 10–25 %", 0.10 <= SHARE <= 0.25, SHARE)

    say("")
    say("=== 2. Разметка продолжает шкалу ===")
    h = Handle()
    hook = diarize._Progress(h, 0.0)
    hook("speech", completed=100, total=100)
    hook("segmentation", completed=0, total=10)
    check("начало разметки — там, где кончился поиск речи", abs(h.values[-1] - SHARE) < 1e-6,
          (h.values[-1], SHARE))
    hook("segmentation", completed=5, total=10)
    check("и идёт дальше", h.values[-1] > SHARE, h.values[-1])
    hook("discrete_diarization", completed=1, total=1)
    check("до конца не доходит, пока разметка не сохранена", h.values[-1] < 1.0, h.values[-1])

    say("")
    say("=== 3. Отмена во время поиска речи ===")
    h = Handle(cancel_at=40)
    vad.speech_timestamps = lambda arr, **kw: fake_ts(arr, handle=h, **kw)
    try:
        diarize.diarize_pcm(pcm, handle=h)
        check("отмена прервала разметку", False, "дошла до конца")
    except diarize.DiarizeCancelled:
        check("отмена прервала разметку", True)
    check("и не дошла до конца поиска речи", h.values and h.values[-1] < SHARE, h.values[-1:])
finally:
    vad.speech_timestamps = real_ts

say("")
say("=== 4. Страница ===")
css = io.open(PROJECT / "hagen" / "static" / "app.css", encoding="utf-8").read()
js = io.open(PROJECT / "hagen" / "static" / "app.js", encoding="utf-8").read()
check("выключенная вкладка выглядит выключенной и не подсвечивается",
      ".rtab:disabled,.rtab:disabled:hover{" in css)
check("у выключенной «+ Документ» — подсказка, чем занята запись",
      "Запись сейчас занята" in js and "addDoc.dataset.title" in js)

say("")
say("=== 5. Запись карточки, пока её читают ===")
import json  # noqa: E402
import shutil  # noqa: E402
import tempfile  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402

from hagen import config  # noqa: E402

tmp = Path(tempfile.mkdtemp(prefix="t106_"))
try:
    card = tmp / "meta.json"
    config.atomic_json(card, {"status": "recording"})
    opened = threading.Event()

    def reader(hold_s):
        # Так читает хранилище: обычный open, файл держится открытым.
        with io.open(card, "r", encoding="utf-8") as fh:
            opened.set()
            time.sleep(hold_s)
            fh.read()

    t = threading.Thread(target=reader, args=(0.4,))
    t.start()
    opened.wait(5)
    t0 = time.monotonic()
    try:
        config.atomic_json(card, {"status": "recorded"})
        err = None
    except OSError as e:
        err = e
    waited = time.monotonic() - t0
    t.join()
    check("карточка записана, хоть её и читали", err is None
          and json.load(io.open(card, encoding="utf-8")).get("status") == "recorded", err)
    check("ждали, пока чтение кончится, а не сдались сразу", waited >= 0.2, "%.2f с" % waited)
    check("временного файла не осталось", not (tmp / "meta.json.tmp").exists())

    opened.clear()
    t = threading.Thread(target=reader, args=(3.0,))
    t.start()
    opened.wait(5)
    try:
        config.atomic_json(card, {"status": "never"})
        err = None
    except PermissionError as e:
        err = e
    t.join()
    check("если файл не отпускают дольше двух секунд — честная ошибка, а не вечное ожидание",
          isinstance(err, PermissionError), err)
finally:
    shutil.rmtree(tmp, ignore_errors=True)

say("")
say("=== 6. Пока идёт запись, тяжёлый счёт уступает ===")
from hagen import jobs  # noqa: E402

rule = {"reason": ""}
real_rule = jobs._yield_rule
real_poll = jobs.YIELD_POLL_S
jobs.set_yield_rule(lambda: rule["reason"])
jobs.YIELD_POLL_S = 0.1


def job_state(job_id):
    return next((j for j in jobs.list_all() if j.get("id") == job_id), {})


def wait_for(pred, timeout=10.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.05)
    return False


try:
    steps = []
    handles = {}

    def work(handle, n=12):
        handles["h"] = handle
        for i in range(1, n + 1):
            if handle.cancelled:
                return {"stopped": i}
            time.sleep(0.05)
            steps.append(time.time())
            handle.progress(i / float(n), "считаю")
        return {"done": n}

    jid = jobs.submit("diarize", work, "Проверка 106: разметка")
    wait_for(lambda: len(steps) >= 3)
    rule["reason"] = "идёт запись"
    wait_for(lambda: job_state(jid).get("paused"), 3)
    st = job_state(jid)
    check("начатая задача встала на паузу", st.get("paused") is True, st.get("note"))
    check("причина видна в пометке задачи", st.get("note") == "на паузе, пока идёт запись", st.get("note"))
    check("остаток на паузе не показывается", st.get("eta_s") is None, st.get("eta_s"))
    n_at_pause = len(steps)
    time.sleep(0.8)
    check("на паузе задача не считает", len(steps) == n_at_pause, (n_at_pause, len(steps)))
    check("и не закончилась", job_state(jid).get("status") == "running", job_state(jid).get("status"))
    rule["reason"] = ""
    wait_for(lambda: job_state(jid).get("status") == "done")
    st = job_state(jid)
    check("запись кончилась — задача продолжила сама и дошла до конца",
          st.get("status") == "done" and len(steps) == 12, (st.get("status"), len(steps)))
    check("пометка «на паузе» снята", not st.get("paused"), st.get("paused"))
    check("время паузы задача помнит", 0.7 <= handles["h"].paused_s <= 3.0, handles["h"].paused_s)

    say("")
    started = []
    rule["reason"] = "идёт запись"
    jid = jobs.submit("retranscribe", lambda h: started.append(time.time()) or {"ok": 1},
                      "Проверка 106: перечитать")
    time.sleep(0.8)
    check("новая тяжёлая задача во время записи не начинается", not started)
    check("и честно пишет, чего ждёт", job_state(jid).get("note") == "на паузе, пока идёт запись",
          job_state(jid).get("note"))
    docs = []
    djid = jobs.submit("minutes", lambda h: docs.append(1) or {"ok": 1}, "Проверка 106: протокол",
                       lane="docs")
    wait_for(lambda: job_state(djid).get("status") == "done", 3)
    check("документ паузу не ждёт: считает не наш процессор", docs == [1], job_state(djid).get("status"))
    rule["reason"] = ""
    wait_for(lambda: job_state(jid).get("status") == "done")
    check("после записи отложенная задача пошла", started and job_state(jid).get("status") == "done",
          job_state(jid).get("status"))

    say("")
    steps.clear()
    jid = jobs.submit("diarize", work, "Проверка 106: отмена на паузе")
    wait_for(lambda: len(steps) >= 2)
    rule["reason"] = "идёт запись"
    wait_for(lambda: job_state(jid).get("paused"), 3)
    jobs.cancel(jid)
    ok = wait_for(lambda: job_state(jid).get("status") == "cancelled", 3)
    check("отмена на паузе срабатывает, не дожидаясь конца записи", ok, job_state(jid).get("status"))
    rule["reason"] = ""

    say("")
    h = Handle()
    h.paused_s = 100.0
    etas = []
    h.eta = etas.append
    hook = diarize._Progress(h, time.time() - 110.0)
    hook("segmentation", completed=5, total=10)
    value = h.values[-1]
    check("время паузы в оценку остатка не входит",
          etas and abs(etas[-1] - (10.0 / value - 10.0)) < 1.0, (etas[-1:], value))

    say("")
    from hagen import server  # noqa: E402

    check("правило ставит служба", jobs._yield_rule is server._live_yield_reason)
    # С 22.09 по умолчанию разметку считает помощник (t107), остальная тяжёлая
    # работа во время записи встаёт на паузу так же.
    check("по умолчанию тяжёлая работа уступает записи",
          config.DEFAULTS.get("processing_during_recording") in ("pause", "background"),
          config.DEFAULTS.get("processing_during_recording"))
    real_active = server.sessions.active_session
    real_get = config.get
    choice = {"v": "pause"}
    config.get = lambda k, d=None: choice["v"] if k == "processing_during_recording" else real_get(k, d)
    try:
        server.sessions.active_session = lambda: object()
        check("идёт запись — служба велит уступать", server._live_yield_reason() == "идёт запись",
              server._live_yield_reason())
        choice["v"] = "run"
        check("выбрано «не останавливать» — не уступает и при записи", server._live_yield_reason() == "",
              server._live_yield_reason())
        choice["v"] = "pause"
        server.sessions.active_session = lambda: None
        check("записи нет — уступать некому", server._live_yield_reason() == "", server._live_yield_reason())
    finally:
        server.sessions.active_session = real_active
        config.get = real_get
finally:
    rule["reason"] = ""
    jobs.YIELD_POLL_S = real_poll
    jobs.set_yield_rule(real_rule)

check("страница показывает паузу и в строке записи, и в «В работе»",
      "if (j.paused)" in js and "!j.paused" in js)
html = io.open(PROJECT / "hagen" / "static" / "index.html", encoding="utf-8").read()
check("выбор в «Основном»: пауза или не останавливать",
      'id="set-busy-rec"' in html and 'value="pause"' in html and 'value="run"' in html
      and "patch.processing_during_recording = $('set-busy-rec').value" in js)

say("")
say("ИТОГО провалов: %d" % len(FAIL))
for f in FAIL:
    say("   - " + f)
io.open(PROJECT / "tests" / "t106_result.txt", "w", encoding="utf-8").write("\n".join(LINES))
sys.exit(1 if FAIL else 0)
