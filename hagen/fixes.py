# -*- coding: utf-8 -*-
"""Словарь из ручных правок: «было → стало» запоминается и предлагается впредь.

Замысел 16.09, взят в работу 17.09. Поправив
«гранулятор» на «грануллятор» третий раз, человек вправе ожидать, что дальше
программа сделает это сама. Такой словарь имён и терминов никаким дообучением
модели не получить: он у каждого свой.

Как устроено.
  * При правке текста реплики сравниваем старый текст с новым ПО СЛОВАМ и
    складываем в журнал только изменившиеся куски — не всю фразу.
  * Журнал лежит рядом с записями (`data/fixes.json`) и в сборку не попадает:
    в нём фамилии, названия компаний и прочее, чему в чужих руках не место.
  * Когда одна и та же пара встретилась столько раз, сколько сказано в
    настройках, программа предлагает сделать её постоянной заменой.
  * Постоянные замены применяются к НОВОМУ распознанному тексту: к живой
    записи и к «Перечитать точнее». Уже готовые стенограммы не переписываются
    задним числом — человек мог править их руками, и затирать это нельзя.

Разрезы и переносы слов сюда не идут: там слова не менялись, менялись границы
реплик, и в словарь такому попадать незачем.
"""
from __future__ import annotations

import difflib
import io
import json
import logging
import re
import threading
import time
from typing import Any

from . import config

log = logging.getLogger("hagen.fixes")

#: Журнал правок. Рядом с записями, а не в настройках: это личные данные.
FIXES_NAME = "fixes.json"
#: Длиннее этого куска пара в словарь не годится: это уже переписанная фраза,
#: а не поправленное слово.
MAX_WORDS = 3
#: Сколько пар помним. Старые вытесняются: словарь должен отражать нынешние
#: термины, а не всё, что когда-либо правилось.
MAX_PAIRS = 400

_lock = threading.Lock()
_WORD_EDGE = r"(?<![0-9A-Za-zА-Яа-яЁё])%s(?![0-9A-Za-zА-Яа-яЁё])"


def path():
    return config.DATA_DIR / FIXES_NAME


def _read() -> dict[str, Any]:
    try:
        data = json.loads(io.open(path(), encoding="utf-8").read())
    except (OSError, ValueError):
        return {"pairs": []}
    if not isinstance(data, dict) or not isinstance(data.get("pairs"), list):
        return {"pairs": []}
    return data


def _write(data: dict[str, Any]) -> None:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    config.atomic_json(path(), data)


def _suggest_after() -> int:
    try:
        return max(1, int(config.get("fix_suggest_after") or 3))
    except (TypeError, ValueError):
        return 3


def changed_pairs(old: str, new: str) -> list[tuple[str, str]]:
    """Что именно поменялось в реплике: пары «было → стало» по словам.

    Берём только замены: добавленное и удалённое в словарь не годится (нечего
    заменять или не на что). Куски длиннее MAX_WORDS отбрасываем — это уже
    переписанная мысль, а не поправленное слово.
    """
    a, b = str(old or "").split(), str(new or "").split()
    if not a or not b:
        return []
    out: list[tuple[str, str]] = []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(a=a, b=b).get_opcodes():
        if op != "replace":
            continue
        if (i2 - i1) > MAX_WORDS or (j2 - j1) > MAX_WORDS:
            continue
        was, became = " ".join(a[i1:i2]), " ".join(b[j1:j2])
        if was and became and was != became:
            out.append((was, became))
    return out


def note_edit(old: str, new: str, rec_id: str | None = None) -> int:
    """Запомнить правку. Возвращает число отложенных пар."""
    pairs = changed_pairs(old, new)
    if not pairs:
        return 0
    now = time.time()
    with _lock:
        data = _read()
        rows = data["pairs"]
        for was, became in pairs:
            row = next((r for r in rows
                        if r.get("from") == was and r.get("to") == became), None)
            if row is None:
                row = {"from": was, "to": became, "count": 0, "at": now,
                       "rec_id": rec_id or ""}
                rows.append(row)
            row["count"] = int(row.get("count") or 0) + 1
            row["at"] = now
        if len(rows) > MAX_PAIRS:
            rows.sort(key=lambda r: (int(r.get("count") or 0), float(r.get("at") or 0)))
            del rows[:len(rows) - MAX_PAIRS]
        _write(data)
    log.info("правка запомнена: %s", "; ".join("«%s» → «%s»" % p for p in pairs))
    return len(pairs)


def rules(where: str = "transcript") -> list[dict[str, str]]:
    """Постоянные замены. `where`: "transcript", "dictation" или "all".

    У каждой замены помечено, где она действует. Словарь один на программу:
    раньше их было два — для стенограмм и для диктовки, — и они делали одно и
    то же для разных входов (решение 18.09).
    """
    raw = config.get("replacements") or []
    out: list[dict[str, str]] = []
    for r in raw:
        if not isinstance(r, dict):
            continue
        was, became = str(r.get("from") or "").strip(), str(r.get("to") or "").strip()
        scope = str(r.get("where") or "both").strip() or "both"
        if scope not in ("both", "transcript", "dictation"):
            scope = "both"
        if not was or not became or was == became:
            continue
        if where != "all" and scope != "both" and scope != where:
            continue
        out.append({"from": was, "to": became, "where": scope})
    return out


