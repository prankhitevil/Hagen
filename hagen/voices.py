# -*- coding: utf-8 -*-
"""База голосов: люди, варианты их имён и образцы голоса.

Смысл: один раз подписал «Спикер 2» как «Иван Петров» — дальше его реплики
в новых записях подписываются сами.

Данные лежат ТОЛЬКО локально, в файле data/voices.json на этой машине.
Никуда не отправляются: ни в облако, ни в API документов, ни в логи.
В логи пишутся только имена и числа близости, сами векторы — никогда.

Как устроено (версия 2, решения 13.09.2026). Человек, варианты его
имени и образцы голоса хранятся раздельно — так в разработке обычно решают
«сопоставление сущностей»:
  * точное совпадение имени связывается само, похожее — только предлагается;
  * порядок слов, регистр, «ё/е» и приписки в скобках не важны:
    «Петров Иван» = «Иван Петров (ООО Ромашка)»;
  * у каждого образца голоса записано, откуда он: запись, говорящий, способ.
    Поэтому ошибку можно отменить — образец убирается ровно у того, кому попал;
  * человек может быть и без голоса (имя из расшифровки Teams без звука);
  * «общее устройство» (переговорка, один компьютер на двоих) — голос такой
    учётки не учится и никому не подставляется;
  * перед объединением и удалением людей база копируется в data/voices-backups.

Формат файла:
  {"version": 2,
   "people": {
      "<person_id>": {"name": "Иван Петров",
                      "aliases": ["Petrov Ivan"],
                      "kind": "person" | "shared",
                      "owner": false,               # владелец записи («Я»)
                      "not_same": ["<id>", ...],    # «это разные люди»
                      "samples": [{"v": [256 float], "rec_id": ..., "speaker_key": ...,
                                   "origin": "manual"|"teams"|"legacy", "added_at": iso}],
                      "centroid": [256 float],      # среднее нормированных образцов
                      "created_at": iso, "updated_at": iso, "count": int}}}

person_id — стабильный слаг от имени плюс короткий хеш. Он присваивается один
раз при создании и при переименовании НЕ меняется, поэтому ссылки из meta.json
записей не рвутся.

Про векторы pyannote: норма около 3.2, размерность 256 у модели
speaker-diarization-community-1. Перед сравнением всё нормируется, а битые
векторы (не та размерность, NaN, inf, нулевая норма) молча игнорируются.
"""
from __future__ import annotations

import difflib
import hashlib
import io
import json
import logging
import os
import re
import shutil
import threading
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

import numpy as np

from . import config, store

log = logging.getLogger("hagen.voices")

VOICES_PATH = config.DATA_DIR / "voices.json"

VERSION = 2
MAX_SAMPLES = 12          # больше не нужно: центроид перестаёт меняться
EXPECTED_DIM = 256        # размерность эмбеддинга community-1
MIN_DIM = 16              # защита от мусора вроде списка из трёх чисел
MAX_DIM = 4096
KEEP_BACKUPS = 20

KIND_PERSON = "person"
KIND_SHARED = "shared"
KINDS = (KIND_PERSON, KIND_SHARED)

ORIGINS = {
    "manual": "подписан вручную",
    "teams": "из расшифровки Teams",
    "legacy": "из прежней базы",
}

#: Имя учётки, похожее на переговорку: такой человек сразу заводится как
#: «общее устройство», и его голос не учится.
_ROOM_RE = re.compile(r"переговорн|конференц|meeting\s*room|conference|\broom\b|\bзал\b",
                      re.IGNORECASE)

_lock = threading.RLock()
_cache: dict[str, Any] | None = None
_cache_stamp: tuple[float, int] | None = None

# транслитерация для слага и для сравнения кириллицы с латиницей
_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "h", "ц": "c", "ч": "ch", "ш": "sh", "щ": "sch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}

#: Упрощения латиницы после транслитерации: разные системы записывают одно имя
#: по-разному (Petrov/Petroff, Yulia/Julia/Iuliia). Применяются к обеим сторонам.
_LOOSE = (("shch", "sh"), ("sch", "sh"), ("tch", "ch"), ("kh", "h"), ("ts", "c"),
          ("tz", "c"), ("zh", "j"), ("ya", "ia"), ("yu", "iu"), ("yo", "io"),
          ("ye", "e"), ("y", "i"), ("j", "i"), ("w", "v"), ("x", "ks"), ("q", "k"))


class NameTaken(ValueError):
    """Имя уже занято другим человеком. В .other — этот человек (без векторов)."""

    def __init__(self, message: str, other: dict[str, Any]):
        super().__init__(message)
        self.other = other


# ---------------------------------------------------------------- служебное


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _norm_name(name: Any) -> str:
    """Имя для показа: схлопнутые пробелы, без краёв."""
    return re.sub(r"\s+", " ", str(name or "")).strip()


_BRACKETS = re.compile(r"[\(\[\{][^\)\]\}]*[\)\]\}]")


def name_tokens(name: Any) -> list[str]:
    """Слова имени для сравнения: без регистра, «ё» как «е», без приписок в скобках."""
    s = unicodedata.normalize("NFKC", str(name or ""))
    s = _BRACKETS.sub(" ", s)
    s = s.casefold().replace("ё", "е")
    s = re.sub(r"[^\w\-]+", " ", s).replace("_", " ")
    return [t.strip("-") for t in s.split() if t.strip("-")]


def name_key(name: Any) -> str:
    """Ключ имени: слова по алфавиту — «Петров Иван» и «Иван Петров» совпадают."""
    return " ".join(sorted(name_tokens(name)))


# старое имя функции: ключ поиска по имени
_name_key = name_key


def _loose_token(tok: str) -> str:
    out = "".join(_TRANSLIT.get(ch, ch) for ch in tok)
    for a, b in _LOOSE:
        out = out.replace(a, b)
    return re.sub(r"(.)\1+", r"\1", out)


def _loose_key(tokens: list[str]) -> str:
    return " ".join(sorted(_loose_token(t) for t in tokens))


