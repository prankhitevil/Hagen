# -*- coding: utf-8 -*-
"""Проверка 97: забытая запись (20.09).

Запись 18.09 проработала 1 ч 17 мин, из них разговор — первые шесть минут: её
просто забыли остановить. Теперь программа сама спрашивает «похоже, разговор
закончился — остановить?», и без ответа останавливает запись.

Два решения, на которых всё держится.

1. **Считаем СЛОВА, а не реплики.** После 05:52 в той записи распознавание
   выдавало однословный мусор — «По.», «Т.», «Ава.» — с промежутками в 11 и
   17 минут. Правило «нет реплик N минут» такой мусор сбрасывал бы снова и
   снова, и запись писалась бы дальше.

2. **Это новый ПОВОД для того же вопроса, а не второй механизм.** Вопрос
   показывает и снимает та же автоматика, что спрашивает в конце звонка
   (`CallAutomation`), и работает она в том числе для записей, начатых кнопкой.

Чего не делаем: не останавливаем молча. Совещание бывает с долгими паузами —
читают документ, смотрят экран, — поэтому сначала вопрос. «Продолжаю»
откладывает следующий вопрос, а не выключает защиту на всю запись.

Что проверяем:
  1. тишина: вопрос показан, кнопки «Остановить» и «Продолжаю»;
  2. без ответа запись останавливается сама;
  3. разговор идёт — вопроса нет; однословный мусор разговором не считается;
  4. запись короче окна тишины — рано спрашивать;
  5. «Продолжаю» откладывает вопрос, но не выключает защиту;
  6. 0 минут в настройках — защиты нет вовсе;
  7. чужой вопрос на экране не перебиваем; записи нет — молчим;
  8. сторож поднимается и останавливается.

Запуск из корня проекта:  .venv\\Scripts\\python.exe tests\\t97_idle_stop.py
"""
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
sys.path.insert(0, str(PROJECT / "tests"))
import isolate  # noqa: E402

isolate.voices()
isolate.settings()

from hagen import calls, config, store  # noqa: E402

FAIL = []


def ok(label, cond, detail=""):
    print("   %s   %s%s" % ("ok  " if cond else "ПРОВАЛ", label,
                            ("" if not detail else ": %s" % detail)))
    if not cond:
        FAIL.append(label)


OVERRIDES = {}
_real_get = config.get


def fake_get(key, default=None):
    if key in OVERRIDES:
        return OVERRIDES[key]
    return _real_get(key, default)


config.get = fake_get
OVERRIDES.update({"idle_stop_min": 10, "idle_stop_words": 5, "idle_stop_confirm_s": 1})


class FakeService:
    """Служба на подмену: помнит, что просили начать и остановить."""

    def __init__(self):
        self.active = None
        self.log = []

    def stop(self, rec_id):
        self.log.append(("stop", rec_id))
        self.active = None
        auto.recording_stopped(rec_id)


svc = FakeService()
auto = calls.CallAutomation(calls.Hooks(
    active_recording=lambda: svc.active,
    start_recording=lambda meeting: "",
    stop_recording=svc.stop,
    discard_recording=lambda rec_id: None,
    merge_and_resume=lambda src, dst: None,
    publish=lambda ev: None,
))


def new_recording(started_min_ago: float, title="Забытая планёрка"):
    """Запись, начатая столько-то минут назад."""
    meta = store.create(title=title, mode="online", source="live")
    started = datetime.now() - timedelta(minutes=started_min_ago)
    store.update(meta["id"], {"created_at": started.isoformat(timespec="seconds")})
    return meta["id"]


def say_words(rec_id, at_min, text):
    """Дописать реплику на такой-то минуте записи."""
    segs = store.load_transcript(rec_id).get("segments") or []
    start = at_min * 60.0
    segs.append(store.make_segment("mic", start, start + 3.0, text))
    store.replace_segments(rec_id, segs)


print("=== 1. Тишина: вопрос ===")
# Как 18.09: разговор в первые шесть минут, потом час однословного мусора.
rec = new_recording(75)
svc.active = rec
for i, word in enumerate(["Обсудили смету и сроки поставки на третий квартал.",
                          "Договорились созвониться в пятницу."]):
    say_words(rec, i + 1, word)
for i, junk in enumerate(["По.", "Т.", "Ава."]):
    say_words(rec, 20 + i * 15, junk)

