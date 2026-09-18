# -*- coding: utf-8 -*-
"""Проверка 14: действительно ли календарь Outlook читается.

Отличаем «встреч нет» от «фильтр по дате не работает»: сначала считаем все
встречи без фильтра, потом с фильтром на широкое окно.
"""
from __future__ import annotations

import io
import sys
from datetime import datetime, timedelta
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

LINES = []


def say(msg=""):
    LINES.append(str(msg))
    try:
        print(str(msg).encode("ascii", "replace").decode("ascii"), flush=True)
    except Exception:
        pass


def main():
    import pythoncom
    import win32com.client

    pythoncom.CoInitialize()
    try:
        try:
            app = win32com.client.GetActiveObject("Outlook.Application")
            say("подключился к запущенному Outlook")
        except Exception as err:
            say("GetActiveObject не сработал: %r" % (err,))
            return 1

        ns = app.GetNamespace("MAPI")
        cal = ns.GetDefaultFolder(9)
        say("папка календаря: %s" % cal.Name)

        items = cal.Items
        say("всего элементов в календаре (без разворота повторов): %d" % items.Count)

        items.Sort("[Start]")
        items.IncludeRecurrences = True

        now = datetime.now()
        for days in (1, 7, 30):
            a = (now - timedelta(days=days)).strftime("%m/%d/%Y %H:%M")
            b = (now + timedelta(days=days)).strftime("%m/%d/%Y %H:%M")
            flt = "[Start] >= '%s' AND [Start] <= '%s'" % (a, b)
            try:
                sel = items.Restrict(flt)
                n = 0
                first = []
                it = sel.GetFirst()
                while it is not None and n < 2000:
                    n += 1
                    if len(first) < 6:
                        try:
                            first.append((str(it.Start), str(it.Subject)[:46]))
                        except Exception:
                            pass
                    it = sel.GetNext()
                say("окно ±%-2d дней: найдено %d встреч" % (days, n))
                for s, subj in first:
                    say("      %s  %s" % (s, subj))
            except Exception as err:
                say("окно ±%-2d дней: ОШИБКА фильтра %r" % (days, err))

        say()
        say("=== проверяю чтение участников на любой встрече ===")
        a = (now - timedelta(days=60)).strftime("%m/%d/%Y %H:%M")
        b = (now + timedelta(days=60)).strftime("%m/%d/%Y %H:%M")
        sel = items.Restrict("[Start] >= '%s' AND [Start] <= '%s'" % (a, b))
        it = sel.GetFirst()
        shown = 0
        while it is not None and shown < 3:
            try:
                req = str(getattr(it, "RequiredAttendees", "") or "")
                opt = str(getattr(it, "OptionalAttendees", "") or "")
                org = str(getattr(it, "Organizer", "") or "")
                names = [x.strip() for x in (req + ";" + opt).split(";") if x.strip()]
                say("встреча: %s" % str(it.Subject)[:60])
                say("   начало: %s  организатор: %s" % (it.Start, org[:40]))
                say("   приглашённых: %d -> %s" % (len(names), names[:6]))
                try:
                    rec = [str(r.Name) for r in it.Recipients]
                    say("   через Recipients: %d -> %s" % (len(rec), rec[:6]))
                except Exception as err:
                    say("   Recipients недоступны: %r" % (err,))
                shown += 1
            except Exception as err:
                say("   ошибка чтения встречи: %r" % (err,))
            it = sel.GetNext()
        if shown == 0:
            say("встреч за ±60 дней нет — календарь пуст либо это другой профиль")

        say()
        say("=== что вернёт наша функция ===")
        from hagen import platform

        for w in (15, 240, 1440):
            m = platform.desktop().current_meeting(window_minutes=w)
            say("окно ±%-4d мин: %s" % (w, (m or {}).get("subject") if m else "ничего"))
        return 0
    finally:
        pythoncom.CoUninitialize()


if __name__ == "__main__":
    code = 1
    try:
        code = main()
    except Exception:
        import traceback

        say("СБОЙ:")
        say(traceback.format_exc())
    finally:
        io.open(PROJECT / "tests" / "t14_result.txt", "w", encoding="utf-8").write("\n".join(LINES))
    sys.exit(code)
