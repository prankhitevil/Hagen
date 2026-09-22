# -*- coding: utf-8 -*-
"""Проверка 43: определение звонка и автоматика «звонок → запись».

Живой звонок 12.09: предложение записать приходило поздно, а звонок шесть раз
«заканчивался» и «начинался» — признаком был СЛЫШИМЫЙ звук из Teams, и паузы
собеседника (у него отвалился микрофон) читались как конец звонка.

Что проверяем:
  1. Признак звонка: программа держит микрофон и вывод звука, громкость не
     важна; Telegram и браузер без вывода звука звонком не считаются.
  2. Наблюдатель на подменённых часах: минута тишины в звонке его не рвёт,
     старт — через 3 с, конец — через 15 с после освобождения микрофона.
  3. Автоматика на подменённой службе: «Не писать», «Остановить?» с остановкой
     без ответа, звонок вернулся во время вопроса, «Дописать в прошлую».
  4. Склейка двух записей: звук, время реплик, отметки перерывов.
  5. Сквозь настоящую службу (тестовый клиент в этом же процессе, настоящие
     устройства): звонок → запись → конец → ответ «Остановить» кнопкой окна;
     второй звонок → «Дописать в прошлую» → запись продолжается в прошлой.

Запуск из корня проекта:  .venv\\Scripts\\python.exe tests\\t43_call_automation.py
Сквозную часть можно пропустить ключом --no-live.
"""
import io
import sys
import threading
import time
import wave
from pathlib import Path

import numpy as np

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
import isolate  # noqa: E402  настоящая база голосов не трогается

isolate.voices()

LINES = []
FAIL = []
SR = 16000


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
    say(("   ok    " if ok else "   ПЛОХО ") + name + (": " + str(detail) if detail != "" else ""))


from hagen import audio_io, calls, config, store  # noqa: E402
from hagen.platform.windows import desktop  # noqa: E402

OVERRIDES = {}
_real_get = config.get


def fake_get(key, default=None):
    if key in OVERRIDES:
        return OVERRIDES[key]
    return _real_get(key, default)


config.get = fake_get


def sess(proc, state=1, peak=0.0):
    return {"process": proc, "pid": 100, "state": state, "peak": peak, "device": "x"}


# ------------------------------------------------------------ 1. признак звонка
say("=== 1. Признак звонка ===")
RENDER, CAPTURE = [], []
desktop.list_sessions = lambda: list(RENDER)
desktop.list_capture_sessions = lambda: list(CAPTURE)
OVERRIDES["call_watch_require_mic"] = True


def case(name, render, capture, want, proc=None):
    RENDER[:] = render
    CAPTURE[:] = capture
    got = desktop.detect_call()
    ok = bool(got["active"]) == want and (proc is None or got["process"] == proc)
    check(name, ok, "active=%s process=%s" % (got["active"], got["process"]))


case("Teams: микрофон и вывод, собеседник МОЛЧИТ", [sess("ms-teams.exe", peak=0.0)],
     [sess("ms-teams.exe")], True, "ms-teams.exe")
case("Teams: микрофон и вывод, собеседник говорит", [sess("ms-teams.exe", peak=0.3)],
     [sess("ms-teams.exe")], True)
case("Teams: только вывод (звонок входящий, не принят)", [sess("ms-teams.exe", peak=0.3)],
     [], False)
case("Teams: только микрофон", [], [sess("ms-teams.exe")], True)
case("Teams: сессии простаивают", [sess("ms-teams.exe", state=0)],
     [sess("ms-teams.exe", state=0)], False)
