# -*- coding: utf-8 -*-
"""Проверка 66: время встречи из Outlook и выбор встречи для названия (16.09).

Найдено 16.09. Звонок в 12:21 получил имя «Ежедневная планерка с 9:30
до 10:00» — встречи, которая была тремя часами раньше. Причина не в названии:
Outlook отдал время 9:30, но пометил его поясом GMT (UTC+0), хотя показания
часов уже московские. Программа «переводила в местное время» и получала 12:30 —
ровно рядом со звонком.

Пояс, который приписывает pywin32, ненадёжен, поэтому смещение определяется по
самой встрече: рядом со Start Outlook отдаёт StartUTC, и разница между ними —
это и есть настоящее смещение.

Что проверяем:
  1. показания часов местные (StartUTC на три часа раньше) — время как есть;
  2. показания часов в UTC (StartUTC совпадает) — переводим в местное;
  3. нет StartUTC — верим показаниям, как их показывает Outlook;
  4. кривое значение не роняет разбор;
  5. выбор встречи: идущая берётся, закончившаяся — нет, будущая — только в
     пределах «за сколько минут до встречи» (решение 16.09);
  6. настоящий случай 16.09: звонок в 12:21, встреча 9:30–10:00 — имени не даёт;
  7. поля «за сколько минут до встречи» и «выдержка микрофона» есть в настройках
     звонков и подключены.

Outlook не запускается: календарь подставной.
Запуск из корня проекта:  .venv\\Scripts\\python.exe tests\\t66_meeting_time.py
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
isolate.settings()

LINES = []
FAIL = []


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


from hagen.platform.windows import desktop  # noqa: E402

OFFSET = datetime.now().astimezone().utcoffset() or timedelta(0)


class FakeDT:
    """Как pywintypes.datetime: показания часов плюс пояс, которому верить нельзя."""

    def __init__(self, dt, tz="GMT Standard Time"):
        self.year, self.month, self.day = dt.year, dt.month, dt.day
        self.hour, self.minute, self.second = dt.hour, dt.minute, dt.second
        self.tzinfo = tz


class FakeItem:
    def __init__(self, start, end, subject, utc=True, all_day=False, status=0):
        self.Subject = subject
        self.Start = FakeDT(start)
        self.End = FakeDT(end)
        if utc:                       # рядом лежит двойник в UTC — как у Outlook
            self.StartUTC = FakeDT(start - OFFSET)
            self.EndUTC = FakeDT(end - OFFSET)
        self.AllDayEvent = all_day
        self.MeetingStatus = status
        self.Organizer = "Организатор"
        self.Body = "Подключиться к собранию Microsoft Teams"
        self.Location = "Microsoft Teams"
        self.RequiredAttendees = "Иванов Иван; Петрова Ольга"
        self.OptionalAttendees = ""

    @property
    def Recipients(self):
        raise RuntimeError("нет доступа")   # как в корпоративном Outlook


class FakeItems:
    def __init__(self, items):
        self._items = items
        self.IncludeRecurrences = False

    def Sort(self, _field):
        self._items.sort(key=lambda it: (it.Start.hour, it.Start.minute))

    def Restrict(self, _flt):
        return list(self._items)


class FakeCalendar:
    def __init__(self, items):
        self.Items = FakeItems(items)


say("=== 1. Пояс определяется по самой встрече ===")
local_930 = datetime(2026, 9, 16, 9, 30)
# Так отдаёт Outlook на рабочем ноутбуке: часы местные, пояс подписан как GMT.
shown_local = FakeDT(local_930)
utc_twin = FakeDT(local_930 - OFFSET)
got = desktop._to_naive_dt(shown_local, utc_twin)
check("часы местные — время не сдвигается", got == local_930, got)

# А так наблюдалось 13.09: часы показывают UTC, двойник совпадает с ними.
shown_utc = FakeDT(local_930 - OFFSET)
got = desktop._to_naive_dt(shown_utc, FakeDT(local_930 - OFFSET))
check("часы в UTC — переводим в местное", got == local_930, got)

got = desktop._to_naive_dt(shown_local, None)
check("без двойника верим показаниям", got == local_930, got)
check("пусто остаётся пустым", desktop._to_naive_dt(None, None) is None)


class Broken:
    year = "не число"


check("кривое значение не роняет разбор", desktop._to_naive_dt(Broken(), None) is None)

say("")
say("=== 2. Разбор встречи целиком ===")
item = FakeItem(local_930, datetime(2026, 9, 16, 10, 0), "Ежедневная планерка с 9:30 до 10:00")
data = desktop._appointment_to_dict(item)
check("время встречи местное", data["_start_dt"] == local_930 and data["_end_dt"] == datetime(2026, 9, 16, 10, 0),
      (data["start"], data["end"]))
check("тема на месте", data["subject"] == "Ежедневная планерка с 9:30 до 10:00", data["subject"])
check("участники прочитаны, хотя Recipients недоступны", data["n_attendees"] == 2, data["attendees"])

say("")
say("=== 3. Какая встреча даёт имя записи ===")


def pick(now, meetings, window_minutes=5):
    """Что вернул бы просмотр календаря в момент now."""
    items = [FakeItem(s, e, subj) for s, e, subj in meetings]
    cal = FakeCalendar(items)
    out = desktop._scan_window(cal, "%d/%m/%Y %H:%M",
                                  now - timedelta(hours=12), now + timedelta(minutes=window_minutes),
                                  now, window_minutes * 60.0)
    return [(d["subject"], d["ongoing"]) for d in out]


PLAN = (datetime(2026, 9, 16, 9, 30), datetime(2026, 9, 16, 10, 0), "Планёрка")
NEXT = (datetime(2026, 9, 16, 12, 30), datetime(2026, 9, 16, 13, 0), "Следующая")

check("идущая встреча берётся",
      pick(datetime(2026, 9, 16, 9, 40), [PLAN]) == [("Планёрка", True)],
      pick(datetime(2026, 9, 16, 9, 40), [PLAN]))
check("закончившаяся встреча имени не даёт",
      pick(datetime(2026, 9, 16, 10, 3), [PLAN]) == [], pick(datetime(2026, 9, 16, 10, 3), [PLAN]))
# Короткая встреча, кончившаяся минуту назад: по времени начала она рядом, но
# имени давать не должна — окно смотрит только вперёд.
SHORT = (datetime(2026, 9, 16, 12, 15), datetime(2026, 9, 16, 12, 17), "Короткая")
check("только что закончившаяся короткая встреча имени не даёт",
      pick(datetime(2026, 9, 16, 12, 18), [SHORT]) == [], pick(datetime(2026, 9, 16, 12, 18), [SHORT]))
check("будущая встреча берётся, если вот-вот начнётся",
      pick(datetime(2026, 9, 16, 12, 27), [NEXT]) == [("Следующая", False)],
      pick(datetime(2026, 9, 16, 12, 27), [NEXT]))
check("будущая встреча за 9 минут при сроке 5 минут не берётся",
      pick(datetime(2026, 9, 16, 12, 21), [NEXT]) == [], pick(datetime(2026, 9, 16, 12, 21), [NEXT]))
check("при сроке 15 минут — берётся",
      pick(datetime(2026, 9, 16, 12, 21), [NEXT], window_minutes=15) == [("Следующая", False)],
      pick(datetime(2026, 9, 16, 12, 21), [NEXT], window_minutes=15))
# Ради этого всё и делалось: звонок в 12:21 и планёрка в 9:30.
check("случай 16.09: звонок в 12:21 не получает имя планёрки 9:30",
      pick(datetime(2026, 9, 16, 12, 21), [PLAN], window_minutes=15) == [],
      pick(datetime(2026, 9, 16, 12, 21), [PLAN], window_minutes=15))

say("")
say("=== 4. Настройки ===")
from hagen import config  # noqa: E402

check("заводской срок «до встречи» — 5 минут", config.DEFAULTS["meeting_lookahead_min"] == 5,
      config.DEFAULTS["meeting_lookahead_min"])
check("заводская выдержка микрофона — 3 секунды", config.DEFAULTS["call_watch_debounce_s"] == 3,
      config.DEFAULTS["call_watch_debounce_s"])
html = io.open(PROJECT / "hagen" / "static" / "index.html", encoding="utf-8").read()
js = io.open(PROJECT / "hagen" / "static" / "app.js", encoding="utf-8").read()
pane = html[html.find('id="t-call"'):html.find('id="t-processing"')]
check("поле «до встречи» на вкладке «Запись и звонки»", 'id="set-meeting-ahead"' in pane)
check("поле выдержки микрофона на вкладке «Запись и звонки»", 'id="set-call-debounce"' in pane)
check("у обоих полей есть подсказка «?»", pane.count('class="q"') >= 2)
check("сказано, что закончившаяся встреча имени не даёт",
      "Закончившаяся встреча имени не даёт" in pane)
check("поля читаются и сохраняются",
      "meeting_lookahead_min:" in js and "call_watch_debounce_s:" in js
      and "$('set-meeting-ahead').value" in js and "$('set-call-debounce').value" in js)
check("ноль в поле уважается, а не заменяется на заводское",
      "s.meeting_lookahead_min === null) ? 5" in js and "s.call_watch_debounce_s === null) ? 3" in js)

say("")
say("ИТОГО провалов: %d" % len(FAIL))
for f in FAIL:
    say("   - " + f)
io.open(PROJECT / "tests" / "t66_result.txt", "w", encoding="utf-8").write("\n".join(LINES))
sys.exit(1 if FAIL else 0)
