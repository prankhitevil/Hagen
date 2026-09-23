# -*- coding: utf-8 -*-
"""Проверка 107: разметка отдельной программой с низким приоритетом (решения 22.09).

Разметка длинной встречи делила процессор с живой записью звонка. Теперь по
умолчанию её считает помощник — отдельная программа с приоритетом «ниже
среднего», а пока идёт запись, ещё и в режиме эффективности. Остальная
тяжёлая работа во время записи встаёт на паузу.

Что проверяем:
  1. розетка приоритета: Windows на самом деле меняет класс приоритета
     дочерней программы на ходу; заглушка записывает просьбы;
  2. помощник на настоящей записи: разметка та же, что в самой программе;
     ход доходит до задачи; приоритет «ниже среднего», пока записи нет, и
     режим эффективности, пока она идёт;
  3. отмена задачи останавливает помощника;
  4. помощник не запустился — разметка идёт в самой программе, об этом
     сказано; задача снова считается «здесь»;
  5. очередь: разметка в помощнике начинается и идёт во время записи, а как
     только счёт вернулся в программу — уступает записи;
  6. сквозь очередь: разметка записи идёт помощником и сохраняется;
  7. настройка: по умолчанию помощник, три варианта на странице.

Запуск из корня проекта:  .venv\\Scripts\\python.exe tests\\t107_diarize_helper.py
"""
import io
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
sys.path.insert(0, str(PROJECT / "tests"))
import isolate  # noqa: E402
from harness import LINES, FAIL, say, check, finish  # noqa: E402

isolate.voices()
TMP = Path(tempfile.mkdtemp(prefix="t107_"))
isolate.settings(vault_path=str(TMP / "vault"))


import psutil  # noqa: E402

from hagen import audio_io, config, diarize, diarize_worker, jobs, platform, recordings, store  # noqa: E402
from hagen.platform import fake  # noqa: E402

WAV = PROJECT / "tests" / "meeting.wav"


class Handle:
    """Задача очереди глазами разметки: запоминает всё, что ей сообщили."""

    def __init__(self):
        self.values, self.notes, self.logs, self.etas = [], [], [], []
        self.cancelled = False
        self.paused_s = 0.0
        self.on_progress = None

    def progress(self, value, note=""):
        self.values.append(float(value))
        self.notes.append(note)
        if self.on_progress:
            self.on_progress(self)

    def eta(self, seconds):
        self.etas.append(seconds)

    def log(self, msg):
        self.logs.append(str(msg))


def helper_children():
    """Живые помощники разметки этой проверки."""
    out = []
    for p in psutil.Process().children(recursive=True):
        try:
            if "hagen.diarize_worker" in " ".join(p.cmdline()):
                out.append(p)
        except psutil.Error:
            continue
    return out


