# -*- coding: utf-8 -*-
"""Проверка 108: какая встреча даёт имя записи, когда в календаре их две (решение 22.09).

Случай 22.09: одна встреча стоит в календаре 14:30–15:30, а кончилась в
14:51; другая — с 15:00. Звонок в 14:59
получил имя первой: идущая была важнее будущей. Теперь первой идёт та, что
начинается ближе к звонку, а другая встреча того же времени — кнопкой в
уведомлении о звонке: одним щелчком запись переходит на неё.

Что проверяем (Outlook и служба подменены):
  1. порядок встреч: сегодняшний случай, опоздание к только что начавшейся,
     одна встреча;
  2. ответ календаря: другие встречи — только по просьбе и без служебных полей;
  3. уведомление о звонке: кнопка «Это «…»», в карточку записи другие встречи
     не попадают; кнопка переключает название и встречу; без второй встречи —
     всё как раньше;
  4. заглушка отдаёт другие встречи так же, как Windows.
"""
import io
import sys
from datetime import datetime, timedelta
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
sys.path.insert(0, str(PROJECT / "tests"))
import isolate  # noqa: E402

isolate.voices()
isolate.settings(outlook_enabled=True, call_watch_autostart=True, meeting_lookahead_min=5)

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


from hagen import calls, platform, store  # noqa: E402
from hagen.platform import fake  # noqa: E402
from hagen.platform.windows import desktop  # noqa: E402

EARLIER = "Планёрка отдела"
LATER = "Обзор проекта"


def meeting(subject, start, end, now):
    return {"subject": subject, "_start_dt": start, "_end_dt": end,
            "ongoing": start <= now <= end, "attendees": ["Иван Петров", "Орлов"],
            "n_attendees": 2}


say("=== 1. Порядок встреч ===")
now = datetime(2026, 9, 22, 14, 59, 49)
cands = [meeting(EARLIER, datetime(2026, 9, 22, 14, 30), datetime(2026, 9, 22, 15, 30), now),
         meeting(LATER, datetime(2026, 9, 22, 15, 0), datetime(2026, 9, 22, 16, 0), now)]
got = [m["subject"] for m in desktop.order_meetings(cands, now)]
check("случай 22.09: звонок в 14:59:49 — встреча с 15:00, а не 14:30–15:30",
      got == [LATER, EARLIER], got)
now2 = datetime(2026, 9, 22, 15, 2)
late = [meeting(LATER, datetime(2026, 9, 22, 15, 0), datetime(2026, 9, 22, 16, 0), now2),
        meeting("Планёрка", datetime(2026, 9, 22, 15, 5), datetime(2026, 9, 22, 15, 30), now2)]
got = [m["subject"] for m in desktop.order_meetings(late, now2)]
check("опоздал на 2 минуты, а следующая через 3 — берётся идущая", got == [LATER, "Планёрка"], got)
check("одна встреча — она и есть", [m["subject"] for m in desktop.order_meetings(cands[:1], now)] == [EARLIER])

say("")
say("=== 2. Ответ календаря ===")
import win32com.client  # noqa: E402


class FakeOutlook:
    def GetNamespace(self, name):
        return self

    def GetDefaultFolder(self, n):
        return object()


real_active, real_scan = win32com.client.GetActiveObject, desktop._scan_window


def scan(cal, fmt, lower, upper, now, window_s):
    # Как 22.09, только относительно настоящего «сейчас»: прошлая встреча
    # началась полчаса назад и стоит ещё на полчаса, новая — через 11 секунд.
    return [meeting(EARLIER, now - timedelta(minutes=29, seconds=49), now + timedelta(minutes=30), now),
            meeting(LATER, now + timedelta(seconds=11), now + timedelta(minutes=60), now)]


win32com.client.GetActiveObject = lambda name: FakeOutlook()
desktop._scan_window = scan
try:
    plain = desktop.current_meeting()
    full = desktop.current_meeting(with_alternatives=True)
finally:
    win32com.client.GetActiveObject, desktop._scan_window = real_active, real_scan
check("выбрана встреча с 15:00", (plain or {}).get("subject") == LATER, plain and plain.get("subject"))
check("без просьбы других встреч в ответе нет — кнопка «Старт» получает то же, что раньше",
      "alternatives" not in (plain or {}), list(plain or {}))
alts = (full or {}).get("alternatives") or []
check("по просьбе — другая встреча того же времени", [m.get("subject") for m in alts] == [EARLIER], alts)
check("служебного времени наружу не уходит",
      not any(k.startswith("_") for m in [full or {}] + alts for k in m), [list(m) for m in alts])

say("")
say("=== 3. Уведомление о звонке ===")