ok("вопрос показан", auto.check_idle() is True)
pr = auto.prompt
ok("вопрос своего вида", pr and pr["kind"] == "idle_stop", pr and pr.get("kind"))
ok("кнопки «Остановить» и «Продолжаю»",
   pr and [b["id"] for b in pr["buttons"]] == ["stop", "keep"], pr and pr["buttons"])
ok("в тексте сказано про разговор, а не про звонок",
   pr and "разговор закончился" in pr["text"], pr and pr["text"])
ok("вопрос помнит, о какой записи он", pr and pr.get("rec_id") == rec, pr and pr.get("rec_id"))
ok("однословный мусор разговором не считается",
   auto._idle_words(rec, 10 * 60.0, 75 * 60.0) <= 1,
   auto._idle_words(rec, 10 * 60.0, 75 * 60.0))

print("\n=== 2. Без ответа — остановка ===")
time.sleep(1.4)
ok("запись остановлена сама", ("stop", rec) in svc.log, svc.log)
ok("вопрос снят", auto.prompt is None)

print("\n=== 3. Разговор идёт — вопроса нет ===")
svc.log.clear()
auto._idle_asked_at = 0.0
rec2 = new_recording(30, "Идёт разговор")
svc.active = rec2
say_words(rec2, 28, "Смета согласована, отгрузка в пятницу, остальное по почте.")
ok("при живом разговоре не спрашиваем", auto.check_idle() is False)
ok("слова последних минут посчитаны",
   auto._idle_words(rec2, 10 * 60.0, 30 * 60.0) == 8,
   auto._idle_words(rec2, 10 * 60.0, 30 * 60.0))
ok("старые слова в окно не попадают",
   auto._idle_words(rec2, 1 * 60.0, 30 * 60.0) == 0,
   auto._idle_words(rec2, 1 * 60.0, 30 * 60.0))

print("\n=== 4. Запись короче окна ===")
auto._idle_asked_at = 0.0
rec3 = new_recording(3, "Только началась")
svc.active = rec3
ok("молодую запись не трогаем", auto.check_idle() is False)

print("\n=== 5. «Продолжаю» откладывает вопрос ===")
auto._idle_asked_at = 0.0
svc.log.clear()
OVERRIDES["idle_stop_confirm_s"] = 60        # чтобы вопрос не истёк сам
rec4 = new_recording(75, "Долгая пауза")
svc.active = rec4
say_words(rec4, 2, "Начали обсуждать и надолго замолчали.")
ok("вопрос показан", auto.check_idle() is True)
pr = auto.prompt
res = auto.answer(pr["id"], "keep")
ok("ответ принят", res.get("ok") is True, str(res))
ok("запись не остановлена", not svc.log, svc.log)
ok("сразу второй раз не спрашиваем", auto.check_idle() is False)
# Защита не выключена: когда окно тишины прошло, вопрос задаётся снова.
auto._idle_asked_at = time.time() - 11 * 60
ok("после паузы спрашиваем снова", auto.check_idle() is True)
auto._close()

print("\n=== 6. Защиту можно выключить ===")
auto._idle_asked_at = 0.0
OVERRIDES["idle_stop_min"] = 0
ok("0 минут — не следим вовсе", auto.check_idle() is False)
OVERRIDES["idle_stop_min"] = 10

print("\n=== 7. Чужой вопрос и пустой экран ===")
auto._idle_asked_at = 0.0
auto._show("call_ended", "Звонок закончился. Остановить?",
           [("stop", "Остановить"), ("keep", "Писать дальше")])
ok("чужой вопрос не перебиваем", auto.check_idle() is False)
ok("чужой вопрос остался на экране", auto.prompt and auto.prompt["kind"] == "call_ended")
auto._close()
svc.active = None
ok("записи нет — молчим", auto.check_idle() is False)

print("\n=== 8. Сторож ===")
auto.start_idle_watch(every_s=0.2)
ok("сторож поднялся", auto._idle_watch is not None and auto._idle_watch.is_alive())
auto.start_idle_watch(every_s=0.2)
ok("второй раз не плодится", auto._idle_watch is not None)
auto.stop_idle_watch()
time.sleep(0.4)
ok("сторож остановлен", auto._idle_watch is None)

config.get = _real_get
for rid in (rec, rec2, rec3, rec4):
    try:
        store.delete(rid)
    except Exception:
        pass

print("\nВсего провалов: %d" % len(FAIL))
if FAIL:
    for f in FAIL:
        print("  - %s" % f)
sys.exit(1 if FAIL else 0)