case("Telegram: голосовое сообщение (только микрофон)", [], [sess("Telegram.exe")], False)
case("Telegram: звонок (микрофон и вывод)", [sess("Telegram.exe")], [sess("Telegram.exe")], True)
case("браузер: голосовой ввод (только микрофон)", [], [sess("chrome.exe")], False)
case("браузер: звонок (микрофон и вывод)", [sess("chrome.exe")], [sess("chrome.exe")], True)
case("YouTube в браузере без микрофона", [sess("chrome.exe", peak=0.5)], [], False)
case("микрофон держит сам Hagen", [sess("python.exe", peak=0.2)], [sess("python.exe")], False)
case("никого", [], [], False)
OVERRIDES["call_watch_require_mic"] = False
case("без требования микрофона: слышимый звук Teams", [sess("ms-teams.exe", peak=0.2)], [], True)
case("без требования микрофона: тишина Teams", [sess("ms-teams.exe", peak=0.0)], [], False)
OVERRIDES["call_watch_require_mic"] = True

# ------------------------------------------------------------ 2. наблюдатель
say("")
say("=== 2. Наблюдатель на подменённых часах ===")


class Clock:
    now = 1000.0

    def time(self):
        return self.now


clock = Clock()
real_time = desktop.time
desktop.time = clock
OVERRIDES["call_watch_debounce_s"] = 3
OVERRIDES["call_watch_enabled"] = True
events = []
w = desktop.CallWatcher(on_call_start=lambda i: events.append(("start", clock.now)),
                           on_call_end=lambda i: events.append(("end", clock.now)))
RENDER[:] = [sess("ms-teams.exe", peak=0.0)]
CAPTURE[:] = [sess("ms-teams.exe")]
t0 = clock.now
for _ in range(40):          # 80 с звонка, собеседник всё время молчит
    w._tick()
    clock.now += 2.0
starts = [e for e in events if e[0] == "start"]
ends = [e for e in events if e[0] == "end"]
check("звонок замечен один раз", len(starts) == 1, events)
check("замечен быстро (≤ 4 с)", bool(starts) and starts[0][1] - t0 <= 4.0,
      starts and "%.0f с" % (starts[0][1] - t0))
check("тишина собеседника звонок не рвёт", not ends, ends)
RENDER[:] = []
CAPTURE[:] = []
t_hang = clock.now
for _ in range(12):
    w._tick()
    clock.now += 2.0
ends = [e for e in events if e[0] == "end"]
check("конец звонка замечен", len(ends) == 1, events)
# последний опрос со звонком был за один шаг (2 с) до t_hang
check("конец через 15 с после последнего опроса со звонком (±2 с)",
      bool(ends) and 13.0 <= ends[0][1] - t_hang <= 17.0, ends and "%.0f с" % (ends[0][1] - t_hang))
desktop.time = real_time

# ------------------------------------------------------------ 3. автоматика
say("")
say("=== 3. Автоматика на подменённой службе ===")
OVERRIDES["call_watch_autostart"] = True
OVERRIDES["call_end_confirm_s"] = 1
OVERRIDES["call_resume_window_s"] = 600


class FakeService:
    def __init__(self):
        self.active = None
        self.log = []
        self.events = []
        self.created = []

    def start(self, meeting):
        meta = store.create(title="Проверка 43 — звонок %d" % (len(self.created) + 1),
                            mode="online", source="live")
        self.created.append(meta["id"])
        self.active = meta["id"]
        self.log.append(("start", meta["id"]))
        return meta["id"]

    def stop(self, rec_id):
        self.log.append(("stop", rec_id))
        self.active = None
        auto.recording_stopped(rec_id)

    def discard(self, rec_id):
        self.log.append(("discard", rec_id))
        self.active = None

    def merge(self, src, dst):
        self.log.append(("merge", src, dst))
        self.active = dst


svc = FakeService()
auto = calls.CallAutomation(calls.Hooks(
    active_recording=lambda: svc.active,
    start_recording=svc.start,
    stop_recording=svc.stop,
    discard_recording=svc.discard,
    merge_and_resume=svc.merge,
    publish=svc.events.append,
))
shown = []
auto.add_listener(lambda p: shown.append(p))

auto.on_call_start({"process": "ms-teams.exe"}, None)
check("звонок начался — запись пошла сама", svc.log[:1] and svc.log[0][0] == "start", svc.log)
pr = auto.prompt
check("в уведомлении кнопка «Не писать»", pr and [b["label"] for b in pr["buttons"]] == ["Не писать"],
      pr and pr["buttons"])