def _slug(name: str) -> str:
    """ASCII-слаг имени. Пустой результат заменяем на «person»."""
    low = unicodedata.normalize("NFKC", _norm_name(name)).lower()
    out: list[str] = []
    for ch in low:
        if ch in _TRANSLIT:
            out.append(_TRANSLIT[ch])
        elif ch.isascii() and (ch.isalnum()):
            out.append(ch)
        else:
            out.append("-")
    slug = re.sub(r"-+", "-", "".join(out)).strip("-")
    return slug[:40] or "person"


def _make_person_id(name: str, taken: Iterable[str] = ()) -> str:
    """Стабильный id: слаг + 6 знаков хеша от ключа имени."""
    digest = hashlib.sha1(name_key(name).encode("utf-8")).hexdigest()
    base = "%s-%s" % (_slug(name), digest[:6])
    taken = set(taken)
    if base not in taken:
        return base
    for i in range(2, 100):
        cand = "%s-%d" % (base, i)
        if cand not in taken:
            return cand
    return "%s-%s" % (base, digest[6:12])


def looks_like_room(name: Any) -> bool:
    """Похоже на учётку переговорки."""
    return bool(_ROOM_RE.search(str(name or "")))


def _vec(embedding: Any, expect_dim: int | None = EXPECTED_DIM) -> np.ndarray | None:
    """Привести эмбеддинг к одномерному float32. None, если вектор негодный."""
    if embedding is None:
        return None
    if isinstance(embedding, dict):
        embedding = embedding.get("v")
    try:
        arr = np.asarray(embedding, dtype=np.float64).reshape(-1)
    except (TypeError, ValueError):
        return None
    if arr.size < MIN_DIM or arr.size > MAX_DIM:
        return None
    if expect_dim is not None and arr.size != int(expect_dim):
        return None
    if not np.all(np.isfinite(arr)):
        return None
    if float(np.linalg.norm(arr)) <= 1e-9:
        return None
    return np.ascontiguousarray(arr.astype(np.float32))


def _unit(arr: np.ndarray) -> np.ndarray:
    n = float(np.linalg.norm(arr))
    if n <= 1e-9:
        return arr.astype(np.float32)
    return np.ascontiguousarray((arr / n).astype(np.float32))


def _as_list(arr: np.ndarray) -> list[float]:
    """Вектор в JSON: округляем до 6 знаков, файл и так немаленький."""
    return [round(float(x), 6) for x in np.asarray(arr).reshape(-1)]


# ---------------------------------------------------------------- хранилище


def _empty_db() -> dict[str, Any]:
    return {"version": VERSION, "people": {}}


def _sample(raw: Any, dim: int | None) -> dict[str, Any] | None:
    """Образец из файла: старый формат — голый список чисел."""
    meta: dict[str, Any] = raw if isinstance(raw, dict) else {"v": raw, "origin": "legacy"}
    v = _vec(meta.get("v"), expect_dim=dim)
    if v is None:
        return None
    origin = str(meta.get("origin") or "legacy")
    return {
        "v": _as_list(v),
        "rec_id": str(meta["rec_id"]) if meta.get("rec_id") else None,
        "speaker_key": str(meta["speaker_key"]) if meta.get("speaker_key") else None,
        "origin": origin if origin in ORIGINS else "legacy",
        "added_at": str(meta.get("added_at") or ""),
    }


def _sanitize(raw: Any) -> dict[str, Any]:
    """Привести прочитанный JSON к рабочему виду, выкинув мусор."""
    db = _empty_db()
    if not isinstance(raw, dict):
        return db
    people_raw = raw.get("people")
    if not isinstance(people_raw, dict):
        return db
    people: dict[str, Any] = {}
    for pid, rec in people_raw.items():
        if not isinstance(rec, dict):
            continue
        name = _norm_name(rec.get("name"))
        if not str(pid) or not name:
            continue
        dim: int | None = None
        samples: list[dict[str, Any]] = []
        for s in rec.get("samples") or []:
            smp = _sample(s, dim)
            if smp is None:
                continue
            dim = len(smp["v"])
            samples.append(smp)
        samples = samples[-MAX_SAMPLES:]
        key = name_key(name)
        aliases: list[str] = []
        seen = {key}
        for a in rec.get("aliases") or []:
            a = _norm_name(a)
            if a and name_key(a) not in seen:
                seen.add(name_key(a))
                aliases.append(a)
        kind = str(rec.get("kind") or KIND_PERSON)
        entry = {
            "name": name,
            "aliases": aliases,
            "kind": kind if kind in KINDS else KIND_PERSON,
            "owner": bool(rec.get("owner")),
            "not_same": [str(x) for x in (rec.get("not_same") or []) if str(x)],
            "samples": samples,
            "centroid": [],
            "created_at": str(rec.get("created_at") or _now_iso()),
            "updated_at": str(rec.get("updated_at") or _now_iso()),
            "count": len(samples),
        }
        if samples:
            entry["centroid"] = _as_list(_centroid(samples))
        people[str(pid)] = entry
    db["people"] = people
    return db


def _centroid(samples: list[Any]) -> np.ndarray:
    """Среднее нормированных образцов, само приведённое к единичной норме."""
    vecs = []
    dim: int | None = None
    for s in samples:
        v = _vec(s, expect_dim=dim)
        if v is None:
            continue
        dim = int(v.size)
        vecs.append(_unit(v))
    if not vecs:
        return np.zeros(0, dtype=np.float32)
    return _unit(np.mean(np.stack(vecs, axis=0), axis=0))


def _stamp() -> tuple[float, int] | None:
    """Отпечаток файла для кеша: время правки и размер (одного времени мало)."""
    try:
        st = VOICES_PATH.stat()
    except OSError:
        return None
    return (float(st.st_mtime), int(st.st_size))


def backups_dir() -> Path:
    return VOICES_PATH.parent / "voices-backups"