def suggestions() -> list[dict[str, Any]]:
    """Пары, которые пора предложить сделать постоянными."""
    need = _suggest_after()
    known = {(r["from"].lower(), r["to"].lower()) for r in rules()}
    out = []
    with _lock:
        rows = list(_read()["pairs"])
    for row in rows:
        was, became = str(row.get("from") or ""), str(row.get("to") or "")
        if int(row.get("count") or 0) < need or row.get("dismissed"):
            continue
        if (was.lower(), became.lower()) in known:
            continue
        out.append({"from": was, "to": became, "count": int(row["count"]),
                    "at": float(row.get("at") or 0)})
    out.sort(key=lambda r: (-r["count"], -r["at"]))
    return out


def dismiss(was: str, became: str) -> bool:
    """«Не предлагать эту пару»: правка остаётся в журнале, предложение уходит."""
    with _lock:
        data = _read()
        hit = False
        for row in data["pairs"]:
            if row.get("from") == was and row.get("to") == became:
                row["dismissed"] = True
                hit = True
        if hit:
            _write(data)
    return hit


def add_rule(was: str, became: str, where: str = "both") -> list[dict[str, str]]:
    """Сделать пару постоянной заменой. `where` — где она действует."""
    was, became = str(was or "").strip(), str(became or "").strip()
    if not was or not became or was == became:
        raise ValueError("Замена должна менять текст на другой текст.")
    if where not in ("both", "transcript", "dictation"):
        where = "both"
    have = rules("all")
    for r in have:
        if r["from"].lower() == was.lower():
            r["to"], r["where"] = became, where
            break
    else:
        have.append({"from": was, "to": became, "where": where})
    config.save({"replacements": have})
    log.info("замена «%s» → «%s» (%s)", was, became, where)
    return have


def set_scope(was: str, where: str) -> list[dict[str, str]]:
    """Поменять область действия замены, не трогая саму пару."""
    if where not in ("both", "transcript", "dictation"):
        raise ValueError("Непонятно, где действует замена: %s" % where)
    have = rules("all")
    for r in have:
        if r["from"].lower() == str(was or "").lower():
            r["where"] = where
            break
    else:
        raise ValueError("Такой замены нет: %s" % was)
    config.save({"replacements": have})
    return have


def forget_rule(was: str) -> list[dict[str, str]]:
    have = [r for r in rules("all") if r["from"].lower() != str(was or "").lower()]
    config.save({"replacements": have})
    return have


def _tokens(rule_side: str) -> list[str]:
    return [t for t in str(rule_side or "").split() if t]


def apply_words(words: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Те же замены в словах со временем: текст и слова не должны разойтись.

    Если замена короче или длиннее того, что заменяет, время делим между
    новыми словами поровну внутри прежнего промежутка: точнее взять неоткуда,
    а рассинхрон текста и слов сломал бы и разрез реплики, и разметку.
    """
    good = [dict(w) for w in (words or [])
            if isinstance(w, dict) and str(w.get("text") or "").strip()]
    if not good:
        return []
    for rule in rules():
        src, dst = _tokens(rule["from"]), _tokens(rule["to"])
        if not src or not dst:
            continue
        i = 0
        while i <= len(good) - len(src):
            same = all(str(good[i + k].get("text") or "").strip(" ,.;:!?").lower()
                       == src[k].lower() for k in range(len(src)))
            if not same:
                i += 1
                continue
            start = float(good[i].get("start") or 0.0)
            end = float(good[i + len(src) - 1].get("end") or start)
            tail = str(good[i + len(src) - 1].get("text") or "")
            punct = tail[len(tail.rstrip(" ,.;:!?")):]      # знак в конце сохраняем
            step = (end - start) / max(1, len(dst))
            made = []
            for k, word in enumerate(dst):
                keep = (word + punct) if k == len(dst) - 1 else word
                if good[i].get("text", "")[:1].isupper() and keep[:1].islower():
                    keep = keep[:1].upper() + keep[1:]
                made.append({"text": keep,
                             "start": round(start + step * k, 3),
                             "end": round(start + step * (k + 1), 3)})
            good[i:i + len(src)] = made
            i += len(made)
    return good


def apply(text: str) -> str:
    """Применить постоянные замены к свежераспознанному тексту.

    Заменяем ЦЕЛЫМИ словами, иначе «ЖД» внутри «ждать» испортило бы слово.
    Регистр первой буквы сохраняем: замена, написанная строчными, не должна
    превращать начало предложения в строчное.
    """
    out = str(text or "")
    if not out.strip():
        return out
    for rule in rules():
        pattern = re.compile(_WORD_EDGE % re.escape(rule["from"]), re.IGNORECASE)

        def swap(m: re.Match, to=rule["to"]) -> str:
            got = m.group(0)
            if got[:1].isupper() and to[:1].islower():
                return to[:1].upper() + to[1:]
            return to

        out = pattern.sub(swap, out)
    return out