check("слушатель уведомлений получил вопрос", shown and shown[-1] and shown[-1]["kind"] == "call_started")
res = auto.answer(pr["id"], "discard")
check("«Не писать» снимает запись", res.get("ok") and svc.log[-1][0] == "discard", (res, svc.log))
check("после ответа вопрос снят", auto.prompt is None and shown[-1] is None)
check("чужой ответ на снятый вопрос не проходит", auto.answer(pr["id"], "discard").get("ok") is False)

# конец звонка без ответа
svc.log.clear()
auto.on_call_start({"process": "ms-teams.exe"}, None)
rec1 = svc.active
auto.on_call_end({"process": "ms-teams.exe"})
pr = auto.prompt
check("конец звонка — вопрос «Остановить?»", pr and pr["kind"] == "call_ended", pr and pr["kind"])
check("кнопки «Остановить» и «Писать дальше»",
      pr and [b["id"] for b in pr["buttons"]] == ["stop", "keep"])
time.sleep(1.6)
check("без ответа запись остановилась сама", ("stop", rec1) in svc.log, svc.log)
check("вопрос снят", auto.prompt is None)

# звонок вернулся, пока висел вопрос
svc.log.clear()
svc.active = rec1
auto.linked_rec = rec1
auto.on_call_end({"process": "ms-teams.exe"})
check("вопрос задан", auto.prompt is not None and auto.prompt["kind"] == "call_ended")
auto.on_call_start({"process": "ms-teams.exe"}, None)
time.sleep(1.4)
check("звонок вернулся — вопрос снят, запись не остановлена",
      auto.prompt is None and not any(e[0] == "stop" for e in svc.log), svc.log)
check("новой записи не заведено", not any(e[0] == "start" for e in svc.log), svc.log)

# «Писать дальше»
auto.on_call_end({})
res = auto.answer(auto.prompt["id"], "keep")
time.sleep(1.4)
check("«Писать дальше» — запись идёт", res.get("ok") and svc.active == rec1 and
      not any(e[0] == "stop" for e in svc.log), svc.log)

# новый звонок вскоре после записи
svc.stop(rec1)
svc.log.clear()
auto.on_call_start({"process": "ms-teams.exe"}, {"subject": "Планёрка"})
rec2 = svc.active
pr = auto.prompt
check("новый звонок вскоре после записи — запись всё равно пошла сразу",
      svc.log and svc.log[0][0] == "start" and rec2 != rec1, svc.log)
check("вопрос «дописать в прошлую или оставить новой»",
      pr and pr["kind"] == "call_resumed" and pr.get("prev_id") == rec1, pr)
res = auto.answer(pr["id"], "merge")
check("«Дописать в прошлую» склеивает новую запись с прошлой",
      res.get("ok") and ("merge", rec2, rec1) in svc.log, svc.log)
check("дальше запись идёт в прошлую заметку", auto.linked_rec == rec1 and svc.active == rec1)

# давний звонок вопроса о склейке не вызывает
svc.stop(rec1)
auto.last_stopped["at"] -= 3600
svc.log.clear()
auto.on_call_start({}, None)
check("звонок через час после записи — без вопроса о склейке",
      auto.prompt is not None and auto.prompt["kind"] == "call_started", auto.prompt and auto.prompt["kind"])

# оба срока — из «Настройки → Запись и звонки» (решение 15.09)
OVERRIDES["call_end_confirm_s"] = 7
auto.linked_rec = svc.active
auto.on_call_end({"process": "ms-teams.exe"})
pr = auto.prompt
check("срок остановки из настроек: в вопросе «через 7 с» и таймер на 7 с",
      pr and "через 7 с" in pr["text"] and pr.get("timeout_s") == 7, pr and (pr["text"], pr.get("timeout_s")))
if pr:
    auto.answer(pr["id"], "keep")