try:
    say("=== 1. Розетка приоритета ===")
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                             creationflags=platform.system().hidden_process_flags())
    try:
        proc = psutil.Process(child.pid)
        got = {}
        for level in ("low", "lowest", "normal"):
            ok = platform.system().set_process_priority(child.pid, level)
            got[level] = (ok, proc.nice())
        check("«ниже среднего»", got["low"] == (True, psutil.BELOW_NORMAL_PRIORITY_CLASS), got["low"])
        check("режим эффективности — низший класс", got["lowest"] == (True, psutil.IDLE_PRIORITY_CLASS),
              got["lowest"])
        check("и обратно — как всем", got["normal"] == (True, psutil.NORMAL_PRIORITY_CLASS), got["normal"])
        try:
            platform.system().set_process_priority(child.pid, "быстро")
            check("незнакомый уровень — ошибка", False)
        except ValueError:
            check("незнакомый уровень — ошибка", True)
    finally:
        child.kill()
        child.wait(5)
    platform.use("fake")
    fake.reset()
    check("заглушка записывает просьбы", platform.system().set_process_priority(7, "low")
          and fake.system.PRIORITIES == [(7, "low")], fake.system.PRIORITIES)
    platform.use(None)

    say("")
    say("=== 2. Помощник на настоящей записи ===")
    pcm, sr = audio_io.read_wav(WAV)
    here = diarize.diarize_pcm(pcm, sr=sr)
    h = Handle()
    busy = {"on": False}
    seen = []

    def watch(handle):
        kids = helper_children()
        if kids:
            try:
                seen.append((busy["on"], kids[0].nice()))
            except psutil.Error:
                pass
        if len(handle.values) == 3:
            busy["on"] = True          # началась запись
    h.on_progress = watch
    t0 = time.time()
    res = diarize_worker.run(WAV, handle=h, busy=lambda: busy["on"])
    say("   помощник разметил за %.1f c" % (time.time() - t0))
    check("разметка та же, что в самой программе: голоса",
          res.get("labels") == here.get("labels"), (res.get("labels"), here.get("labels")))
    same = len(res.get("turns") or []) == len(here.get("turns") or []) and all(
        a["speaker"] == b["speaker"] and abs(a["start"] - b["start"]) < 0.05
        and abs(a["end"] - b["end"]) < 0.05
        for a, b in zip(res.get("turns") or [], here.get("turns") or []))
    check("и интервалы", same, (len(res.get("turns") or []), len(here.get("turns") or [])))
    check("отпечатки голосов доехали", set(res.get("embeddings") or {}) == set(here.get("embeddings") or {}),
          list(res.get("embeddings") or {}))
    check("ход дошёл до задачи и не откатывался",
          len(h.values) > 5 and all(b >= a for a, b in zip(h.values, h.values[1:])) and h.values[-1] > 0.9,
          (len(h.values), h.values[-1:] if h.values else None))
    check("подписи шагов — по-русски", "поиск речи" in h.notes, sorted(set(h.notes)))
    before = [n for b, n in seen if not b]
    during = [n for b, n in seen[2:] if b]      # смене нужно полсекунды
    check("пока записи нет — «ниже среднего»",
          before and all(n == psutil.BELOW_NORMAL_PRIORITY_CLASS for n in before), before[:5])
    check("пока идёт запись — режим эффективности",
          during and during[-1] == psutil.IDLE_PRIORITY_CLASS, during[-5:])
    time.sleep(0.5)
    check("помощник закрылся", not helper_children(), helper_children())

    say("")
    say("=== 3. Отмена ===")
    h = Handle()

    def cancel_early(handle):
        if len(handle.values) >= 2:
            handle.cancelled = True
    h.on_progress = cancel_early
    t0 = time.time()
    try:
        diarize_worker.run(WAV, handle=h, busy=lambda: False)
        check("отмена остановила помощника", False, "разметка дошла до конца")
    except diarize.DiarizeCancelled:
        check("отмена остановила помощника", True)
    check("быстро: %.1f c" % (time.time() - t0), time.time() - t0 < 60)
    time.sleep(0.5)
    check("и помощник закрылся", not helper_children(), helper_children())

    say("")
    say("=== 4. Помощник не запустился ===")
    real_popen = diarize_worker.subprocess.Popen

    def no_start(*a, **kw):
        raise PermissionError("[WinError 5] Отказано в доступе")

    class JobLike(Handle):
        where = "helper"

    h = JobLike()
    diarize_worker.subprocess.Popen = no_start
    try:
        res = diarize.diarize_track(WAV, handle=h, isolated=True)
    finally:
        diarize_worker.subprocess.Popen = real_popen
    check("разметка всё равно сделана — в самой программе",
          res.get("labels") == here.get("labels"), res.get("labels"))
    check("об этом сказано", any("не запустился" in m for m in h.logs), h.logs)
    check("задача снова считается «здесь» — во время записи встанет на паузу", h.where == "here", h.where)

    say("")
    say("=== 5. Очередь: помощнику уступать нечего ===")
    rule = {"reason": "идёт запись"}
    real_rule, real_poll = jobs._yield_rule, jobs.YIELD_POLL_S
    jobs.set_yield_rule(lambda: rule["reason"])
    jobs.YIELD_POLL_S = 0.1
    marks = []

    def work(handle):
        for i in range(4):
            handle.progress(i / 8.0, "в помощнике")
            marks.append(("helper", time.time()))
        handle.where = "here"
        handle.progress(0.9, "сохраняю")
        marks.append(("here", time.time()))
        return {}

    def state(job_id):
        return next((j for j in jobs.list_all() if j.get("id") == job_id), {})

    try:
        jid = jobs.submit("diarize", work, "Проверка 107", extra={"where": "helper"})
        end = time.time() + 5
        while time.time() < end and not state(jid).get("paused"):
            time.sleep(0.05)
        check("во время записи разметка в помощнике началась и дошла до своей части",
              [m for m, _ in marks] == ["helper"] * 4, marks)
        check("а вернувшись в программу — встала на паузу",
              state(jid).get("paused") is True and state(jid).get("note") == "на паузе, пока идёт запись",
              state(jid).get("note"))
        rule["reason"] = ""
        end = time.time() + 5
        while time.time() < end and state(jid).get("status") != "done":
            time.sleep(0.05)
        check("запись кончилась — дошла до конца", state(jid).get("status") == "done", state(jid).get("status"))
    finally:
        jobs.set_yield_rule(real_rule)
        jobs.YIELD_POLL_S = real_poll

    say("")
    say("=== 6. Сквозь очередь ===")
    from hagen import diarize_jobs  # noqa: E402

    meta = store.create(title="Проверка 107", mode="online", source="live", category="Встречи")
    rid = meta["id"]
    try:
        shutil.copyfile(WAV, store.track_path(rid, store.TRACK_FAR))
        store.replace_segments(rid, [store.make_segment("far", 1.0, 5.0, "Добрый день.")])
        store.update(rid, {"status": "recorded", "duration_s": audio_io.wav_duration(WAV),
                           "tracks": [store.TRACK_FAR]})
        check("по умолчанию — помощник", config.get("processing_during_recording") == "background"
              and diarize.runs_in_helper(), config.get("processing_during_recording"))
        jid = diarize_jobs.queue_diarize(rid)
        check("задача разметки ставится в помощника", state(jid).get("where") == "helper", state(jid).get("where"))
        end = time.time() + 240
        while time.time() < end and state(jid).get("status") in ("queued", "running"):
            time.sleep(0.5)
        st = state(jid)
        check("разметка сохранена", st.get("status") == "done" and (store.get(rid) or {}).get("diarized"),
              (st.get("status"), st.get("note")))
        check("счёт шёл в помощнике, сохранение — в программе", st.get("where") == "here", st.get("where"))
    finally:
        store.delete(rid)

    say("")
    say("=== 7. Настройка ===")
    check("по умолчанию — помощник", config.DEFAULTS.get("processing_during_recording") == "background")
    from hagen import server  # noqa: E402

    real_active, real_get = recordings.sessions.active_session, config.get
    choice = {"v": "background"}
    config.get = lambda k, d=None: choice["v"] if k == "processing_during_recording" else real_get(k, d)
    try:
        recordings.sessions.active_session = lambda: object()
        check("помощник: остальное во время записи уступает, помощник — в режим эффективности",
              server._live_yield_reason() == "идёт запись", server._live_yield_reason())
        choice["v"] = "run"
        check("«не останавливать» — никто не уступает", server._live_yield_reason() == "")
        choice["v"] = "pause"
        check("«пауза» — помощника нет", not diarize.runs_in_helper())
    finally:
        recordings.sessions.active_session = real_active
        config.get = real_get
    html = io.open(PROJECT / "hagen" / "static" / "index.html", encoding="utf-8").read()
    js = io.open(PROJECT / "hagen" / "static" / "app.js", encoding="utf-8").read()
    check("на странице три варианта, помощник первым",
          html.find('value="background"') < html.find('value="pause"') < html.find('value="run"')
          and html.find('value="background"') > 0)
    check("пустая настройка на странице — помощник", ": 'background';" in js)
finally:
    shutil.rmtree(TMP, ignore_errors=True)

sys.exit(finish("t107"))
