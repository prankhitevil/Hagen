# -*- coding: utf-8 -*-
"""Разбор готовых субтитров (.vtt/.srt) в реплики «Hagen».

Зачем это нужно. Teams и Stream кладут рядом с записью готовый .vtt, а иногда
встречу не записывали вовсе и остался ОДИН файл субтитров. Готовый транскрипт
снимает самый долгий шаг обработки — распознавание — и вдобавок приносит
настоящие имена говорящих (тег ``<v Фамилия Имя>``), которых не даёт ни
whisper, ни разметка по голосам.

ГЛАВНОЕ ОТЛИЧИЕ от саммаризатора, откуда этот разбор приехал. Там имя
говорящего вклеивалось прямо в текст реплики («Иванов Иван: реплика»), потому
что дальше был один сплошной текст для модели. Здесь так делать НЕЛЬЗЯ: имя
живёт в отдельных полях сегмента (speaker/speaker_key), а
minutes.build_transcript_text подставляет его сам — и в стенограмме вышло бы
«Иванов: Иванов: реплика». Поэтому текст отдаём чистым, имя — отдельно.

Модуль ничего не скачивает и не запускает: только читает локальный файл.
"""
from __future__ import annotations

import html
import logging
import re
from pathlib import Path
from typing import Any

from . import store

log = logging.getLogger("hagen.subs")

#: Расширения файлов с готовым транскриптом.
TRANSCRIPT_EXTS = frozenset({".vtt", ".srt"})

#: Невидимый маркер кодировки в начале файла: Teams отдаёт VTT именно с ним.
#: Пишем через chr(), чтобы символ был виден в исходнике глазами.
_BOM = chr(0xFEFF)

#: Приставка ключей говорящих, найденных в субтитрах: sub1, sub2, …
#: Ключ должен быть устойчивым внутри файла — по нему store.rename_speaker
#: правит сразу все реплики одного человека.
SPEAKER_KEY_PREFIX = "sub"

# Строка таймкода. Дробная часть намеренно необязательна: WebVTT её всегда
# пишет, а вот чужие .srt из разных программ иногда приходят без миллисекунд,
# и терять из-за этого весь файл («нет реплик с таймкодами») обидно.
_TC_LINE = re.compile(
    r"(?P<a>\d{1,3}:\d{2}(?::\d{2})?(?:[.,]\d{1,3})?)\s*-->\s*"
    r"(?P<b>\d{1,3}:\d{2}(?::\d{2})?(?:[.,]\d{1,3})?)"
)

# <v Иванов Иван> и <v.loud Иванов Иван> — так Teams помечает говорящего.
_SPEAKER_TAG = re.compile(r"<v(?:\.[^\s>]+)*\s+([^>]*?)/?>", re.I)
_ANY_TAG = re.compile(r"<[^>]*>")
_SPACES = re.compile(r"\s+")

# Признак «ползущих» автосубтитров YouTube: караоке-теги <c> и таймкоды
# отдельных слов внутри реплики.
_ROLLING_RE = re.compile(r"<c[.>]|<\d{1,3}:\d{2}:\d{2}\.\d{1,3}>")

# «Иванов Иван: реплика» — второй способ пометить говорящего, так делают
# расшифровки из сторонних сервисов. Двоеточие ищем только в начале строки и
# только близко к началу: длинный «заголовок» именем не бывает.
_LINE_SPEAKER = re.compile(r"^([^:]{1,40}):\s+(\S.*)$")

# Знаки, которых в имени не бывает. Точка разрешена ради инициалов «Иванов И.И.»,
# а вот запятая сразу выдаёт обычную фразу («Итак, коллеги: начнём»).
_BAD_IN_NAME = set(',!?;()[]{}"«»<>/\\|@#$%^&*=+~`…')

# Слова, которые выглядят как имя, но именем не являются. Список короткий и
# только из явного: остальное отсеивает правило «одиночное слово должно
# повториться» (см. _apply_line_speakers).
_NOT_A_NAME = {
    "итак", "важно", "внимание", "примечание", "например", "музыка", "смех",
    "аплодисменты", "субтитры", "реклама", "продолжение", "note", "music",
}


# ---------------------------------------------------------------- мелкие помощники


def _check_cancel(handle: Any) -> None:
    if handle is not None and getattr(handle, "cancelled", False):
        raise RuntimeError("отменено")