OVERRIDES["call_end_confirm_s"] = 1
OVERRIDES["call_resume_window_s"] = 60
for ago, want in ((90, "call_started"), (40, "call_resumed")):
    svc.stop(svc.active)
    auto.last_stopped["at"] = time.time() - ago
    auto.on_call_start({}, None)
    check("окно «дописать» 60 с из настроек: звонок через %d с — %s" % (ago, want),
          auto.prompt is not None and auto.prompt["kind"] == want, auto.prompt and auto.prompt["kind"])
    auto.answer(auto.prompt["id"], "new" if want == "call_resumed" else "ok") if auto.prompt else None
OVERRIDES["call_resume_window_s"] = 600
for rid in svc.created:
    store.delete(rid)

# ------------------------------------------------------------ 4. склейка
say("")
say("=== 4. Склейка записей ===")


def tone(sec, hz):
    t = np.arange(int(sec * SR)) / SR
    return (0.2 * np.sin(2 * np.pi * hz * t)).astype(np.float32)


dst = store.create(title="Проверка 43 — прошлая", mode="online", source="live")["id"]
src = store.create(title="Проверка 43 — новая", mode="online", source="live")["id"]
audio_io.write_wav(store.track_path(dst, "mic"), tone(4.0, 300))
audio_io.write_wav(store.track_path(dst, "far"), tone(3.0, 500))
store.replace_segments(dst, [store.make_segment("mic", 0.0, 2.0, "прошлая реплика")])
store.update(dst, {"tracks": ["far", "mic"], "capture_gaps": [{"track": "far", "at_s": 1.0, "gap_s": 0.5}]})
audio_io.write_wav(store.track_path(src, "mic"), tone(2.0, 700))
audio_io.write_wav(store.track_path(src, "far"), tone(2.0, 900))
store.replace_segments(src, [store.make_segment("far", 0.5, 1.5, "новая реплика")])
store.update(src, {"tracks": ["far", "mic"], "capture_gaps": [{"track": "far", "at_s": 0.2, "gap_s": 0.3}]})

info = calls.merge_recordings(src, dst)


def frames(rid, tr):
    with wave.open(str(store.track_path(rid, tr)), "rb") as wf:
        return wf.getnframes()


check("новая запись приклеена с 4-й секунды", abs(info["offset_s"] - 4.0) < 1e-6, info)
check("микрофон: 4 + 2 = 6 с", frames(dst, "mic") == 6 * SR, frames(dst, "mic") / SR)
check("собеседники: короткая дорожка догнана, 4 + 2 = 6 с", frames(dst, "far") == 6 * SR,
      frames(dst, "far") / SR)
far, _ = audio_io.read_wav(store.track_path(dst, "far"))
check("дорожка собеседников: на 3–4 с тишина", float(np.max(np.abs(far[3 * SR + 100:4 * SR - 100]))) < 1e-3)
check("дорожка собеседников: с 4-й секунды звук новой записи",
      float(np.max(np.abs(far[4 * SR + 100:5 * SR]))) > 0.1)
segs = store.sorted_segments(dst)
moved = [s for s in segs if s["text"] == "новая реплика"]
check("реплика новой записи сдвинута на 4 с", moved and abs(moved[0]["start"] - 4.5) < 1e-6,
      moved and moved[0]["start"])
meta = store.get(dst)
check("отметки перерывов сдвинуты", [g["at_s"] for g in meta["capture_gaps"]] == [1.0, 4.2],
      meta["capture_gaps"])
check("длительность 6 с", abs(meta["duration_s"] - 6.0) < 0.01, meta["duration_s"])
check("новая запись удалена", store.get(src) is None)
store.delete(dst)

