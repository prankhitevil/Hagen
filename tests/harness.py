# -*- coding: utf-8 -*-
"""Общее для проверок: печать, учёт провалов, файл результата.

Раньше каждая проверка носила свои `say()` и `check()` — семьдесят копий одного
и того же десятка строк, и любая правка печати означала семьдесят правок.
Здесь они лежат один раз. Проверка берёт их так:

    from harness import LINES, FAIL, say, check, finish
    ...
    sys.exit(finish("t71"))

`LINES` и `FAIL` — те же списки, что были в каждой проверке: в них можно
дописывать и по ним считать, как раньше. `finish` печатает итог, пишет
`tests/<имя>_result.txt` и возвращает код выхода.
"""
from __future__ import annotations

import io
from pathlib import Path
from typing import Any

TESTS = Path(__file__).resolve().parent

#: Всё, что напечатано, — уходит и в файл результата.
LINES: list[str] = []
#: Имена проваленных пунктов.
FAIL: list[str] = []


def say(msg: Any = "") -> None:
    """Печать, переживающая консоль cp1251: там нет ни стрелок, ни тире."""
    LINES.append(str(msg))
    try:
        print(str(msg), flush=True)
    except Exception:
        try:
            print(str(msg).encode("ascii", "replace").decode("ascii"), flush=True)
        except Exception:
            pass


def check(name: str, ok: Any, detail: Any = "") -> bool:
    """Пункт проверки: «ok» или «ПЛОХО» с подробностью. Возвращает ok."""
    if not ok:
        FAIL.append(name)
    say(("   ok    " if ok else "   ПЛОХО ") + name
        + (": " + str(detail)[:300] if detail != "" else ""))
    return bool(ok)


def expect(name: str, got: Any, want: Any) -> bool:
    """Пункт проверки с ожидаемым значением: печатает и что вышло, и что ждали."""
    ok = got == want
    if not ok:
        FAIL.append(name)
    say(("   ok    " if ok else "   ПЛОХО ") + name + ": " + repr(got)
        + ("" if ok else "  (ждали " + repr(want) + ")"))
    return ok


def finish(name: str) -> int:
    """Итог: сводка провалов, файл результата, код выхода для run_all."""
    say("")
    say("ИТОГО провалов: %d" % len(FAIL))
    for item in FAIL:
        say("   - " + item)
    io.open(TESTS / (name + "_result.txt"), "w", encoding="utf-8").write("\n".join(LINES))
    return 1 if FAIL else 0