def _backup(reason: str) -> str | None:
    """Копия базы перед необратимым шагом. Хранятся последние KEEP_BACKUPS."""
    if not VOICES_PATH.exists():
        return None
    folder = backups_dir()
    try:
        folder.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        target = folder / ("voices-%s-%s.json" % (stamp, re.sub(r"[^\w-]+", "-", reason)[:30]))
        shutil.copy2(VOICES_PATH, target)
        old = sorted(folder.glob("voices-*.json"))
        for extra in old[:-KEEP_BACKUPS]:
            try:
                extra.unlink()
            except OSError:
                pass
        return target.name
    except OSError as err:
        log.warning("копия базы голосов не сделана: %s", err)
        return None


def _keep_broken_file() -> str | None:
    """Отложить непрочитанную базу в сторону, чтобы её не затёрла первая запись."""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = VOICES_PATH.with_name("voices-broken-%s.json" % stamp)
    try:
        os.replace(VOICES_PATH, target)
    except OSError:
        return None
    return target.name


def _read_db() -> dict[str, Any]:
    """Прочитать базу с диска. Кеш сбрасывается, если файл изменился извне."""
    global _cache, _cache_stamp
    with _lock:
        stamp = _stamp()
        if _cache is not None and stamp == _cache_stamp:
            return _cache
        raw: Any = None
        if stamp is not None:
            try:
                # utf-8-sig, а не utf-8: Блокнот и PowerShell дописывают в начало
                # файла BOM, и на обычном utf-8 вся база выглядела бы битой
                with io.open(VOICES_PATH, "r", encoding="utf-8-sig") as fh:
                    raw = json.load(fh)
            except (json.JSONDecodeError, OSError, UnicodeError, ValueError) as err:
                raw = None
                backup = _keep_broken_file()
                stamp = _stamp()
                log.warning(
                    "База голосов %s не читается (%s). Начинаю с пустой. "
                    "Испорченный файл сохранён рядом как «%s», имена из него можно "
                    "вернуть руками.",
                    VOICES_PATH.name, err,
                    backup or "— отложить не удалось, файл будет перезаписан")
        migrate = isinstance(raw, dict) and bool(raw.get("people")) and \
            int(raw.get("version") or 1) < VERSION
        if migrate:
            # переход на новый формат: старый файл сохраняется целиком, новый
            # пишется сразу — иначе копия делалась бы при каждом чтении
            kept = _backup("v%s" % (raw.get("version") or 1))
            log.info("база голосов переходит на версию %d, копия старой: %s", VERSION, kept)
        _cache = _sanitize(raw)
        _cache_stamp = stamp
        if migrate:
            _write_db(_cache)
        return _cache


def _write_db(db: dict[str, Any]) -> None:
    """Атомарная запись: пишем .tmp и подменяем файл одним os.replace."""
    global _cache, _cache_stamp
    with _lock:
        db["version"] = VERSION
        VOICES_PATH.parent.mkdir(parents=True, exist_ok=True)
        config.atomic_json(VOICES_PATH, db, indent=1)
        _cache = db
        _cache_stamp = _stamp()


def reload() -> dict[str, Any]:
    """Сбросить кеш и перечитать файл (после правки базы снаружи)."""
    global _cache, _cache_stamp
    with _lock:
        _cache = None
        _cache_stamp = None
        return _read_db()


def path() -> Path:
    """Путь к файлу базы голосов."""
    return VOICES_PATH


# ---------------------------------------------------------------- чтение


def _public(pid: str, rec: dict[str, Any]) -> dict[str, Any]:
    """Запись о человеке без векторов — в таком виде её можно отдавать в API."""
    return {
        "id": pid,
        "name": rec.get("name") or "",
        "aliases": list(rec.get("aliases") or []),
        "kind": rec.get("kind") or KIND_PERSON,
        "owner": bool(rec.get("owner")),
        "count": int(rec.get("count") or len(rec.get("samples") or [])),
        "has_voice": bool(rec.get("centroid")),
        "created_at": rec.get("created_at"),
        "updated_at": rec.get("updated_at"),
    }


def _people() -> dict[str, Any]:
    return _read_db().get("people") or {}


def list_people() -> list[dict[str, Any]]:
    """Список людей для интерфейса. Без векторов."""
    with _lock:
        out = [_public(pid, rec) for pid, rec in _people().items()]
    out.sort(key=lambda p: (not p["owner"], name_key(p.get("name"))))
    return out


def get_person(person_id: str) -> dict[str, Any] | None:
    """Человек по id: те же поля плюс размерность голоса. Векторы не отдаются."""
    with _lock:
        rec = _people().get(str(person_id))
        if not isinstance(rec, dict):
            return None
        out = _public(str(person_id), rec)
        out["dim"] = len(rec.get("centroid") or [])
        return out


def _resolve_id(name: Any, people: dict[str, Any] | None = None) -> str | None:
    key = name_key(name)
    if not key:
        return None
    people = _people() if people is None else people
    for pid, rec in people.items():
        if name_key(rec.get("name")) == key:
            return str(pid)
    for pid, rec in people.items():
        if any(name_key(a) == key for a in rec.get("aliases") or []):
            return str(pid)
    return None


def resolve(name: str) -> dict[str, Any] | None:
    """Человек, у которого это имя — основное или вариант (порядок слов не важен)."""
    with _lock:
        pid = _resolve_id(name)
        return _public(pid, _people()[pid]) if pid else None


def find_by_name(name: str) -> dict[str, Any] | None:
    """Поиск человека по имени. То же, что resolve (оставлено для старых вызовов)."""
    return resolve(name)


def stats() -> dict[str, Any]:
    """Сводка для интерфейса: сколько людей, сколько образцов, где лежит база."""
    with _lock:
        people = _people()
        samples = sum(len(rec.get("samples") or []) for rec in people.values())
        return {"people": len(people), "samples": samples, "path": str(VOICES_PATH)}


# ---------------------------------------------------------------- похожие имена


