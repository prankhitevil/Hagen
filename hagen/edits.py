# -*- coding: utf-8 -*-
"""Ручная правка стенограммы: разрезать реплику, передать кусок другому, поправить текст.

Разметка голосов ошибается предсказуемо: хвост фразы уезжает к соседу, а
человек, сказавший пару слов, приклеивается к похожему голосу. Настройками это
чинится не всегда, поэтому у человека должна быть возможность поправить руками
(решение 16.09).

Здесь только работа с репликами записи — ни моделей, ни звука:
  * разрезать реплику по границе слова;
  * передать выделенные слова другому говорящему (соседу или любому);
  * поправить текст реплики.

Перед каждой правкой откладывается снимок стенограммы, поэтому «Отменить»
(Ctrl+Z) возвращает предыдущее состояние. Снимки лежат стопкой: отменять можно
несколько правок подряд, вплоть до глубины и срока из настроек — иначе рядом с
длинной записью копились бы десятки копий стенограммы.

Время слов есть не всегда: у живого текста, пришедшего по ходу разговора, его
нет. Тогда границы считаются пропорционально длине слов, и об этом честно
сказано в ответе (`exact: false`) — интерфейс предупреждает.
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from pathlib import Path
from typing import Any

from . import store

log = logging.getLogger("hagen.edits")

#: Снимки стенограммы до правок — стопка рядом с записью, новые сверху.
UNDO_DIR = "transcript.undo"
#: Снимок старого образца (одна ступень) — переносится в стопку при встрече.
UNDO_NAME = "transcript.undo.json"


class EditError(RuntimeError):
    """Правка невозможна, и человеку надо сказать почему."""


def _undo_dir(rec_id: str) -> Path:
    return store.paths(rec_id)["dir"] / UNDO_DIR


def _limits() -> tuple[int, float]:
    """Сколько правок хранить и сколько минут. 0 в любом из них — без предела."""
    from . import config

    try:
        steps = int(config.get("edit_undo_steps") or 0)
    except (TypeError, ValueError):
        steps = 20
    try:
        minutes = float(config.get("edit_undo_minutes") or 0)
    except (TypeError, ValueError):
        minutes = 30.0
    return max(0, steps), max(0.0, minutes)


def _stack(rec_id: str) -> list[Path]:
    """Снимки от старых к новым. Заодно подбираем снимок старого образца."""
    folder = _undo_dir(rec_id)
    old = store.paths(rec_id)["dir"] / UNDO_NAME
    if old.exists():
        folder.mkdir(parents=True, exist_ok=True)
        old.replace(folder / "0001.json")
    if not folder.exists():
        return []
    return sorted(p for p in folder.glob("*.json") if p.stem.isdigit())


def _prune(rec_id: str) -> list[Path]:
    """Убрать лишние снимки: сверх глубины и просроченные."""
    steps, minutes = _limits()
    files = _stack(rec_id)
    drop: list[Path] = []
    if steps and len(files) > steps:
        drop += files[:len(files) - steps]
    if minutes:
        edge = time.time() - minutes * 60.0
        drop += [p for p in files if p not in drop and p.stat().st_mtime < edge]
    for p in drop:
        p.unlink(missing_ok=True)
    return [p for p in files if p not in drop]


def _count_edit(rec_id: str) -> None:
    """Счётчик ручных правок в записи — он переживает выброс старых снимков.

    Нужен, чтобы перед «Перечитать точнее» честно сказать, сколько работы
    пропадёт: стопка снимков для этого не годится, её подрезают по сроку.
    """
    meta = store.get(rec_id) or {}
    try:
        was = int(meta.get("edits_count") or 0)
    except (TypeError, ValueError):
        was = 0
    store.update(rec_id, {"edits_count": was + 1})


def snapshot(rec_id: str, what: str) -> None:
    """Отложить стенограмму до правки: «Отменить» вернёт её целиком.

    Снимки складываются стопкой, поэтому отменять можно несколько правок
    подряд — вплоть до глубины и срока из настроек (решение 16.09).
    """
    files = _prune(rec_id)
    nxt = (int(files[-1].stem) + 1) if files else 1
    _count_edit(rec_id)
    payload = {"what": what, "at": time.time(),
               "segments": store.sorted_segments(rec_id, include_echo=True)}
    folder = _undo_dir(rec_id)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / ("%04d.json" % nxt)).write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    _prune(rec_id)


def _read(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as err:
        raise EditError("Снимок стенограммы не читается: %s" % err) from err
    if not isinstance(data.get("segments"), list):
        raise EditError("Снимок стенограммы испорчен.")
    return data


def undo_info(rec_id: str) -> dict[str, Any]:
    """Что вернёт «Отменить» и сколько шагов назад ещё можно. Пусто — нечего."""
    files = _prune(rec_id)
    if not files:
        return {}
    try:
        data = _read(files[-1])
    except EditError:
        return {"what": "правка", "steps": len(files)}
    return {"what": str(data.get("what") or "правка"), "steps": len(files)}


def undo(rec_id: str) -> dict[str, Any]:
    """Вернуть стенограмму на шаг назад — к состоянию до последней правки."""
    files = _prune(rec_id)
    if not files:
        raise EditError("Отменять нечего: правок больше не осталось.")
    top = files[-1]
    data = _read(top)
    store.replace_segments(rec_id, data["segments"])
    top.unlink(missing_ok=True)
    store.refresh_participants(rec_id)
    left = len(files) - 1
    log.info("запись %s: отменена правка «%s», осталось шагов: %d",
             rec_id, data.get("what"), left)
    return {"what": str(data.get("what") or "правка"), "steps_left": left,
            "segments": store.sorted_segments(rec_id)}


def _find(segments: list[dict[str, Any]], seg_id: str) -> int:
    for i, seg in enumerate(segments):
        if str(seg.get("id")) == str(seg_id):
            return i
    raise EditError("Реплика не найдена — обновите страницу.")


def words_of(seg: dict[str, Any]) -> tuple[list[dict[str, Any]], bool]:
    """Слова реплики со временем. Второе значение — настоящее ли это время.

    Если времени слов нет (живой текст), раздаём его пропорционально длине слов:
    делить фразу всё равно нужно, но точность границы — «на глаз».
    """
    raw = seg.get("words")
    good = [w for w in (raw or []) if isinstance(w, dict) and str(w.get("text") or "").strip()]
    if good:
        return [dict(w) for w in good], True
    parts = str(seg.get("text") or "").split()
    if not parts:
        return [], False
    start = float(seg.get("start") or 0.0)
    end = max(start, float(seg.get("end") or 0.0))
    total = sum(len(p) for p in parts) or 1
    out: list[dict[str, Any]] = []
    at = start
    for part in parts:
        share = (end - start) * (len(part) / total)
        out.append({"text": part, "start": round(at, 3), "end": round(at + share, 3)})
        at += share
    return out, False


def _piece(seg: dict[str, Any], words: list[dict[str, Any]], exact: bool) -> dict[str, Any]:
    """Новая реплика из части слов: всё как у исходной, кроме текста и времени."""
    child = dict(seg)
    child["id"] = uuid.uuid4().hex[:8]
    child["text"] = " ".join(str(w.get("text") or "").strip() for w in words).strip()
    child["start"] = round(float(words[0].get("start") or seg.get("start") or 0.0), 3)
    child["end"] = round(float(words[-1].get("end") or seg.get("end") or 0.0), 3)
    child["words"] = [dict(w) for w in words] if exact else []
    child["suggestion"] = None
    return child


def _joined_words(a: dict[str, Any], b: dict[str, Any]) -> list[dict[str, Any]]:
    """Слова склеенной реплики: только если время слов настоящее у обеих половин."""
    wa, ea = words_of(a)
    wb, eb = words_of(b)
    return [dict(w) for w in wa] + [dict(w) for w in wb] if ea and eb else []


def move_words(rec_id: str, seg_id: str, first: int, last: int, where: str) -> dict[str, Any]:
    """Передать выделенные слова соседней реплике — той, что выше или ниже.

    Соседу отдаётся край реплики: вверх — начало, вниз — хвост. Кусок из
    середины сначала надо отрезать (разрез по слову), иначе непонятно, что
    делать с остатком.

    Выделена вся реплика целиком — она уходит соседу целиком и исчезает как
    отдельная (замечено 17.09: эхо колонок приписало ему чужую
    фразу от первого до последнего слова, и передать её было нечем).
    """
    if where not in ("prev", "next"):
        raise EditError("Непонятно, кому передавать: соседу выше или ниже.")
    segments = store.sorted_segments(rec_id, include_echo=True)
    i = _find(segments, seg_id)
    seg = segments[i]
    words, exact = words_of(seg)
    first, last = int(first), int(last)
    if not words or first < 0 or last >= len(words) or first > last:
        raise EditError("Выделите слова внутри реплики.")
    whole = last - first + 1 == len(words)
    if not whole and where == "prev" and first != 0:
        raise EditError("Соседу выше передаётся начало реплики: выделите её от первого слова.")
    if not whole and where == "next" and last != len(words) - 1:
        raise EditError("Соседу ниже передаётся хвост реплики: выделите её до последнего слова.")
    j = i - 1 if where == "prev" else i + 1
    if j < 0 or j >= len(segments):
        raise EditError("Передавать некому: соседней реплики с этой стороны нет.")
    near = segments[j]
    moved = words[first:last + 1]
    rest = words[:first] if where == "next" else words[last + 1:]

    snapshot(rec_id, "перенос слов соседу")
    piece = _piece(seg, moved, exact)
    grown = dict(near)
    grown["by_hand"] = True                 # слова отданы человеком, а не моделью
    grown["speaker_locked"] = True
    if where == "prev":
        grown["text"] = (str(near.get("text") or "").strip() + " " + piece["text"]).strip()
        grown["words"] = _joined_words(near, piece)
        grown["end"] = max(float(near.get("end") or 0.0), piece["end"])
    else:
        grown["text"] = (piece["text"] + " " + str(near.get("text") or "").strip()).strip()
        grown["words"] = _joined_words(piece, near)
        grown["start"] = min(float(near.get("start") or 0.0), piece["start"])
    segments[j] = grown
    if whole:
        segments.pop(i)                        # отдавать было нечего — реплики больше нет
    else:
        left = _piece(seg, rest, exact)
        left["id"] = str(seg.get("id"))        # остаток — та же реплика
        segments[i] = left
    store.replace_segments(rec_id, segments)
    store.refresh_participants(rec_id)
    log.info("запись %s: %d слов передано соседу %s%s",
             rec_id, len(moved), where, " (реплика целиком)" if whole else "")
    return {"exact": exact, "to": str(grown.get("speaker") or ""),
            "segments": store.sorted_segments(rec_id)}


def _glue(host: dict[str, Any], add: dict[str, Any], before: bool = False) -> dict[str, Any]:
    """Приклеить кусок к реплике: имя, id и всё прочее — у принимающей стороны."""
    out = dict(host)
    h = str(host.get("text") or "").strip()
    a = str(add.get("text") or "").strip()
    out["text"] = (a + " " + h).strip() if before else (h + " " + a).strip()
    out["words"] = _joined_words(add, host) if before else _joined_words(host, add)
    out["start"] = round(min(float(host.get("start") or 0.0), float(add.get("start") or 0.0)), 3)
    out["end"] = round(max(float(host.get("end") or 0.0), float(add.get("end") or 0.0)), 3)
    # Пометка «решено человеком» не должна теряться при склейке: иначе решение
    # не переживёт «Перечитать точнее». Склеиваются только реплики одного
    # человека, поэтому пометка верна для всего куска целиком.
    if host.get("by_hand") or add.get("by_hand"):
        out["by_hand"] = True
        out["speaker_locked"] = True
    return out


def speakers_of(rec_id: str) -> list[dict[str, Any]]:
    """Кто говорит в этой записи — для выбора «кому отдать слова»."""
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for seg in store.sorted_segments(rec_id, include_echo=True):
        key = str(seg.get("speaker_key") or "")
        if not key or key in seen:
            continue
        seen.add(key)
        out.append({"key": key, "name": str(seg.get("speaker") or key)})
    return out


def assign_speaker(rec_id: str, seg_id: str, first: int, last: int, speaker_key: str) -> dict[str, Any]:
    """Отдать выделенные слова любому говорящему записи, не только соседу.

    Кусок вырезается из реплики и становится репликой выбранного человека.
    Если рядом уже есть его реплика — приклеиваем к ней, чтобы не плодить
    обрывки в стенограмме.
    """
    segments = store.sorted_segments(rec_id, include_echo=True)
    i = _find(segments, seg_id)
    seg = segments[i]
    words, exact = words_of(seg)
    first, last = int(first), int(last)
    if not words or first < 0 or last >= len(words) or first > last:
        raise EditError("Выделите слова внутри реплики.")
    key = str(speaker_key or "").strip()
    who = next((s for s in speakers_of(rec_id) if s["key"] == key), None)
    if who is None:
        raise EditError("Такого говорящего в этой записи нет.")
    if key == str(seg.get("speaker_key") or ""):
        raise EditError("Эти слова и так за этим человеком.")

    snapshot(rec_id, "передача слов другому")
    piece = _piece(seg, words[first:last + 1], exact)
    piece["speaker"] = who["name"]
    piece["speaker_key"] = key
    piece["speaker_locked"] = True          # это решение человека, не модели
    piece["by_hand"] = True                 # переживёт «Перечитать точнее»
    parts = [piece]
    if first > 0:
        parts.insert(0, _piece(seg, words[:first], exact))
    if last < len(words) - 1:
        parts.append(_piece(seg, words[last + 1:], exact))
    # Исходная реплика остаётся собой: её id получает первый оставшийся кусок.
    keep = next((p for p in parts if p is not piece), piece)
    keep["id"] = str(seg.get("id"))
    at = i + parts.index(piece)
    segments[i:i + 1] = parts

    def same(other: dict[str, Any]) -> bool:
        """Сосед того же человека И той же дорожки.

        Дорожку сверяем с 17.09: эхо колонок живёт в микрофоне, и, приклеив его
        к реплике собеседников, мы теряли бы, к какой дорожке относилось решение
        человека, — а без этого его не наложить на пересобранную стенограмму.
        """
        return (str(other.get("speaker_key") or "") == key
                and str(other.get("track") or "") == str(piece.get("track") or ""))

    if at + 1 < len(segments) and same(segments[at + 1]):
        segments[at:at + 2] = [_glue(segments[at + 1], segments[at], before=True)]
    if at > 0 and same(segments[at - 1]):
        segments[at - 1:at + 1] = [_glue(segments[at - 1], segments[at])]
    store.replace_segments(rec_id, segments)
    store.refresh_participants(rec_id)
    log.info("запись %s: %d слов передано «%s»", rec_id, last - first + 1, who["name"])
    return {"exact": exact, "to": who["name"], "segments": store.sorted_segments(rec_id)}


def edit_text(rec_id: str, seg_id: str, text: str) -> dict[str, Any]:
    """Поправить текст реплики руками: опечатки, имена, обрывки слов.

    Время реплики не трогаем — правится текст, а не звук. Время слов остаётся
    только если слов столько же, сколько было: тогда понятно, где какое. Иначе
    оно стирается, и дальше границы считаются по длине слов.
    """
    segments = store.sorted_segments(rec_id, include_echo=True)
    i = _find(segments, seg_id)
    seg = segments[i]
    new = " ".join(str(text or "").split())
    if not new:
        raise EditError("Реплика не может остаться пустой.")
    if new == str(seg.get("text") or "").strip():
        return {"changed": False, "kept_times": True, "segments": store.sorted_segments(rec_id)}

    snapshot(rec_id, "правка текста")
    # Копим словарь из правок: «было → стало» пригодится в следующий раз.
    # Журнал лежит рядом с записями.
    try:
        from . import fixes

        fixes.note_edit(str(seg.get("text") or ""), new, rec_id)
    except Exception:
        log.debug("правка не запомнилась для словаря", exc_info=True)
    old = [w for w in (seg.get("words") or [])
           if isinstance(w, dict) and str(w.get("text") or "").strip()]
    parts = new.split()
    kept = len(old) == len(parts)
    fixed = dict(seg)
    fixed["text"] = new
    fixed["words"] = ([dict(w, text=p) for w, p in zip(old, parts)] if kept else [])
    fixed["edited"] = True                  # текст правлен человеком
    segments[i] = fixed
    store.replace_segments(rec_id, segments)
    store.refresh_participants(rec_id)
    log.info("запись %s: текст реплики правлен%s", rec_id, "" if kept else " (время слов стёрто)")
    return {"changed": True, "kept_times": kept, "segments": store.sorted_segments(rec_id)}


def counts(rec_id: str) -> dict[str, int]:
    """Сколько ручной работы в записи — для предупреждения перед переразбором."""
    segs = store.sorted_segments(rec_id, include_echo=True)
    meta = store.get(rec_id) or {}
    try:
        made = int(meta.get("edits_count") or 0)
    except (TypeError, ValueError):
        made = 0
    return {"edits": made,
            "speakers": sum(1 for s in segs if s.get("by_hand")),
            "texts": sum(1 for s in segs if s.get("edited"))}


def hand_marks(rec_id: str) -> list[dict[str, Any]]:
    """Ручные решения «чьи это слова» — отрезками времени.

    Переразбор собирает реплики заново, и прежние исчезают. Но время звука не
    меняется, поэтому решение человека можно наложить на новые реплики
    (решение 17.09). Правку самого текста так не сохранить: текст
    распознаётся заново.
    """
    out: list[dict[str, Any]] = []
    for seg in store.sorted_segments(rec_id, include_echo=True):
        if not seg.get("by_hand") or not seg.get("speaker_key"):
            continue
        out.append({"track": str(seg.get("track") or ""),
                    "start": float(seg.get("start") or 0.0),
                    "end": float(seg.get("end") or 0.0),
                    "speaker": str(seg.get("speaker") or ""),
                    "speaker_key": str(seg.get("speaker_key") or "")})
    return out


def apply_hand_marks(rec_id: str, marks: list[dict[str, Any]]) -> int:
    """Наложить ручные решения на пересобранную стенограмму. Сколько легло.

    Реплика считается попавшей в ручной отрезок, если внутри него её середина:
    границы после переразбора сдвигаются на доли секунды, а середина остаётся
    там же. Снимок не делается — это продолжение переразбора, а не правка.
    """
    if not marks:
        return 0
    segments = store.sorted_segments(rec_id, include_echo=True)
    hit = 0
    for seg in segments:
        mid = (float(seg.get("start") or 0.0) + float(seg.get("end") or 0.0)) / 2.0
        track = str(seg.get("track") or "")
        for m in marks:
            if m["track"] and m["track"] != track:
                continue
            if not (m["start"] <= mid <= m["end"]):
                continue
            hit += 1
            seg["speaker"] = m["speaker"]
            seg["speaker_key"] = m["speaker_key"]
            seg["speaker_locked"] = True
            seg["by_hand"] = True
            break
    if hit:
        store.replace_segments(rec_id, segments)
        store.refresh_participants(rec_id)
        log.info("запись %s: ручные решения о говорящих наложены на %d реплик", rec_id, hit)
    return hit


def split_segment(rec_id: str, seg_id: str, word_index: int) -> dict[str, Any]:
    """Разрезать реплику перед словом word_index. Обе половины — того же говорящего."""
    segments = store.sorted_segments(rec_id, include_echo=True)
    i = _find(segments, seg_id)
    seg = segments[i]
    words, exact = words_of(seg)
    cut = int(word_index)
    if cut <= 0 or cut >= len(words):
        raise EditError("Разрезать здесь нечего: выберите слово внутри реплики.")
    snapshot(rec_id, "разрез реплики")
    left = _piece(seg, words[:cut], exact)
    right = _piece(seg, words[cut:], exact)
    left["id"] = str(seg.get("id"))          # первая половина остаётся той же репликой
    segments[i:i + 1] = [left, right]
    store.replace_segments(rec_id, segments)
    store.refresh_participants(rec_id)
    log.info("запись %s: реплика разрезана на слове %d%s", rec_id, cut,
             "" if exact else " (время слов неизвестно)")
    return {"exact": exact, "ids": [left["id"], right["id"]],
            "segments": store.sorted_segments(rec_id)}