class Service:
    """Служба глазами автоматики: запись заводится в настоящей базе записей."""

    def __init__(self):
        self.active = None
        self.started = []
        self.events = []
        self.made = []

    def start(self, meet):
        self.started.append(dict(meet or {}))
        meta = store.create(title=desktop.suggest_title(meet), mode="online", source="live")
        if meet:
            store.update(meta["id"], {"meeting": meet})
        self.active = meta["id"]
        self.made.append(meta["id"])
        return meta["id"]

    def stop(self, rec_id):
        self.active = None


svc = Service()
auto = calls.CallAutomation(calls.Hooks(
    active_recording=lambda: svc.active,
    start_recording=svc.start,
    stop_recording=svc.stop,
    discard_recording=lambda rec_id: None,
    merge_and_resume=lambda src, dst: None,
    publish=svc.events.append,
))
try:
    later_meeting = {"subject": LATER, "attendees": ["Иван Петров"], "n_attendees": 1,
              "alternatives": [{"subject": EARLIER, "attendees": ["Орлов", "Белова"], "n_attendees": 2},
                               {"subject": LATER, "attendees": [], "n_attendees": 0},
                               {"subject": "Без темы"}]}
    auto.on_call_start({"process": "ms-teams.exe"}, later_meeting)
    rid = svc.active
    meta = store.get(rid) or {}
    check("запись названа по ближайшей встрече", meta.get("title") == LATER, meta.get("title"))
    check("другие встречи в карточку записи не попали",
          "alternatives" not in (meta.get("meeting") or {}) and "alternatives" not in svc.started[0],
          list(meta.get("meeting") or {}))
    pr = auto.prompt or {}
    labels = [b["label"] for b in pr.get("buttons") or []]
    check("в уведомлении — «Не писать» и «Это «…»» с другой встречей",
          labels == ["Не писать", "Это «%s»" % EARLIER], labels)
    check("та же тема и встреча без темы кнопок не дают", len(labels) == 2, labels)
    check("в тексте сказано, что в это же время есть другая встреча",
          "в это же время: «%s»" % EARLIER in pr.get("text", ""), pr.get("text"))
    check("вопрос с выбором висит дольше обычного", pr.get("timeout_s") == 120, pr.get("timeout_s"))
    res = auto.answer(pr.get("id"), "meeting-0", source="toast")
    meta = store.get(rid) or {}
    check("кнопка сработала", res.get("ok") is True, res)
    check("запись переименована по выбранной встрече", meta.get("title") == EARLIER, meta.get("title"))
    check("участники — от выбранной встречи",
          (meta.get("meeting") or {}).get("attendees") == ["Орлов", "Белова"], meta.get("meeting"))
    check("окно узнало о переименовании",
          any(e.get("type") == "recording" and (e.get("meta") or {}).get("title") == EARLIER
              for e in svc.events), [e.get("type") for e in svc.events])
    check("и получило понятное сообщение",
          any(e.get("type") == "notice" and EARLIER in str(e.get("text")) for e in svc.events))
    check("вопрос снят", auto.prompt is None)

    auto.on_call_end({"process": "ms-teams.exe"})
    auto.answer((auto.prompt or {}).get("id"), "stop")
    auto.last_stopped = None
    auto.on_call_start({"process": "ms-teams.exe"}, {"subject": LATER, "alternatives": []})
    pr = auto.prompt or {}
    check("второй встречи нет — всё как раньше: одна кнопка «Не писать», 45 с",
          [b["label"] for b in pr.get("buttons") or []] == ["Не писать"] and pr.get("timeout_s") == 45,
          (pr.get("buttons"), pr.get("timeout_s")))
    svc.active = None
    auto.on_call_start({"process": "ms-teams.exe"}, None)
    pr = auto.prompt or {}
    check("без встречи вовсе — запись идёт, кнопка одна",
          svc.active is not None and [b["label"] for b in pr.get("buttons") or []] == ["Не писать"],
          pr.get("buttons"))
finally:
    for rid in svc.made:
        store.delete(rid)

say("")
say("=== 4. Заглушка ===")
platform.use("fake")
fake.reset()
fake.desktop.MEETING = {"subject": LATER, "alternatives": [{"subject": EARLIER}]}
check("заглушка: без просьбы — без других встреч",
      "alternatives" not in platform.desktop().current_meeting())
check("заглушка: по просьбе — с ними",
      [m["subject"] for m in platform.desktop().current_meeting(with_alternatives=True)["alternatives"]]
      == [EARLIER])
check("заглушка не портит заданную встречу", fake.desktop.MEETING.get("alternatives") == [{"subject": EARLIER}])
fake.reset()
platform.use(None)

say("")
say("ИТОГО провалов: %d" % len(FAIL))
for f in FAIL:
    say("   - " + f)
io.open(PROJECT / "tests" / "t108_result.txt", "w", encoding="utf-8").write("\n".join(LINES))
sys.exit(1 if FAIL else 0)