def _similar_reason(a: str, b: str) -> str | None:
    """Почему имена a и b могут быть одним человеком. None — не похожи.

    Одинаковый ключ (порядок слов) сюда не попадает: это просто одно имя.
    """
    ta, tb = name_tokens(a), name_tokens(b)
    if not ta or not tb:
        return None
    ka, kb = " ".join(sorted(ta)), " ".join(sorted(tb))
    if ka == kb:
        return None
    if _loose_key(ta) == _loose_key(tb):
        return "то же имя другими буквами"
    # инициалы: «Петров И.» и «Иван Петров»
    for short, full in ((ta, tb), (tb, ta)):
        initials = [t for t in short if len(t) == 1]
        words = [t for t in short if len(t) > 1]
        if initials and words and set(words) <= set(full):
            rest = [t for t in full if t not in words]
            if len(rest) >= len(initials) and all(any(r.startswith(i) for r in rest) for i in initials):
                return "инициалы"
    sa, sb = set(ta), set(tb)
    if sa < sb or sb < sa:
        common = sa & sb
        if len(common) >= 2 or any(len(t) >= 4 for t in common):
            return "часть имени совпадает" if len(common) == 1 else "лишнее слово (например, отчество)"
    if len(ka) >= 6 and difflib.SequenceMatcher(None, ka, kb).ratio() >= 0.86:
        return "похоже написано"
    la, lb = _loose_key(ta), _loose_key(tb)
    if len(la) >= 6 and difflib.SequenceMatcher(None, la, lb).ratio() >= 0.9:
        return "похоже написано"
    return None


def similar_people(name: str, exclude: Iterable[str] = ()) -> list[dict[str, Any]]:
    """Люди с похожим, но не тем же именем — кандидаты на вопрос «это он?»."""
    exclude = {str(x) for x in exclude}
    out: list[dict[str, Any]] = []
    with _lock:
        for pid, rec in _people().items():
            if str(pid) in exclude:
                continue
            best = None
            for nm in [rec.get("name")] + list(rec.get("aliases") or []):
                reason = _similar_reason(name, nm)
                if reason:
                    best = reason
                    break
            if best:
                item = _public(str(pid), rec)
                item["reason"] = best
                out.append(item)
    out.sort(key=lambda p: name_key(p["name"]))
    return out


def possible_duplicates() -> list[dict[str, Any]]:
    """Пары людей с похожими именами, которых ещё не отметили «разные люди»."""
    pairs: list[dict[str, Any]] = []
    with _lock:
        items = list(_people().items())
        for i, (pa, ra) in enumerate(items):
            for pb, rb in items[i + 1:]:
                if pb in (ra.get("not_same") or []) or pa in (rb.get("not_same") or []):
                    continue
                if ra.get("owner") or rb.get("owner"):
                    continue
                reason = None
                for na in [ra.get("name")] + list(ra.get("aliases") or []):
                    for nb in [rb.get("name")] + list(rb.get("aliases") or []):
                        reason = _similar_reason(na, nb)
                        if reason:
                            break
                    if reason:
                        break
                if reason:
                    pairs.append({"a": _public(pa, ra), "b": _public(pb, rb), "reason": reason})
    return pairs


# ---------------------------------------------------------------- поиск для окна имени


def search(query: str, embedding: Any = None, limit: int = 12,
           include_owner: bool = False) -> list[dict[str, Any]]:
    """Люди для списка подсказок: по вхождению символов в любое слово имени.

    Набранное «пет ив» найдёт «Иван Петров»; латиница находит кириллицу и
    наоборот. Если передан голос говорящего — у каждого человека есть процент
    похожести, и похожие по голосу стоят выше. Без текста — сначала похожие
    по голосу, потом недавние.
    """
    q = name_tokens(query)
    ql = [_loose_token(t) for t in q]
    vec = _vec(embedding, expect_dim=None) if embedding is not None else None
    out: list[dict[str, Any]] = []
    with _lock:
        for pid, rec in _people().items():
            if rec.get("owner") and not include_owner:
                continue
            best_rank: int | None = None
            matched: str | None = None
            for nm in [rec.get("name")] + list(rec.get("aliases") or []):
                toks = name_tokens(nm)
                loose = [_loose_token(t) for t in toks]
                rank = 0
                ok = True
                for qt, qlt in zip(q, ql):
                    if any(t.startswith(qt) for t in toks):
                        rank += 3
                    elif any(qt in t for t in toks):
                        rank += 2
                    elif any(lt.startswith(qlt) or (len(qlt) >= 3 and qlt in lt) for lt in loose):
                        rank += 1
                    else:
                        ok = False
                        break
                if ok and (best_rank is None or rank > best_rank):
                    best_rank, matched = rank, nm
            if q and best_rank is None:
                continue
            item = _public(str(pid), rec)
            item["matched"] = matched if matched and matched != rec.get("name") else None
            item["rank"] = best_rank or 0
            voice = None
            cent = rec.get("centroid") or []
            if vec is not None and len(cent) == vec.size and rec.get("kind") != KIND_SHARED:
                voice = round(cosine(vec, cent), 4)
            item["voice"] = voice
            out.append(item)
    if q:
        out.sort(key=lambda p: (-p["rank"], -(p["voice"] or 0.0), name_key(p["name"])))
    else:
        # без текста: похожие по голосу, затем недавно подписанные
        out.sort(key=lambda p: str(p.get("updated_at") or ""), reverse=True)
        out.sort(key=lambda p: -(p["voice"] or 0.0))
    return out[:max(1, int(limit))]


# ---------------------------------------------------------------- правка


def _new_entry(name: str, kind: str = KIND_PERSON, owner: bool = False) -> dict[str, Any]:
    now = _now_iso()
    return {"name": name, "aliases": [], "kind": kind if kind in KINDS else KIND_PERSON,
            "owner": bool(owner), "not_same": [], "samples": [], "centroid": [],
            "created_at": now, "updated_at": now, "count": 0}


def _unique_name(name: str, people: dict[str, Any]) -> str:
    if _resolve_id(name, people) is None:
        return name
    for i in range(2, 100):
        cand = "%s %d" % (name, i)
        if _resolve_id(cand, people) is None:
            return cand
    return "%s %s" % (name, datetime.now().strftime("%H%M%S"))