# ------------------------------------------------------------ 5. сквозь службу
if "--no-live" not in sys.argv:
    say("")
    say("=== 5. Сквозь настоящую службу ===")
    OVERRIDES["call_end_confirm_s"] = 30
    OVERRIDES["call_watch_enabled"] = True
    from fastapi.testclient import TestClient

    from hagen import server

    ORIGIN = {"Origin": "http://127.0.0.1:8787"}
    made = []
    with TestClient(server.app, base_url="http://127.0.0.1:8787") as cli:
        auto = server._calls
        check("автоматика звонков поднята службой", auto is not None)
        if server._watcher is not None:
            server._watcher.stop()          # настоящий наблюдатель не мешает сценарию

        auto.on_call_start({"process": "ms-teams.exe"}, {"subject": "Проверка 43"})
        rec_a = server._call_active_recording()
        made.append(rec_a)
        check("звонок начал запись в службе", bool(rec_a), rec_a)
        st = cli.get("/api/state").json()
        check("окно видит идущую запись", st.get("active_recording") == rec_a, st.get("active_recording"))
        check("окно видит вопрос", (st.get("prompt") or {}).get("kind") == "call_started",
              (st.get("prompt") or {}).get("kind"))
        time.sleep(6.0)

        auto.on_call_end({"process": "ms-teams.exe"})
        pr = cli.get("/api/prompt").json().get("prompt") or {}
        check("конец звонка — вопрос в окне", pr.get("kind") == "call_ended", pr.get("kind"))
        r = cli.post("/api/prompt/%s" % pr.get("id"), headers=ORIGIN, json={"answer": "stop"})
        check("ответ кнопкой окна принят", r.status_code == 200 and r.json().get("ok"), r.text[:200])
        meta_a = store.get(rec_a) or {}
        check("запись остановлена", meta_a.get("status") == "recorded" and
              server._call_active_recording() is None, meta_a.get("status"))
        dur_a = float(meta_a.get("duration_s") or 0)
        say("   длительность первой записи: %.1f c" % dur_a)

        auto.on_call_start({"process": "ms-teams.exe"}, {"subject": "Проверка 43"})
        rec_b = server._call_active_recording()
        made.append(rec_b)
        pr = cli.get("/api/prompt").json().get("prompt") or {}
        check("второй звонок — вопрос о склейке", pr.get("kind") == "call_resumed", pr.get("kind"))
        check("подсказка про ту же встречу", "та же встреча" in (pr.get("text") or ""), pr.get("text"))
        time.sleep(5.0)
        r = cli.post("/api/prompt/%s" % pr.get("id"), headers=ORIGIN, json={"answer": "merge"})
        check("«Дописать в прошлую» выполнено", r.status_code == 200 and r.json().get("ok"), r.text[:300])
        check("вторая запись влита и удалена", store.get(rec_b) is None)
        check("запись продолжается в прошлой заметке", server._call_active_recording() == rec_a,
              server._call_active_recording())
        time.sleep(4.0)
        r = cli.post("/api/recordings/%s/stop" % rec_a, headers=ORIGIN, json={})
        check("остановка кнопкой «Стоп»", r.status_code == 200, r.status_code)
        meta_a = store.get(rec_a) or {}
        dur_final = float(meta_a.get("duration_s") or 0)
        say("   длительность после склейки и продолжения: %.1f c" % dur_final)
        check("прошлая заметка выросла на склеенное и дописанное (≥ +8 с)",
              dur_final >= dur_a + 8.0, "%.1f → %.1f" % (dur_a, dur_final))
        check("в карточке помечено, что влита вторая запись", rec_b in (meta_a.get("merged_from") or []))
        mic = frames(rec_a, "mic") / SR
        far_len = frames(rec_a, "far") / SR if store.track_path(rec_a, "far").exists() else mic
        check("дорожки после склейки идут в ногу (±0,6 с)", abs(mic - far_len) <= 0.6,
              "микрофон %.1f, собеседники %.1f" % (mic, far_len))
        for rid in made:
            if rid and store.get(rid):
                cli.delete("/api/recordings/%s?scope=all" % rid, headers=ORIGIN)
        check("тестовые записи убраны", all(store.get(r) is None for r in made if r))

say("")
say("ИТОГО провалов: %d" % len(FAIL))
for f in FAIL:
    say("   - " + f)
io.open(PROJECT / "tests" / "t43_result.txt", "w", encoding="utf-8").write("\n".join(LINES))
sys.exit(1 if FAIL else 0)