def _note(handle: Any, value: float, text: str) -> None:
    """Отчёт о прогрессе. Задача не должна падать из-за сломанного handle."""
    if handle is None:
        return
    try:
        handle.progress(value, text)
    except Exception:
        pass


def _shown_name(path: Any) -> str:
    """Имя файла, пригодное для лога и текста ошибки: без хвоста ссылки.

    Путь может прийти собранным из преавторизованной ссылки SharePoint, и тогда
    в Path(...).name сидит «?tempauth=…» — то есть живой токен. В лог и в
    сообщение об ошибке он попасть не должен ни при каких условиях, поэтому
    хвост запроса режем прямо здесь. obsidian._clean_url тут не подходит: он
    умеет только http/https-ссылки и на локальном пути вернул бы пустую строку.
    """
    return Path(path).name.split("?", 1)[0].split("#", 1)[0].strip() or "без имени"


def is_transcript_name(name: Any) -> bool:
    """Похоже ли имя файла (или хвост ссылки) на готовый транскрипт."""
    s = str(name or "").strip().lower()
    # У преавторизованной ссылки SharePoint после имени файла идёт хвост
    # «?tempauth=…»; проверяем именно имя, а не всю строку целиком.
    s = s.split("?", 1)[0].split("#", 1)[0].rstrip()
    return any(s.endswith(ext) for ext in TRANSCRIPT_EXTS)


def tc_to_sec(tc: str) -> float:
    """«00:01:02.500», «01:02.500» (VTT допускает ММ:СС), SRT с запятой -> секунды."""
    parts = str(tc or "").strip().replace(",", ".").split(":")
    if len(parts) == 2:              # ММ:СС.мс — часы опущены
        parts = ["0"] + parts
    if len(parts) != 3:
        raise ValueError("непонятный таймкод: %r" % (tc,))
    return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])


# ---------------------------------------------------------------- разбор текста


def parse_cues(text: str) -> list[dict[str, Any]]:
    """WebVTT/SRT -> [{'start','end','text','speaker'}].

    Заголовок WEBVTT, блоки NOTE/STYLE, номера реплик SRT и вся разметка
    (<v>, <c>, <i>) отбрасываются.

    Тело реплики берётся до ПУСТОЙ строки, даже если внутри попалась стрелка
    «-->». Это сознательный выбор: стрелка внутри текста («перенесли с 10:00
    --> 11:30») встречается чаще, чем файл, где между репликами забыли пустую
    строку.
    """
    # BOM убираем ещё раз, хотя его снимает и utf-8-sig: parse_cues зовут и
    # на текст, пришедший не из файла.
    lines = (str(text or "").replace(_BOM, "")
             .replace("\r\n", "\n").replace("\r", "\n").split("\n"))
    cues: list[dict[str, Any]] = []
    i = 0
    while i < len(lines):
        m = _TC_LINE.search(lines[i])
        if not m:
            i += 1
            continue
        try:
            start, end = tc_to_sec(m.group("a")), tc_to_sec(m.group("b"))
        except ValueError:
            i += 1
            continue
        i += 1
        buf: list[str] = []
        while i < len(lines) and lines[i].strip():
            buf.append(lines[i])
            i += 1
        raw = "\n".join(buf)
        sm = _SPEAKER_TAG.search(raw)
        speaker = _ANY_TAG.sub("", sm.group(1)).strip() if sm else ""
        body = _SPACES.sub(" ", html.unescape(_ANY_TAG.sub("", raw))).strip()
        if body:
            # end < start бывает в кривых файлах: не даём отрицательной длине
            # уехать в стенограмму.
            cues.append({"start": start, "end": max(end, start),
                         "text": body, "speaker": speaker})
    return _apply_line_speakers(cues)


def _looks_like_name(prefix: str) -> bool:
    """Похоже ли начало строки до двоеточия на имя человека."""
    words = prefix.split()
    if not words or len(words) > 4:
        return False
    if prefix.lower() in _NOT_A_NAME:
        return False
    if any(ch in _BAD_IN_NAME for ch in prefix):
        return False
    first = words[0][:1]
    if not (first.isalpha() and first.isupper()):
        return False
    for w in words[1:]:
        c = w[:1]
        # «Иванов Иван», «Иванов И.И.», «SPEAKER 1» — да; «Иван из Москвы» — нет
        if not (c.isdigit() or (c.isalpha() and c.isupper())):
            return False
    return True