def create_person(name: str, kind: str | None = None, namesake: bool = False) -> dict[str, Any]:
    """Завести человека. namesake=True — тёзка: к имени добавится номер («Иван Петров 2»)."""
    nm = _norm_name(name)
    if not nm or not name_tokens(nm):
        raise ValueError("имя не может быть пустым")
    with _lock:
        db = _read_db()
        people = dict(db.get("people") or {})
        existing = _resolve_id(nm, people)
        if existing and not namesake:
            raise NameTaken("«%s» уже есть в базе" % nm, _public(existing, people[existing]))
        if namesake:
            nm = _unique_name(nm, people)
        kind = kind or (KIND_SHARED if looks_like_room(nm) else KIND_PERSON)
        pid = _make_person_id(nm, taken=people.keys())
        people[pid] = _new_entry(nm, kind)
        db["people"] = people
        _write_db(db)
        log.info("новый человек в базе голосов: %s (%s)", nm, pid)
        return _public(pid, people[pid])


def ensure_person(name: str, kind: str | None = None) -> tuple[dict[str, Any], bool]:
    """Человек с этим именем (основным или вариантом); нет — завести. (человек, заведён ли)."""
    nm = _norm_name(name)
    with _lock:
        found = resolve(nm)
        if found:
            return found, False
        return create_person(nm, kind=kind), True


def owner_person(create: bool = True) -> dict[str, Any] | None:
    """Владелец записи в базе голосов: его голос отличает «Я» от соседей в кабинете."""
    with _lock:
        db = _read_db()
        people = dict(db.get("people") or {})
        want = store.owner_name()
        for pid, rec in people.items():
            if rec.get("owner"):
                if _norm_name(rec.get("name")) != want and _resolve_id(want, {
                        k: v for k, v in people.items() if k != pid}) is None:
                    rec = dict(rec)
                    rec["name"] = want
                    people[pid] = rec
                    db["people"] = people
                    _write_db(db)
                return _public(str(pid), rec)
        if not create:
            return None
        nm = want if _resolve_id(want, people) is None else _unique_name(want, people)
        pid = _make_person_id("owner " + nm, taken=people.keys())
        people[pid] = _new_entry(nm, owner=True)
        db["people"] = people
        _write_db(db)
        return _public(pid, people[pid])


def _set_samples(rec: dict[str, Any], samples: list[dict[str, Any]]) -> None:
    rec["samples"] = samples[-MAX_SAMPLES:]
    rec["centroid"] = _as_list(_centroid(rec["samples"])) if rec["samples"] else []
    rec["count"] = len(rec["samples"])
    rec["updated_at"] = _now_iso()


def add_sample(name: str | None, embedding: Any, *, person_id: str | None = None,
               rec_id: str | None = None, speaker_key: str | None = None,
               origin: str = "manual") -> dict[str, Any]:
    """Добавить образец голоса человеку (по id, по имени или новому).

    Образцы накапливаются: храним 12 последних и пересчитываем центроид. Образец
    того же говорящего той же записи заменяет прежний, а не дублирует его.
    Голос «общего устройства» не сохраняется. В ответе поле "added".
    """
    with _lock:
        db = _read_db()
        people = dict(db.get("people") or {})
        pid = str(person_id) if person_id else None
        if pid and pid not in people:
            raise ValueError("человек не найден")
        if pid is None:
            nm = _norm_name(name)
            if not nm or not name_tokens(nm):
                raise ValueError("имя не может быть пустым")
            pid = _resolve_id(nm, people)
            if pid is None:
                pid = _make_person_id(nm, taken=people.keys())
                people[pid] = _new_entry(nm, KIND_SHARED if looks_like_room(nm) else KIND_PERSON)
                log.info("новый человек в базе голосов: %s (%s)", nm, pid)
        rec = dict(people[pid])
        samples = [dict(s) for s in rec.get("samples") or []]
        added = False
        if rec.get("kind") == KIND_SHARED:
            log.info("«%s» — общее устройство, голос не запоминаю", rec.get("name"))
        else:
            dim = len(samples[0]["v"]) if samples else None
            vec = _vec(embedding, expect_dim=dim if dim else EXPECTED_DIM)
            if vec is None:
                log.warning("образец голоса для «%s» отброшен: негодный вектор", rec.get("name"))
            else:
                if rec_id and speaker_key:
                    samples = [s for s in samples
                               if not (s.get("rec_id") == rec_id and s.get("speaker_key") == speaker_key)]
                samples.append({"v": _as_list(vec), "rec_id": rec_id, "speaker_key": speaker_key,
                                "origin": origin if origin in ORIGINS else "manual",
                                "added_at": _now_iso()})
                _set_samples(rec, samples)
                added = True
        people[pid] = rec
        db["people"] = people
        _write_db(db)
        out = _public(pid, rec)
    out["added"] = added
    return out


def samples_from(rec_id: str, speaker_key: str) -> list[dict[str, Any]]:
    """У кого в базе лежат образцы этого говорящего этой записи."""
    out = []
    with _lock:
        for pid, rec in _people().items():
            n = sum(1 for s in rec.get("samples") or []
                    if s.get("rec_id") == rec_id and s.get("speaker_key") == speaker_key)
            if n:
                item = _public(str(pid), rec)
                item["samples_here"] = n
                out.append(item)
    return out


def remove_samples_from(rec_id: str, speaker_key: str, keep_person: str | None = None) -> list[str]:
    """Убрать образцы этого говорящего у всех, кроме keep_person. Вернёт имена, у кого убрано."""
    names: list[str] = []
    with _lock:
        db = _read_db()
        people = dict(db.get("people") or {})
        changed = False
        for pid, rec in people.items():
            if keep_person and str(pid) == str(keep_person):
                continue
            samples = rec.get("samples") or []
            left = [s for s in samples
                    if not (s.get("rec_id") == rec_id and s.get("speaker_key") == speaker_key)]
            if len(left) != len(samples):
                rec = dict(rec)
                _set_samples(rec, left)
                people[pid] = rec
                names.append(rec.get("name") or "")
                changed = True
        if changed:
            db["people"] = people
            _write_db(db)
    return names


