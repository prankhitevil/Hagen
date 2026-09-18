# -*- coding: utf-8 -*-
"""Говорящие записи и база голосов: подписать, проверить, выучить, разделить.

voices.py знает только базу (люди, варианты имён, образцы). Здесь — всё, что
связывает базу с конкретной записью. Решения 13.09.2026:

ПОДПИСАТЬ ГОВОРЯЩЕГО. Сначала проверка, потом вопрос с понятными кнопками, потом
сохранение — молча программа ничего не решает (plan → conflict → apply):
  * имя похоже на кого-то в базе (инициалы, латиница, отчество) — «это он?»;
  * этот человек уже подписан у другого говорящего этой записи — «объединить?»;
  * голос уверенно похож на ДРУГОГО человека — «похоже на Сидорова (82 %)»;
  * у человека в базе совсем другой голос — «это он / тёзка / без голоса»,
    по умолчанию голос не запоминается;
  * переподписали говорящего — его образец голоса уходит от прежнего человека.

ИМЕНА ИЗ TEAMS. При загрузке расшифровки каждое имя связывается с человеком в
базе (или заводится). Если звук записи есть и включена авторазметка, голоса
выучиваются: разметка голосов сравнивается с именами Teams по времени реплик,
образец берётся, только если голос почти целиком приходится на одно имя.

НЕСКОЛЬКО ЧЕЛОВЕК ЗА ОДНИМ УСТРОЙСТВОМ. Переговорка или сосед у компьютера:
под одним именем говорят разные голоса. Такой голос не учится. Программа
предлагает разделить — «Разделить голоса» берёт из записи только реплики этого
говорящего (любой дорожки), распознаёт их заново со временем слов и размечает
голоса. Для «Я» (соседи у меня в кабинете) свой голос узнаётся по образцу
владельца, остальные становятся «Рядом со мной · голос N».
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Callable

import numpy as np

from . import audio_io, config, store, voices

log = logging.getLogger("hagen.speakers")

SR = audio_io.SR
SPLIT_MARK = "~"                 # ключ голоса после разделения: «sub3~2», «me~1»
LEARN_MIN_S = 15.0               # столько речи голоса нужно, чтобы взять образец
LEARN_SHARE = 0.7                # голос должен почти целиком приходиться на одно имя
LEARN_PART = 0.15                # голос — «заметная часть» речи имени
PAD_S = 0.15                     # запас по краям реплик при вырезании звука


class Conflict(Exception):
    """Подписать нельзя без решения человека. В .plan — вопрос и кнопки."""

    def __init__(self, plan: dict[str, Any]):
        super().__init__((plan.get("conflict") or {}).get("text") or "нужно решение")
        self.plan = plan


# ---------------------------------------------------------------- общее


def _pct(x: float | None) -> int:
    return int(round(float(x or 0.0) * 100))


def segments_of(rec_id: str, key: str, include_echo: bool = True) -> list[dict[str, Any]]:
    return [s for s in store.sorted_segments(rec_id, include_echo=include_echo)
            if s.get("speaker_key") == key]


def track_of(rec_id: str, key: str) -> str | None:
    segs = segments_of(rec_id, key)
    return str(segs[0].get("track")) if segs else None


def embedding_for(rec_id: str, key: str) -> list[float] | None:
    """Голос говорящего этой записи: из разметки, из разделения или из Teams."""
    from . import diarize

    res = diarize.load_result(rec_id) or {}
    emb = (res.get("key_embeddings") or {}).get(key)
    if emb is None:
        emb = (res.get("embeddings") or {}).get(key)
    return emb if emb else None


def _save_key_embeddings(rec_id: str, extra: dict[str, Any], **fields: Any) -> None:
    from . import diarize

    res = diarize.load_result(rec_id) or {}
    keys = dict(res.get("key_embeddings") or {})
    keys.update(extra)
    res["key_embeddings"] = keys
    res.update(fields)
    diarize.save_result(rec_id, res)


def _speakers(meta: dict[str, Any]) -> dict[str, Any]:
    sp = meta.get("speakers") or {}
    return dict(sp) if isinstance(sp, dict) else {}


def _set_entry(rec_id: str, key: str, **fields: Any) -> None:
    meta = store.get(rec_id) or {}
    sp = _speakers(meta)
    entry = dict(sp.get(key) or {})
    entry.update(fields)
    sp[key] = entry
    store.update(rec_id, {"speakers": sp})


def display_name(meta: dict[str, Any], key: str) -> str:
    info = _speakers(meta).get(key) or {}
    if info.get("name"):
        return str(info["name"])
    if key == "me":
        return store.owner_name()
    return key


# ---------------------------------------------------------------- подписать говорящего


def _choice(cid: str, label: str, primary: bool = False) -> dict[str, Any]:
    return {"id": cid, "label": label, "primary": primary}


def plan(rec_id: str, key: str, name: str, decision: dict[str, Any] | None = None,
         remember: bool = True) -> dict[str, Any]:
    """Что произойдёт, если подписать говорящего key именем name.

    Возвращает {"target", "conflict", "notes", "remember", "has_voice"}. Если
    conflict не None — нужен выбор человека; выбранная кнопка приходит обратно
    в decision, и план считается заново.
    """
    d = dict(decision or {})
    meta = store.get(rec_id)
    if meta is None:
        raise LookupError("Запись не найдена")
    segs = segments_of(rec_id, key)
    if not segs:
        raise LookupError("Такого говорящего в записи нет")
    emb = embedding_for(rec_id, key)
    out: dict[str, Any] = {"target": None, "conflict": None, "notes": [],
                           "remember": bool(remember), "has_voice": emb is not None,
                           "track": segs[0].get("track")}
    if d.get("me"):
        owner = voices.owner_person()
        out["target"] = dict(owner, me=True)
        return out

    typed = voices._norm_name(name)
    use = str(d.get("voice") or "")
    target: dict[str, Any] | None = None
    if use.startswith("use:"):
        target = voices.get_person(use[4:])
        if target is None:
            raise LookupError("Человек не найден")
    elif d.get("person_id"):
        target = voices.get_person(str(d["person_id"]))
        if target is None:
            raise LookupError("Человек не найден")
    elif d.get("alias_of"):
        target = voices.get_person(str(d["alias_of"]))
        if target is None:
            raise LookupError("Человек не найден")
        if typed and voices.name_key(typed) != voices.name_key(target["name"]):
            out["alias"] = typed
    else:
        if not typed or not voices.name_tokens(typed):
            raise ValueError("Введите имя")
        if not (d.get("namesake") or d.get("new_person")):
            target = voices.resolve(typed)
            if target is None:
                similar = voices.similar_people(typed)
                if similar:
                    choices = [_choice("alias:%s" % p["id"],
                                       "Это «%s» — запомнить «%s» как вариант имени" % (p["name"], typed))
                               for p in similar[:3]]
                    choices.append(_choice("new", "Нет, это другой человек — завести «%s»" % typed))
                    who = ", ".join("«%s» (%s)" % (p["name"], p["reason"]) for p in similar[:3])
                    out["conflict"] = {"kind": "similar_name", "choices": choices,
                                       "text": "В базе есть похожее имя: %s. Это тот же человек?" % who}
                    return out

    if target is None:
        people = voices._people()
        new_name = voices._unique_name(typed, people) if d.get("namesake") else typed
        out["target"] = {"id": None, "name": new_name, "new": True, "has_voice": False,
                         "kind": voices.KIND_SHARED if voices.looks_like_room(new_name) else voices.KIND_PERSON}
    else:
        out["target"] = dict(target)
    tgt = out["target"]

    if tgt.get("kind") == voices.KIND_SHARED:
        out["remember"] = False
        out["notes"].append("«%s» — общее устройство (переговорка): реплики подпишу, голос не запоминаю."
                            % tgt["name"])

    # Сначала голос, потом «тот же человек у другого говорящего»: если голос
    # явно чужой, предлагать объединение говорящих было бы неверно.
    if emb is not None and out["remember"] and not d.get("voice"):
        match_thr, suggest_thr = voices._thresholds()
        gap = voices.margin()
        own = None
        if tgt.get("id"):
            rec = voices._people().get(tgt["id"]) or {}
            if rec.get("centroid"):
                own = voices.cosine(emb, rec["centroid"])
        others = [s for s in voices.voice_scores(emb) if s["person_id"] != tgt.get("id")]
        other = others[0] if others else None
        if other and other["score"] >= match_thr and (own is None or other["score"] - own >= gap):
            tail = (" На голос «%s» в базе — %d %%." % (tgt["name"], _pct(own))) if own is not None else ""
            out["conflict"] = {
                "kind": "voice_is_other", "other": other,
                "text": "Этот голос похож на «%s» (%d %%).%s" % (other["name"], _pct(other["score"]), tail),
                "choices": [_choice("use:%s" % other["person_id"], "Это «%s»" % other["name"]),
                            _choice("keep", "Всё-таки «%s» — запомнить голос" % tgt["name"]),
                            _choice("no", "Подписать «%s», голос не запоминать" % tgt["name"], True)]}
            return out
        if own is not None and own < suggest_thr:
            out["conflict"] = {
                "kind": "voice_differs", "score": own,
                "text": "У «%s» в базе другой голос (сходство %d %%)." % (tgt["name"], _pct(own)),
                "choices": [_choice("add", "Это он — добавить голос (другой микрофон, связь)"),
                            _choice("namesake", "Это тёзка — отдельный человек"),
                            _choice("no", "Подписать, голос не запоминать", True)]}
            return out

    if tgt.get("id") and not d.get("merge") and not d.get("keep_both"):
        others = [k for k, info in _speakers(meta).items()
                  if k != key and isinstance(info, dict) and info.get("person_id") == tgt["id"]
                  and segments_of(rec_id, k)]
        if others:
            other_emb = embedding_for(rec_id, others[0])
            alike = voices.cosine(emb, other_emb) if (emb is not None and other_emb is not None) else None
            match_thr = voices._thresholds()[0]
            if alike is None:
                why = "Возможно, разметка разделила одного человека надвое."
            elif alike >= match_thr:
                why = "Голоса у них похожи (%d %%) — похоже, разметка разделила одного человека надвое." % _pct(alike)
            else:
                why = "Голоса у них разные (сходство %d %%)." % _pct(alike)
            same = alike is None or alike >= match_thr
            out["conflict"] = {
                "kind": "same_in_record", "other_key": others[0], "score": alike,
                "text": "В этой записи «%s» уже подписан у другого говорящего. %s" % (tgt["name"], why),
                "choices": [_choice("merge", "Это один человек — объединить говорящих", same),
                            _choice("namesake", "Это тёзка — отдельный человек", not same),
                            _choice("keep_both", "Оставить двух говорящих с этим именем")]}
            return out

    if emb is not None:
        for p in voices.samples_from(rec_id, key):
            if p["id"] != tgt.get("id"):
                out["notes"].append("Голос этого говорящего был сохранён у «%s» — уберу его оттуда."
                                    % p["name"])
    if emb is None and out["remember"]:
        out["notes"].append("Голос этого говорящего ещё не размечен — запомню только имя.")
    return out


def apply(rec_id: str, key: str, name: str, decision: dict[str, Any] | None = None,
          remember: bool = True) -> dict[str, Any]:
    """Подписать говорящего. Нужен выбор человека — Conflict с планом внутри."""
    d = dict(decision or {})
    p = plan(rec_id, key, name, d, remember)
    if p["conflict"]:
        raise Conflict(p)
    tgt = p["target"]
    emb = embedding_for(rec_id, key)
    if tgt.get("me"):
        return _assign_me(rec_id, key, emb, p["remember"] and d.get("voice") != "no")

    if tgt.get("new"):
        person = voices.create_person(tgt["name"], namesake=bool(d.get("namesake")))
    else:
        person = voices.get_person(tgt["id"]) or tgt
    if p.get("alias"):
        try:
            voices.add_alias(person["id"], p["alias"])
        except voices.NameTaken:
            pass

    final_key = key
    if d.get("merge"):
        meta = store.get(rec_id) or {}
        others = [k for k, info in _speakers(meta).items()
                  if k != key and isinstance(info, dict) and info.get("person_id") == person["id"]]
        if others:
            final_key = others[0]
            _merge_keys(rec_id, key, final_key)

    store.rename_speaker(rec_id, final_key, person["name"])
    # подписали сами — предлагать «добавить образец» больше незачем
    _set_entry(rec_id, final_key, person_id=person["id"], suggestion=None, multi_voice=None,
               sample_offer=False)
    removed = voices.remove_samples_from(rec_id, key, keep_person=person["id"])
    saved = False
    if emb is not None and p["remember"] and d.get("voice") != "no":
        saved = bool(voices.add_sample(None, emb, person_id=person["id"], rec_id=rec_id,
                                       speaker_key=key).get("added"))
    store.refresh_participants(rec_id)
    log.info("запись %s: %s → «%s»%s", rec_id, key, person["name"], " (голос запомнен)" if saved else "")
    return {"person": voices.get_person(person["id"]), "key": final_key,
            "voice_saved": saved, "removed_from": removed}


def _merge_keys(rec_id: str, src: str, dst: str) -> None:
    """Реплики говорящего src переходят к dst (разметка разделила человека надвое)."""
    meta = store.get(rec_id) or {}
    name = display_name(meta, dst)
    segs = store.sorted_segments(rec_id, include_echo=True)
    for s in segs:
        if s.get("speaker_key") == src:
            s["speaker_key"] = dst
            s["speaker"] = name
            s["suggestion"] = None
    store.replace_segments(rec_id, segs)
    sp = _speakers(store.get(rec_id) or {})
    sp.pop(src, None)
    store.update(rec_id, {"speakers": sp})


def _assign_me(rec_id: str, key: str, emb: Any, remember: bool) -> dict[str, Any]:
    """«Это я»: реплики говорящего становятся репликами владельца."""
    owner = voices.owner_person()
    segs = store.sorted_segments(rec_id, include_echo=True)
    for s in segs:
        if s.get("speaker_key") == key:
            s["speaker_key"] = "me"
            s["speaker"] = store.SPEAKER_ME
            s["speaker_locked"] = True
            s["suggestion"] = None
    store.replace_segments(rec_id, segs)
    sp = _speakers(store.get(rec_id) or {})
    sp.pop(key, None)
    store.update(rec_id, {"speakers": sp})
    removed = voices.remove_samples_from(rec_id, key, keep_person=owner["id"])
    saved = False
    if emb is not None and remember:
        saved = bool(voices.add_sample(None, emb, person_id=owner["id"], rec_id=rec_id,
                                       speaker_key=key).get("added"))
    store.refresh_participants(rec_id)
    return {"person": voices.get_person(owner["id"]), "key": "me", "voice_saved": saved,
            "removed_from": removed}


def search_for(rec_id: str, key: str, query: str) -> dict[str, Any]:
    """Подсказки для поля имени: люди из базы с процентом похожести голоса."""
    meta = store.get(rec_id) or {}
    emb = embedding_for(rec_id, key) if key else None
    people = voices.search(query, embedding=emb, limit=12)
    info = _speakers(meta).get(key) or {}
    attendees = []
    q = voices.name_tokens(query)
    for n in (meta.get("meeting") or {}).get("attendees") or []:
        if voices.resolve(n):
            continue
        toks = voices.name_tokens(n)
        if all(any(t.startswith(x) or x in t for t in toks) for x in q):
            attendees.append(n)
    return {"people": people, "attendees": attendees[:5],
            "suggestion": info.get("suggestion"), "has_voice": emb is not None,
            "is_mic": track_of(rec_id, key) == store.TRACK_MIC if key else False,
            "owner": store.owner_name(), "thresholds": voices.thresholds()}


# ---------------------------------------------------------------- имена из Teams


def link_names(rec_id: str) -> dict[str, Any]:
    """Имена говорящих из расшифровки — в базу: связать с человеком или завести."""
    meta = store.get(rec_id) or {}
    sp = _speakers(meta)
    linked = 0
    for key, info in sp.items():
        if not isinstance(info, dict) or not info.get("name") or info.get("person_id"):
            continue
        try:
            person, created = voices.ensure_person(str(info["name"]))
        except ValueError:
            continue
        info = dict(info)
        info["person_id"] = person["id"]
        sp[key] = info
        linked += 1
        if created:
            log.info("из расшифровки в базу: %s", person["name"])
    if linked:
        store.update(rec_id, {"speakers": sp})
    return sp


def _overlap(a0: float, a1: float, b0: float, b1: float) -> float:
    return max(0.0, min(a1, b1) - max(a0, b0))


def learn_from_names(rec_id: str, result: dict[str, Any]) -> dict[str, Any]:
    """Выучить голоса людей, чьи имена пришли из расшифровки.

    Разметка голосов (result) сравнивается с репликами по времени. Для каждого
    имени берутся голоса, которые почти целиком (LEARN_SHARE) приходятся на это
    имя и занимают заметную часть его речи. Один такой голос — образец в базу.
    Несколько разных голосов — общее устройство: не учим, предлагаем разделить.
    Несколько похожих — разметка раздробила одного человека: берём среднее.
    """
    meta = store.get(rec_id) or {}
    sp = _speakers(meta)
    turns = result.get("exclusive_turns") or result.get("turns") or []
    embs = result.get("embeddings") or {}
    segs = [s for s in store.sorted_segments(rec_id) if s.get("speaker_key") in sp]
    label_total: dict[str, float] = {}
    for t in turns:
        label_total[t["speaker"]] = label_total.get(t["speaker"], 0.0) + float(t["end"]) - float(t["start"])
    shared: dict[str, dict[str, float]] = {}
    for s in segs:
        s0, s1 = float(s.get("start") or 0.0), float(s.get("end") or 0.0)
        for t in turns:
            ov = _overlap(s0, s1, float(t["start"]), float(t["end"]))
            if ov > 0:
                row = shared.setdefault(str(s["speaker_key"]), {})
                row[t["speaker"]] = row.get(t["speaker"], 0.0) + ov
    match_thr, suggest_thr = voices._thresholds()
    report: list[dict[str, Any]] = []
    key_embs: dict[str, Any] = {}
    for key, row in shared.items():
        info = sp.get(key) or {}
        name = info.get("name") or key
        total = sum(row.values())
        voices_here = [lab for lab, sec in row.items()
                       if sec >= LEARN_MIN_S and sec >= LEARN_PART * total
                       and sec / max(1e-6, label_total.get(lab, 0.0)) >= LEARN_SHARE and embs.get(lab)]
        item = {"key": key, "name": name, "voices": len(voices_here)}
        person = voices.get_person(info.get("person_id")) if info.get("person_id") else None
        if not voices_here:
            item["status"] = "little"
        elif len(voices_here) > 1 and any(
                voices.cosine(embs[a], embs[b]) < match_thr
                for i, a in enumerate(voices_here) for b in voices_here[i + 1:]):
            item["status"] = "multi"
            _set_entry(rec_id, key, multi_voice={"voices": len(voices_here)})
        else:
            vecs = [np.asarray(embs[lab], dtype=np.float64) for lab in voices_here]
            emb = np.mean([v / np.linalg.norm(v) for v in vecs], axis=0).tolist()
            key_embs[key] = emb
            if person is None:
                item["status"] = "no_person"
            elif person.get("kind") == voices.KIND_SHARED:
                item["status"] = "shared"
            else:
                rec = voices._people().get(person["id"]) or {}
                if rec.get("centroid") and voices.cosine(emb, rec["centroid"]) < suggest_thr:
                    item["status"] = "differs"
                else:
                    added = voices.add_sample(None, emb, person_id=person["id"], rec_id=rec_id,
                                              speaker_key=key, origin="teams")
                    item["status"] = "added" if added.get("added") else "skipped"
        report.append(item)
    if key_embs:
        _save_key_embeddings(rec_id, key_embs)
    store.update(rec_id, {"voices_learned": report})
    log.info("запись %s: голоса из имён — %s", rec_id,
             ", ".join("%s: %s" % (r["name"], r["status"]) for r in report) or "нечего учить")
    return {"report": report}


def dismiss_multi(rec_id: str, key: str) -> None:
    """«Это один человек» — не предлагать разделение."""
    _set_entry(rec_id, key, multi_voice=None, multi_dismissed=True)


def pending_split(meta: dict[str, Any]) -> bool:
    """Ждёт ли запись решения «разделить голоса» (тогда звук убирать рано)."""
    return any(isinstance(i, dict) and i.get("multi_voice") for i in _speakers(meta).values())


# ---------------------------------------------------------------- разделить голоса


#: Метка единственного чужого голоса, когда модель голосов не услышала вовсе.
ABSENT_VOICE = "OTHER_0"
#: Под этим ключом в разметке записи лежит отпечаток голоса, который программа
#: сочла «вашим» по правилу «самый частый». В базу он попадает только по кнопке
#: «Да, это мой голос — запомнить» (confirm_owner_voice). Не ключ говорящего.
OWNER_CANDIDATE = "me:candidate"
#: Как выбран владелец: по образцу, отметкой «меня не было», никто не похож,
#: самый частый голос (догадка — её можно подтвердить кнопкой).
OWNER_BY_SAMPLE, OWNER_ABSENT, OWNER_NO_MATCH, OWNER_GUESS = "sample", "absent", "no_match", "guess"


def _pick_owner(labels: list[str], embs: dict[str, Any],
                absent: bool) -> tuple[str | None, str, str, float | None]:
    """Какой из найденных голосов — владелец. None — ничей (решение 15.09).

    Возвращает (метка, пояснение для плашки, как выбран, похожесть на образец).

    * «меня в записи не было» — «Я» не ставится никому;
    * образец владельца есть и отпечатки посчитаны — «Я» только тому, кто на
      образец похож; не похож никто — никому (раньше «Я» доставалось самому
      частому голосу, и видео с телефона становилось репликами владельца);
    * образца нет — прежнее правило: самый частый голос. Это догадка, и её
      можно подтвердить кнопкой — тогда голос запомнится.
    """
    if not labels:
        return None, "", "", None
    if absent:
        return None, "вы отметили, что вас в записи не было — «Я» не поставлено никому", OWNER_ABSENT, None
    owner = voices.owner_person()
    rec = voices._people().get(owner["id"]) or {}
    cent = rec.get("centroid") or []
    scored = [(voices.cosine(embs.get(lab), cent), lab) for lab in labels if embs.get(lab) and cent]
    if scored:
        _match, suggest_thr = voices._thresholds()
        best = max(scored)
        if best[0] >= suggest_thr:
            return best[1], "ваш голос узнан по образцу (%d %%)" % _pct(best[0]), OWNER_BY_SAMPLE, best[0]
        return None, ("вашего голоса по образцу здесь нет (лучшее сходство %d %%) — «Я» не "
                      "поставлено никому; если ошибка, подпишите «Это я»" % _pct(best[0])), OWNER_NO_MATCH, best[0]
    return (labels[0], "ваш голос — самый частый в микрофоне; если ошибка, подпишите «Это я» у другого",
            OWNER_GUESS, None)


def confirm_owner_voice(rec_id: str) -> dict[str, Any]:
    """«Да, это мой голос — запомнить»: образец владельца из угаданного голоса.

    Решение 15.09. Раньше при верной догадке «самый частый голос —
    ваш» образец не сохранялся нигде: «Это я» есть только у чужих голосов, и
    запомнить свой голос было нечем. Запоминается только по нажатию.
    """
    from . import diarize

    meta = store.get(rec_id)
    if meta is None:
        raise LookupError("Запись не найдена")
    splits = dict(meta.get("splits") or {})
    info = dict(splits.get("me") or {})
    was_guess = bool(info.get("owner_guess"))
    if not (was_guess or info.get("owner_add")):
        raise ValueError("Подтверждать нечего: ваш голос уже запомнен или в этой записи не угадывался.")
    emb = ((diarize.load_result(rec_id) or {}).get("key_embeddings") or {}).get(OWNER_CANDIDATE)
    if not emb:
        raise ValueError("Отпечаток голоса не сохранился. Снимите и снова поставьте "
                         "«со мной в комнате были ещё люди».")
    owner = voices.owner_person()
    res = voices.add_sample(None, emb, person_id=owner["id"], rec_id=rec_id, speaker_key="me")
    info["owner_guess"] = False
    info["owner_add"] = False
    info["owner_note"] = ("ваш голос запомнен — дальше «Я» ставится по нему" if was_guess
                          else "образец добавлен — ваш голос будет узнаваться увереннее")
    splits["me"] = info
    store.update(rec_id, {"splits": splits})
    log.info("запись %s: голос владельца запомнен по подтверждению", rec_id)
    return {"voice_saved": bool(res.get("added")), "person": voices.get_person(owner["id"])}


def add_offered_sample(rec_id: str, key: str) -> dict[str, Any]:
    """«Добавить образец» узнанному по голосу, но неуверенно человеку (15.09).

    Для «Я» — то же, что confirm_owner_voice. Для остальных — только если
    программа сама предложила (sample_offer), и только по нажатию.
    """
    if key == "me":
        return confirm_owner_voice(rec_id)
    meta = store.get(rec_id)
    if meta is None:
        raise LookupError("Запись не найдена")
    entry = _speakers(meta).get(key) or {}
    if not (entry.get("sample_offer") and entry.get("person_id")):
        raise ValueError("Добавлять нечего: этот голос не узнавался или образец уже добавлен.")
    person = voices.get_person(str(entry["person_id"]))
    if person is None:
        raise LookupError("Человек не найден")
    emb = embedding_for(rec_id, key)
    if emb is None:
        raise ValueError("Отпечаток голоса этого говорящего не сохранился — разметьте говорящих заново.")
    res = voices.add_sample(None, emb, person_id=person["id"], rec_id=rec_id, speaker_key=key)
    _set_entry(rec_id, key, sample_offer=False)
    if not res.get("added"):
        raise ValueError("Образец не добавлен: «%s» отмечен как общее устройство — голос "
                         "такой учётки не запоминается." % person["name"])
    log.info("запись %s: %s → образец голоса «%s» добавлен по кнопке", rec_id, key, person["name"])
    return {"voice_saved": True, "person": voices.get_person(person["id"])}


def _split_key(base: str, index: int) -> str:
    return "%s%s%d" % (base.split(SPLIT_MARK)[0], SPLIT_MARK, index)


def split_speaker(rec_id: str, key: str, handle: Any = None,
                  diarize_fn: Callable[..., dict[str, Any]] | None = None,
                  transcribe_fn: Callable[..., list[dict[str, Any]]] | None = None,
                  speakers: int | None = None) -> dict[str, Any]:
    """Разделить голоса одного говорящего (несколько человек за одним устройством).

    1. Из записи берутся только реплики этого говорящего — с той дорожки, где они есть.
    2. Реплики без времени слов (расшифровка Teams, живая запись) распознаются
       заново точной моделью: иначе реплику, где говорили двое, не разрезать.
    3. Разметка голосов по звуку только этих реплик.
    4. Реплики расходятся по голосам: «Имя · голос 1», «голос 2»… Голоса,
       которые есть в базе, подписываются сами или с подсказкой. У «Я» свой
       голос узнаётся по образцу.
    """
    from . import asr, diarize, vad

    diarize_fn = diarize_fn or diarize.diarize_pcm
    transcribe_fn = transcribe_fn or asr.transcribe_spans
    meta = store.get(rec_id)
    if meta is None:
        raise LookupError("Запись не найдена")
    mine = segments_of(rec_id, key, include_echo=False)
    if not mine:
        raise LookupError("Такого говорящего в записи нет")
    track = str(mine[0].get("track"))
    wav = store.track_path(rec_id, track)
    if not wav.exists():
        raise RuntimeError("Звук этой записи не сохранён — разделить голоса нельзя.")
    pcm, sr = audio_io.read_wav(wav)
    pcm = np.asarray(pcm, dtype=np.float32).reshape(-1)
    base_name = display_name(meta, key) if key != "me" else store.owner_name()

    def note(msg: str, value: float | None = None) -> None:
        if handle is None:
            return
        try:
            handle.log(msg)
            if value is not None:
                handle.progress(value, msg)
        except Exception:
            pass

    # --- 1–2. слова со временем
    lang = str(meta.get("asr_lang") or "ru")
    all_segs = store.sorted_segments(rec_id, include_echo=True)
    redo = [s for s in all_segs if s.get("speaker_key") == key and not s.get("echo")
            and not diarize._usable_words(s.get("words"), float(s["start"]), float(s["end"]))]
    if redo:
        note("распознаю заново реплики «%s»: %d" % (base_name, len(redo)), 0.05)
        for i, s in enumerate(redo):
            a = max(0, int((float(s["start"]) - PAD_S) * sr))
            b = min(pcm.shape[0], int((float(s["end"]) + PAD_S) * sr))
            if b - a < int(0.12 * sr):
                continue
            piece = pcm[a:b]
            spans = vad.split_for_asr(piece) if (b - a) > 24 * sr else [{"start": 0, "end": b - a}]
            got = transcribe_fn(piece, spans, precise=True, words=True, lang=lang)
            words, texts = [], []
            for g in got:
                texts.append(g.get("text") or "")
                off = a / float(sr)
                words += [{"text": w["text"], "start": round(off + float(w["start"]), 3),
                           "end": round(off + float(w["end"]), 3)} for w in g.get("words") or []]
            if " ".join(t for t in texts if t).strip():
                s["text"] = " ".join(t for t in texts if t).strip()
                s["words"] = words
                s["retranscribed"] = True
            if handle is not None and redo:
                note("распознано %d из %d" % (i + 1, len(redo)), 0.05 + 0.35 * (i + 1) / len(redo))
        store.replace_segments(rec_id, all_segs)

    # --- 3. разметка только этих реплик
    mask = np.zeros_like(pcm)
    for s in mine:
        a = max(0, int((float(s["start"]) - PAD_S) * sr))
        b = min(pcm.shape[0], int((float(s["end"]) + PAD_S) * sr))
        mask[a:b] = pcm[a:b]
    note("размечаю голоса в репликах «%s»" % base_name, 0.45)
    # Человек назвал число голосов — ищем ровно столько. Не назвал (как было
    # раньше) — модель решает сама.
    limits: dict[str, Any] = {}
    if speakers:
        limits = {"min_speakers": int(speakers), "max_speakers": int(speakers)}
    result = diarize_fn(mask, sr=sr, handle=handle, **limits)
    turns = result.get("exclusive_turns") or result.get("turns") or []
    embs = result.get("embeddings") or {}
    speech: dict[str, float] = {}
    for t in turns:
        speech[t["speaker"]] = speech.get(t["speaker"], 0.0) + float(t["end"]) - float(t["start"])
    labels = sorted(speech, key=lambda lab: -speech[lab])
    absent = key == "me" and bool(meta.get("owner_absent"))
    if key == "me" and not labels and absent:
        # Голосов модель не услышала (звук из динамика телефона в микрофон —
        # запись 15.09 9:14), а человек отметил, что его в записи не было:
        # все его реплики — один чужой голос, а не «Я».
        labels = [ABSENT_VOICE]
        turns = [{"start": 0.0, "end": pcm.shape[0] / float(sr), "speaker": ABSENT_VOICE}]

    # --- 4. кто есть кто
    owner_label, owner_note, owner_how, owner_score = (_pick_owner(labels, embs, absent) if key == "me"
                                          else (None, "", "", None))
    # Делить нечего, только если голос один и это не чужой голос в «моих» репликах.
    if len(labels) < 2 and not (key == "me" and labels and owner_label is None):
        if key != "me":
            _set_entry(rec_id, key, multi_voice=None, multi_dismissed=True)
        note("в репликах «%s» нашёлся один голос — делить нечего" % base_name, 1.0)
        return {"voices": len(labels), "keys": []}
    numbers = {lab: i + 1 for i, lab in enumerate([x for x in labels if x != owner_label])}

    def key_for(lab: str) -> str:
        return "me" if lab == owner_label else _split_key(key, numbers.get(lab, len(labels)))

    def name_for(lab: str) -> str:
        if lab == owner_label:
            return store.SPEAKER_ME
        prefix = "Рядом со мной" if key == "me" else base_name
        return "%s · голос %d" % (prefix, numbers.get(lab, len(labels)))

    segs_now = store.sorted_segments(rec_id, include_echo=True)
    new_segs = diarize.relabel_subset(segs_now, turns, pick=lambda s: s.get("speaker_key") == key,
                                      key_for=key_for, name_for=name_for)
    for s in new_segs:
        k = str(s.get("speaker_key") or "")
        if k == "me":
            s["speaker_locked"] = True
        elif k.startswith(key.split(SPLIT_MARK)[0] + SPLIT_MARK):
            s["speaker_locked"] = False
            s["suggestion"] = None
    store.replace_segments(rec_id, new_segs)

    # голоса разделённых — в разметку записи; старый общий образец неверен
    key_embs = {key_for(lab): embs[lab] for lab in labels if embs.get(lab) and lab != owner_label}
    # Угаданный голос владельца — отдельно и не в key_embs: он не должен
    # участвовать в поиске похожих людей и в базу попадает только по кнопке.
    # То же для голоса, узнанного по образцу неуверенно (ниже 80 %): его
    # предлагается добавить ещё одним образцом.
    guess = owner_how == OWNER_GUESS and bool(embs.get(owner_label))
    add_more = (owner_how == OWNER_BY_SAMPLE and bool(embs.get(owner_label))
                and voices.sample_offer(owner_score))
    _save_key_embeddings(rec_id, dict(key_embs, **({OWNER_CANDIDATE: embs[owner_label]}
                                                   if (guess or add_more) else {})))
    if key != "me":
        # образец, подтверждённый кнопкой «Да, это мой голос», переживает повторное разделение
        voices.remove_samples_from(rec_id, key)

    sp = _speakers(store.get(rec_id) or {})
    old = dict(sp.pop(key, None) or {}) if key != "me" else {}
    matches = voices.match_all(key_embs)
    new_keys = []
    for lab in labels:
        if lab == owner_label:
            continue
        k = key_for(lab)
        new_keys.append(k)
        res = matches.get(k)
        entry = {"name": None, "person_id": None, "score": None, "suggestion": None,
                 "confirmed": False, "split_from": key, "split_name": old.get("name") or base_name}
        if res and res.get("confident"):
            entry.update(name=res["name"], person_id=res["person_id"], score=res["score"],
                         sample_offer=voices.sample_offer(res["score"]))
        elif res:
            entry.update(suggestion=dict(res), score=res["score"])
        sp[k] = entry
    store.update(rec_id, {"speakers": sp})
    # подставить узнанные имена в реплики
    segs_now = store.sorted_segments(rec_id, include_echo=True)
    for s in segs_now:
        e = sp.get(str(s.get("speaker_key") or ""))
        if isinstance(e, dict) and e.get("split_from") == key:
            if e.get("name"):
                s["speaker"] = e["name"]
            s["suggestion"] = e.get("suggestion")
    store.replace_segments(rec_id, segs_now)
    splits = dict((store.get(rec_id) or {}).get("splits") or {})
    splits[key] = {"voices": len(labels), "keys": new_keys, "at": datetime.now().isoformat(timespec="seconds"),
                   "owner_note": owner_note, "owner_guess": guess, "owner_add": add_more,
                   "name": old.get("name") or base_name,
                   "entry": {k: v for k, v in old.items() if k not in ("multi_voice",)}}
    store.update(rec_id, {"splits": splits})
    store.refresh_participants(rec_id)
    note("голосов найдено: %d" % len(labels), 1.0)
    log.info("запись %s: «%s» разделён на %d голоса", rec_id, base_name, len(labels))
    return {"voices": len(labels), "keys": new_keys, "owner_note": owner_note}


def unsplit(rec_id: str, key: str) -> int:
    """Отменить разделение: реплики голосов снова одного говорящего."""
    meta = store.get(rec_id) or {}
    splits = dict(meta.get("splits") or {})
    info = splits.pop(key, None) or {}
    base = key.split(SPLIT_MARK)[0]
    prefix = base + SPLIT_MARK
    name = store.SPEAKER_ME if base == "me" else (info.get("name") or base)
    segs = store.sorted_segments(rec_id, include_echo=True)
    changed = 0
    for s in segs:
        k = str(s.get("speaker_key") or "")
        if k.startswith(prefix) or (base == "me" and s.get("track") == store.TRACK_MIC):
            s["speaker_key"] = base
            s["speaker"] = name
            s["speaker_locked"] = base == "me" or bool(info.get("name"))
            s["suggestion"] = None
            changed += 1
    store.replace_segments(rec_id, segs)
    sp = _speakers(store.get(rec_id) or {})
    for k in [k for k in sp if k.startswith(prefix)]:
        voices.remove_samples_from(rec_id, k)
        sp.pop(k, None)
    if base != "me":
        entry = dict(info.get("entry") or {})
        entry.update(multi_voice=None, multi_dismissed=True)
        if info.get("name") and not entry.get("name"):
            entry["name"] = info["name"]
        sp[base] = entry
    store.update(rec_id, {"speakers": sp, "splits": splits})
    if base == "me":
        # Все реплики микрофона снова «Я» — значит, и «меня не было» снято.
        store.update(rec_id, {"room_shared": False, "owner_absent": False})
    store.refresh_participants(rec_id)
    return changed