def _apply_line_speakers(cues: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Имя из строки «Иванов Иван: реплика» — там, где тега <v> нет.

    Почему в два прохода. Одиночное «Итак: поехали» с виду ничем не отличается
    от имени и превращалось в говорящего «Итак». Поэтому кандидат принимается,
    только если он либо повторяется в файле, либо состоит из нескольких слов
    («Иванов Иван»), либо написан заглавными («АННА»). Одинокое слово с
    заглавной буквы остаётся частью текста — потерять имя не так обидно, как
    завести в стенограмме говорящего «Итак».
    """
    found: list[tuple[int, str, str]] = []
    counts: dict[str, int] = {}
    for idx, c in enumerate(cues):
        if c.get("speaker"):
            continue                 # тег <v> главнее: он от самого сервиса
        m = _LINE_SPEAKER.match(c["text"])
        if not m:
            continue
        name, rest = m.group(1).strip(), m.group(2).strip()
        if not rest or not _looks_like_name(name):
            continue
        found.append((idx, name, rest))
        counts[name] = counts.get(name, 0) + 1
    for idx, name, rest in found:
        if counts[name] >= 2 or len(name.split()) >= 2 or (len(name) > 1 and name.isupper()):
            cues[idx]["speaker"] = name
            cues[idx]["text"] = rest
    return cues


def looks_rolling(text: str) -> bool:
    """Признак «ползущих» субтитров (автосубтитры YouTube).

    У таких каждая следующая реплика повторяет предыдущую целиком: час
    автосубтитров — это 3804 наезжающие друг на друга реплики.
    """
    return bool(_ROLLING_RE.search(str(text or "")))


def dedup_rolling(cues: list[dict[str, Any]], max_overlap: int = 15) -> list[dict[str, Any]]:
    """Схлопывает повторы «ползущих» субтитров.

    Реплика, целиком содержащаяся в предыдущей, отбрасывается, а общий
    «хвост-начало» (перекрытие по словам) срезается. На часе автосубтитров
    YouTube это 3804 реплики -> 118 нормальных.
    """
    out: list[dict[str, Any]] = []
    for c in cues:
        if not out:
            out.append(dict(c))
            continue
        p, t = out[-1], c["text"]
        if t in p["text"]:
            p["end"] = max(p["end"], c["end"])
            continue
        pw, cw = p["text"].split(), t.split()
        k = min(len(pw), len(cw), max_overlap)
        while k > 0 and pw[-k:] != cw[:k]:
            k -= 1
        if k:
            cw = cw[k:]
            if not cw:
                p["end"] = max(p["end"], c["end"])
                continue
            t = " ".join(cw)
        out.append({**c, "text": t})
    return out


def merge_cues(cues: list[dict[str, Any]], gap: float = 2.0,
               max_chars: int = 600) -> list[dict[str, Any]]:
    """Склеивает соседние реплики одного говорящего.

    Субтитры нарезаны по 1-3 секунды, а в стенограмме нужен связный текст.
    Дубли подряд (такие даёт rolling-режим Teams) не копятся.
    """
    out: list[dict[str, Any]] = []
    for c in cues:
        p = out[-1] if out else None
        if (p and c["speaker"] == p["speaker"] and c["start"] - p["end"] <= gap
                and len(p["text"]) + len(c["text"]) <= max_chars):
            if c["text"] not in p["text"]:
                p["text"] = (p["text"] + " " + c["text"]).strip()
            p["end"] = max(p["end"], c["end"])
            continue
        out.append(dict(c))
    return out


def _read_text_any(path: Any) -> str:
    """VTT от Teams — UTF-8 (обычно с BOM); чужие .srt бывают в cp1251.

    Порядок кодировок важен: cp1251 «переваривает» почти любые байты и молча
    превратил бы русский UTF-8 в кракозябры, поэтому он идёт вторым.

    UTF-16 разбираем по метке в первых двух байтах, до перебора: такой файл
    тоже «читается» как cp1251 — только каждая вторая буква становится нулевым
    символом, реплик находится ноль, и никакой ошибки при этом не видно.
    """
    data = Path(path).read_bytes()
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        try:
            return data.decode("utf-16")
        except UnicodeDecodeError:
            pass
    for enc in ("utf-8-sig", "cp1251"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


# ---------------------------------------------------------------- публичные функции


def load_cues(path: Any, handle: Any = None) -> list[dict[str, Any]]:
    """Файл .vtt/.srt -> список реплик [{'start','end','text','speaker'}].

    В саммаризаторе это называлось load_transcript; здесь такое имя занято
    store.load_transcript (она читает стенограмму записи), поэтому разбор
    файла назван иначе.
    """
    p = Path(path)
    try:
        exists = p.is_file()
    except OSError:
        exists = False
    if not exists:
        raise RuntimeError("Файл субтитров не найден: %s" % _shown_name(p))

    _note(handle, 0.05, "читаю файл субтитров")
    raw = _read_text_any(p)
    _check_cancel(handle)

    cues = parse_cues(raw)
    _note(handle, 0.5, "разбираю реплики")
    if looks_rolling(raw):           # автосубтитры: сперва убрать наезды
        was = len(cues)
        cues = dedup_rolling(cues)
        log.info("автосубтитры %s: было %d наезжающих реплик, осталось %d",
                 _shown_name(p), was, len(cues))
    _check_cancel(handle)

    cues = merge_cues(cues)
    _note(handle, 0.9, "склеиваю короткие реплики")
    return cues


def load_segments(path: Any, handle: Any = None) -> list[dict[str, Any]]:
    """Готовые субтитры -> сегменты в формате хранилища (для store.replace_segments).

    Имя говорящего кладётся в поля speaker/speaker_key и НЕ вклеивается в
    текст: иначе minutes подставит его второй раз.

    У реплики без имени говорящий не выдумывается — store.make_segment
    подпишет её обычным «Участником» дорожки файла, а уточнить можно
    разметкой по голосам или руками.
    """
    p = Path(path)
    cues = load_cues(p, handle)
    if not cues:
        raise RuntimeError(
            "В файле %s нет реплик с таймкодами — это точно .vtt или .srt?"
            % _shown_name(p))

    keys: dict[str, str] = {}
    segments: list[dict[str, Any]] = []
    for c in cues:
        name = (c.get("speaker") or "").strip()
        key = None
        if name:
            key = keys.get(name)
            if key is None:
                # нумеруем по первому появлению: один человек — один ключ на весь файл
                key = "%s%d" % (SPEAKER_KEY_PREFIX, len(keys) + 1)
                keys[name] = key
        segments.append(store.make_segment(
            track=store.TRACK_FILE,
            start=c["start"],
            end=c["end"],
            text=c["text"],
            speaker=name or None,
            speaker_key=key,
        ))
    _note(handle, 1.0, "субтитры разобраны")
    log.info("субтитры %s: реплик — %d, говорящих — %d",
             _shown_name(p), len(segments), len(keys))
    return segments


def speakers_from_segments(segments: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Говорящие для meta.json: {"sub1": {"name": "Иванов Иван", …}}.

    confirmed=True намеренно: имя пришло из самого сервиса (Teams пишет в
    субтитры настоящие имена участников), и подбор по голосам не должен
    молча его перебить.

    Ключи «far»/«me» сюда не попадают: «Участник» — это не имя, а заглушка.
    """
    out: dict[str, dict[str, Any]] = {}
    for seg in segments or []:
        if not isinstance(seg, dict):
            continue
        key = str(seg.get("speaker_key") or "")
        name = str(seg.get("speaker") or "").strip()
        if not key.startswith(SPEAKER_KEY_PREFIX) or not name or key in out:
            continue
        out[key] = {"name": name, "person_id": None, "score": None,
                    "suggestion": None, "confirmed": True}
    return out


def probe(path: Any) -> dict[str, Any]:
    """Что внутри файла субтитров: сколько реплик, кто говорит, сколько длится.

    Считает то же, что и load_segments, но ничего никуда не пишет. На файле
    без реплик не падает — возвращает нули, чтобы вызывающий сам решил,
    годится файл или нужно обычное распознавание.
    """
    cues = load_cues(path)
    speakers = list(dict.fromkeys(
        (c.get("speaker") or "").strip() for c in cues if (c.get("speaker") or "").strip()))
    return {
        "cues": len(cues),
        "speakers": speakers,
        "duration_s": round(max((float(c["end"]) for c in cues), default=0.0), 3),
    }