def rename_person(person_id: str, new_name: str) -> dict[str, Any] | None:
    """Переименовать человека. id не меняется, связи с записями не рвутся.

    Имя занято другим — NameTaken: молча объединять людей нельзя, объединение
    делается отдельно (merge_people), с копией базы.
    """
    nm = _norm_name(new_name)
    if not nm or not name_tokens(nm):
        raise ValueError("имя не может быть пустым")
    with _lock:
        db = _read_db()
        people = dict(db.get("people") or {})
        pid = str(person_id)
        if pid not in people:
            return None
        other = _resolve_id(nm, {k: v for k, v in people.items() if k != pid})
        if other:
            raise NameTaken("«%s» уже есть в базе" % nm, _public(other, people[other]))
        rec = dict(people[pid])
        old = rec.get("name")
        rec["name"] = nm
        aliases = [a for a in rec.get("aliases") or [] if name_key(a) != name_key(nm)]
        if old and name_key(old) != name_key(nm) and name_key(old) not in {name_key(a) for a in aliases}:
            aliases.append(old)
        rec["aliases"] = aliases
        rec["updated_at"] = _now_iso()
        people[pid] = rec
        db["people"] = people
        _write_db(db)
        return _public(pid, rec)


def add_alias(person_id: str, alias: str) -> dict[str, Any]:
    """Запомнить другой вариант имени человека («Petrov Ivan», «Петров И.»)."""
    nm = _norm_name(alias)
    if not nm or not name_tokens(nm):
        raise ValueError("пустой вариант имени")
    with _lock:
        db = _read_db()
        people = dict(db.get("people") or {})
        pid = str(person_id)
        if pid not in people:
            raise ValueError("человек не найден")
        other = _resolve_id(nm, {k: v for k, v in people.items() if k != pid})
        if other:
            raise NameTaken("«%s» — это имя другого человека в базе" % nm, _public(other, people[other]))
        rec = dict(people[pid])
        keys = {name_key(rec.get("name"))} | {name_key(a) for a in rec.get("aliases") or []}
        if name_key(nm) not in keys:
            rec["aliases"] = list(rec.get("aliases") or []) + [nm]
            rec["updated_at"] = _now_iso()
            people[pid] = rec
            db["people"] = people
            _write_db(db)
        return _public(pid, rec)


def remove_alias(person_id: str, alias: str) -> dict[str, Any]:
    with _lock:
        db = _read_db()
        people = dict(db.get("people") or {})
        pid = str(person_id)
        if pid not in people:
            raise ValueError("человек не найден")
        rec = dict(people[pid])
        rec["aliases"] = [a for a in rec.get("aliases") or [] if name_key(a) != name_key(alias)]
        rec["updated_at"] = _now_iso()
        people[pid] = rec
        db["people"] = people
        _write_db(db)
        return _public(pid, rec)


def set_kind(person_id: str, kind: str) -> dict[str, Any]:
    """Отметить «общее устройство» (переговорка) или вернуть обычного человека."""
    if kind not in KINDS:
        raise ValueError("неизвестный вид: %s" % kind)
    with _lock:
        db = _read_db()
        people = dict(db.get("people") or {})
        pid = str(person_id)
        if pid not in people:
            raise ValueError("человек не найден")
        rec = dict(people[pid])
        rec["kind"] = kind
        if kind == KIND_SHARED and rec.get("samples"):
            # голос общей учётки — смесь разных людей: подставлять его нельзя
            _backup("shared")
            _set_samples(rec, [])
        rec["updated_at"] = _now_iso()
        people[pid] = rec
        db["people"] = people
        _write_db(db)
        return _public(pid, rec)


def mark_not_same(a: str, b: str) -> bool:
    """«Это разные люди» — пара больше не попадает в «возможные дубли»."""
    with _lock:
        db = _read_db()
        people = dict(db.get("people") or {})
        if str(a) not in people or str(b) not in people or str(a) == str(b):
            return False
        for x, y in ((str(a), str(b)), (str(b), str(a))):
            rec = dict(people[x])
            rec["not_same"] = sorted(set(rec.get("not_same") or []) | {y})
            people[x] = rec
        db["people"] = people
        _write_db(db)
        return True


def merge_people(source_id: str, target_id: str) -> dict[str, Any]:
    """Объединить двух людей в одного (target). Сначала — копия базы.

    Образцы и варианты имени переходят к target, имя source становится
    вариантом. В записях, где говорящий был связан с source, связь и подпись
    переводятся на target.
    """
    src, dst = str(source_id), str(target_id)
    if src == dst:
        raise ValueError("нельзя объединить человека с самим собой")
    with _lock:
        db = _read_db()
        people = dict(db.get("people") or {})
        if src not in people or dst not in people:
            raise ValueError("человек не найден")
        _backup("merge")
        a, b = dict(people[src]), dict(people[dst])
        if a.get("owner") and not b.get("owner"):
            b["owner"] = True
        samples = list(b.get("samples") or []) + list(a.get("samples") or [])
        samples.sort(key=lambda s: str(s.get("added_at") or ""))
        _set_samples(b, samples)
        keys = {name_key(b.get("name"))}
        aliases = []
        for nm in list(b.get("aliases") or []) + [a.get("name")] + list(a.get("aliases") or []):
            if nm and name_key(nm) not in keys:
                keys.add(name_key(nm))
                aliases.append(nm)
        b["aliases"] = aliases
        b["not_same"] = sorted((set(b.get("not_same") or []) | set(a.get("not_same") or [])) - {src, dst})
        people[dst] = b
        people.pop(src, None)
        for pid, rec in people.items():
            if src in (rec.get("not_same") or []):
                rec = dict(rec)
                rec["not_same"] = sorted((set(rec["not_same"]) - {src}) | ({dst} if pid != dst else set()))
                people[pid] = rec
        db["people"] = people
        _write_db(db)
        result = _public(dst, b)
        old_name = a.get("name")
    _relink_records(src, dst, old_name, result["name"])
    log.info("объединил «%s» в «%s»", old_name, result["name"])
    return result


def _relink_records(src: str, dst: str, old_name: str | None, new_name: str) -> None:
    """Записи, где говорящий был связан с объединённым человеком, — на нового."""
    for meta in store.list_all():
        speakers = meta.get("speakers") or {}
        if not isinstance(speakers, dict):
            continue
        for key, info in speakers.items():
            if isinstance(info, dict) and str(info.get("person_id") or "") == src:
                try:
                    store.rename_speaker(meta["id"], key, new_name)
                    fresh = store.get(meta["id"]) or {}
                    sp = dict(fresh.get("speakers") or {})
                    entry = dict(sp.get(key) or {})
                    entry["person_id"] = dst
                    sp[key] = entry
                    store.update(meta["id"], {"speakers": sp})
                except Exception as err:
                    log.warning("запись %s не перевязана на «%s»: %s", meta.get("id"), new_name, err)


def delete_person(person_id: str) -> bool:
    """Удалить человека вместе со всеми образцами (с копией базы)."""
    with _lock:
        db = _read_db()
        people = dict(db.get("people") or {})
        if str(person_id) not in people:
            return False
        _backup("delete")
        people.pop(str(person_id), None)
        for pid, rec in people.items():
            if str(person_id) in (rec.get("not_same") or []):
                rec = dict(rec)
                rec["not_same"] = [x for x in rec["not_same"] if x != str(person_id)]
                people[pid] = rec
        db["people"] = people
        _write_db(db)
        return True


def clear_all() -> dict[str, Any]:
    """Очистить базу голосов целиком. Сначала — копия, её можно вернуть."""
    with _lock:
        db = _read_db()
        count = len(db.get("people") or {})
        kept = _backup("clear") if count else None
        _write_db(_empty_db())
        log.info("база голосов очищена: людей было %d, копия %s", count, kept)
        return {"removed": count, "backup": kept}


def list_backups(limit: int = 10) -> list[dict[str, Any]]:
    """Копии базы, новые сверху: имя файла, время, сколько в ней людей."""
    out = []
    for p in sorted(backups_dir().glob("voices-*.json"), reverse=True)[:limit]:
        try:
            with io.open(p, "r", encoding="utf-8-sig") as fh:
                people = len((json.load(fh) or {}).get("people") or {})
        except (OSError, ValueError):
            continue
        out.append({"name": p.name, "people": people,
                    "at": datetime.fromtimestamp(p.stat().st_mtime).isoformat(timespec="seconds")})
    return out


def restore_backup(name: str) -> dict[str, Any]:
    """Вернуть базу из копии. Текущая база перед этим тоже копируется."""
    src = backups_dir() / Path(str(name)).name
    if not src.is_file() or not src.name.startswith("voices-"):
        raise ValueError("копия не найдена")
    with _lock:
        with io.open(src, "r", encoding="utf-8-sig") as fh:
            raw = json.load(fh)
        db = _sanitize(raw)
        if not db["people"] and (raw or {}).get("people"):
            raise ValueError("копия не читается")
        _backup("before-restore")
        _write_db(db)
        log.info("база голосов возвращена из копии %s: людей %d", src.name, len(db["people"]))
        return {"restored": src.name, "people": len(db["people"])}


def forget_sample(person_id: str, index: int) -> bool:
    """Забыть один образец голоса по номеру. Центроид пересчитывается."""
    with _lock:
        db = _read_db()
        people = dict(db.get("people") or {})
        pid = str(person_id)
        rec = people.get(pid)
        if not isinstance(rec, dict):
            return False
        samples = list(rec.get("samples") or [])
        try:
            i = int(index)
        except (TypeError, ValueError):
            return False
        if i < 0 or i >= len(samples):
            return False
        del samples[i]
        rec = dict(rec)
        _set_samples(rec, samples)
        people[pid] = rec
        db["people"] = people
        _write_db(db)
        return True


def person_details(person_id: str) -> dict[str, Any] | None:
    """Карточка для «Настройки → Голоса»: варианты имени и откуда каждый образец."""
    with _lock:
        rec = _people().get(str(person_id))
        if not isinstance(rec, dict):
            return None
        out = _public(str(person_id), rec)
        odd = set(outliers(str(person_id)))
        out["samples"] = [{
            "index": i, "origin": s.get("origin"), "origin_title": ORIGINS.get(s.get("origin"), ""),
            "rec_id": s.get("rec_id"), "speaker_key": s.get("speaker_key"),
            "added_at": s.get("added_at"), "outlier": i in odd,
        } for i, s in enumerate(rec.get("samples") or [])]
        return out


# ---------------------------------------------------------------- сравнение


def cosine(a: Any, b: Any) -> float:
    """Косинусная близость двух векторов, обрезанная в 0..1."""
    va = _vec(a, expect_dim=None)
    vb = _vec(b, expect_dim=None)
    if va is None or vb is None or va.size != vb.size:
        return 0.0
    val = float(np.dot(_unit(va).astype(np.float64), _unit(vb).astype(np.float64)))
    if not np.isfinite(val):
        return 0.0
    return float(min(1.0, max(0.0, val)))


def _thr(key: str, fallback: float) -> float:
    """Порог из настроек. Ноль — это тоже значение, а не «не задано»."""
    raw = config.get(key)
    if raw is None or raw == "":
        return fallback
    try:
        return float(raw)
    except (TypeError, ValueError):
        return fallback


def _thresholds() -> tuple[float, float]:
    """(порог уверенного совпадения, порог подсказки) из настроек."""
    match_thr = _thr("voice_match_threshold", 0.70)
    suggest_thr = _thr("voice_suggest_threshold", 0.45)
    if suggest_thr > match_thr:
        suggest_thr = match_thr
    return match_thr, suggest_thr


def thresholds() -> dict[str, float]:
    match_thr, suggest_thr = _thresholds()
    return {"match": match_thr, "suggest": suggest_thr, "margin": margin()}


def margin() -> float:
    """На сколько лучший кандидат должен обогнать второго, чтобы имя встало само."""
    return max(0.0, _thr("voice_match_margin", 0.10))


def outliers(person_id: str) -> list[int]:
    """Номера образцов, не похожих на остальные образцы этого человека.

    Так видно чужой голос, попавший по ошибке, или общую учётку, под которой
    говорили разные люди. Нужно хотя бы три образца.
    """
    rec = _people().get(str(person_id)) or {}
    samples = rec.get("samples") or []
    if len(samples) < 3:
        return []
    _match, suggest_thr = _thresholds()
    odd = []
    for i, s in enumerate(samples):
        others = _centroid([x for j, x in enumerate(samples) if j != i])
        if others.size and cosine(s.get("v"), others) < suggest_thr:
            odd.append(i)
    return odd


def voice_scores(embedding: Any, include_owner: bool = False) -> list[dict[str, Any]]:
    """Похожесть голоса на всех людей с голосом, по убыванию. Общие устройства не считаются."""
    vec = _vec(embedding, expect_dim=None)
    if vec is None:
        return []
    out = []
    with _lock:
        for pid, rec in _people().items():
            if rec.get("kind") == KIND_SHARED or (rec.get("owner") and not include_owner):
                continue
            cent = rec.get("centroid") or []
            if len(cent) != vec.size:
                continue
            out.append({"person_id": str(pid), "name": rec.get("name") or "",
                        "score": round(cosine(vec, cent), 4)})
    out.sort(key=lambda c: (-c["score"], c["name"]))
    return out


def _candidates(embedding: Any) -> list[dict[str, Any]]:
    """Люди с близостью выше порога подсказки, по убыванию близости.

    confident — не только выше порога уверенности, но и заметно лучше второго
    кандидата (margin): два похожих голоса не должны подписываться наугад.
    """
    match_thr, suggest_thr = _thresholds()
    scores = voice_scores(embedding)
    out: list[dict[str, Any]] = []
    gap = margin()
    for i, c in enumerate(scores):
        if c["score"] < suggest_thr:
            break
        item = dict(c)
        item["confident"] = False
        if i == 0 and c["score"] >= match_thr:
            second = scores[1]["score"] if len(scores) > 1 else 0.0
            item["confident"] = (c["score"] - second) >= gap
        out.append(item)
    return out


def match(embedding: Any) -> dict[str, Any] | None:
    """Лучшее совпадение по базе или None: {"person_id", "name", "score", "confident"}."""
    cands = _candidates(embedding)
    return cands[0] if cands else None


def match_all(embeddings: dict[str, Any]) -> dict[str, dict[str, Any] | None]:
    """Сопоставить сразу всех спикеров записи: {"SPEAKER_00": вектор, ...}.

    Один человек не достаётся двум спикерам: пары (спикер, человек) идут по
    убыванию близости и занимаются жадно. Спикер, которому никого не осталось,
    получает None.
    """
    result: dict[str, dict[str, Any] | None] = {}
    pairs: list[tuple[float, str, dict[str, Any]]] = []
    for label, emb in (embeddings or {}).items():
        key = str(label)
        result[key] = None
        for cand in _candidates(emb):
            pairs.append((float(cand["score"]), key, cand))
    pairs.sort(key=lambda it: (-it[0], it[1], it[2]["name"]))
    taken_people: set[str] = set()
    for _score, key, cand in pairs:
        if result.get(key) is not None:
            continue
        if cand["person_id"] in taken_people:
            continue
        result[key] = dict(cand)
        taken_people.add(cand["person_id"])
    return result


#: Узнан по голосу, но похожесть ниже этой — предложить кнопкой добавить образец
#: (решение 15.09). Образцы копились только из ошибок: когда человека
#: узнали, новый образец не добавлялся, и узнавание с другим микрофоном или в
#: другой день не крепло. Сам образец — только по нажатию.
SAMPLE_OFFER_BELOW = 0.80


def sample_offer(score: Any) -> bool:
    """Предлагать ли добавить образец узнанному по голосу человеку."""
    try:
        return score is not None and float(score) < SAMPLE_OFFER_BELOW
    except (TypeError, ValueError):
        return False


def apply_to_meta(rec_id: str, embeddings: dict[str, Any]) -> dict[str, Any]:
    """Сопоставить спикеров записи с базой и записать результат в meta.json.

    Структура meta["speakers"]:
      {"SPEAKER_00": {"name": имя или None, "person_id": id или None,
                      "score": float или None, "suggestion": {...} или None,
                      "confirmed": False}}

    Имя подставляется СРАЗУ только при уверенном совпадении (порог и отрыв от
    второго кандидата). При более слабом — suggestion: «Похоже на Ивана
    Петрова (72%) — это он?». Подтверждённые человеком спикеры не трогаются.
    """
    matches = match_all(embeddings or {})
    meta = store.get(rec_id) or {}
    old = meta.get("speakers") or {}
    if not isinstance(old, dict):
        old = {}
    speakers: dict[str, Any] = {}
    for label, res in matches.items():
        key = str(label)
        prev = old.get(key)
        prev = prev if isinstance(prev, dict) else {}
        if prev.get("confirmed") and prev.get("name"):
            entry = dict(prev)
            entry["suggestion"] = None
            speakers[key] = entry
            continue
        entry = {"name": None, "person_id": None, "score": None,
                 "suggestion": None, "confirmed": False}
        if res:
            entry["score"] = res.get("score")
            if res.get("confident"):
                entry["name"] = res.get("name")
                entry["person_id"] = res.get("person_id")
                entry["sample_offer"] = sample_offer(res.get("score"))
            else:
                entry["suggestion"] = dict(res)
        speakers[key] = entry
    for key, entry in old.items():
        if key not in speakers and isinstance(entry, dict):
            speakers[key] = entry
    if meta.get("id"):
        store.update(rec_id, {"speakers": speakers})
    named = sum(1 for e in speakers.values() if (e or {}).get("name"))
    log.info("запись %s: узнал %d из %d говорящих", rec_id, named, len(matches))
    return speakers
