# -*- coding: utf-8 -*-
"""Отсев эха колонок из дорожки микрофона — на уровне фраз, а не сигнала.

Если человек слушает совещание через колонки, микрофон слышит собеседников, и
их слова попадают сразу в обе дорожки: в «участников» — из петли вывода, и в
«меня» — из воздуха. В стенограмме получаются двойные реплики, а разметка
говорящих приписывает чужие слова владельцу микрофона.

Почему не вычитаем эхо из звука. Дорожки пишут РАЗНЫЕ устройства со своими
тактовыми генераторами, и каждая приводится к 16 кГц независимо. На замере
(колонки SVEN + микрофон Brio) эхо приходило с задержкой 665 мс, и задержка
уплывала примерно на 0,2 мс в секунду. Линейный фильтр растяжение времени не
моделирует: оптимальное вычитание дало 5,6 дБ подавления при нужных 25-30 дБ.
Teams такой беды не знает — он давит эхо внутри себя, одним процессом и с
общими часами, а мы берём микрофон сырым, до Teams.

Зато на уровне текста задача решается надёжно: сравниваем не отсчёты, а фразы.
Уплывающая задержка тут не мешает вовсе.

Сверяемся в два захода (17.09). Сперва с каждой фразой собеседников по
отдельности, а если ни одна не подошла — со всей их речью в окне сразу:
микрофон часто слепляет в одну реплику то, что у собеседников разрезано
на две фразы, и по отдельности ни одна порога не набирает.

Реплики не удаляются: они помечаются `echo: true` и просто не показываются.
"""
from __future__ import annotations

import logging
import re
from typing import Any

from . import config, store

log = logging.getLogger("hagen.echo")

# Насколько далеко друг от друга могут стоять фраза и её эхо. Складывается из
# задержки колонок, разного момента старта дорожек и того, что тишину в двух
# дорожках Silero режет не в одних и тех же местах.
WINDOW_S = 3.0

# Короткие реплики сравниваем строже: «да, хорошо» человек мог сказать и сам,
# повторив за собеседником, и терять такую реплику обидно.
SHORT_WORDS = 4
SHORT_THRESHOLD = 0.9

_PUNCT = re.compile(r"[^\w\s-]+", re.UNICODE)
_SPACES = re.compile(r"\s+")


def _words(text: str) -> list[str]:
    s = _PUNCT.sub(" ", str(text or "").lower().replace("ё", "е"))
    return [w for w in _SPACES.sub(" ", s).strip().split(" ") if w]


def _threshold() -> float:
    try:
        return max(0.1, min(1.0, float(config.get("echo_match_threshold") or 0.6)))
    except (TypeError, ValueError):
        return 0.6


def _contained(mic_words: list[str], far_words: list[str]) -> float:
    """Доля слов фразы микрофона, нашедшихся в фразе собеседников.

    Именно доля вхождения, а не взаимное сходство: эхо распознаётся хуже
    оригинала и обычно короче его, а кусок чужой длинной реплики — это всё
    равно эхо.
    """
    if not mic_words:
        return 0.0
    pool: dict[str, int] = {}
    for w in far_words:
        pool[w] = pool.get(w, 0) + 1
    hit = 0
    for w in mic_words:
        if pool.get(w, 0) > 0:
            pool[w] -= 1
            hit += 1
    return hit / float(len(mic_words))


def _overlaps(mic: dict[str, Any], far: dict[str, Any]) -> bool:
    m0, m1 = float(mic.get("start") or 0.0), float(mic.get("end") or 0.0)
    f0, f1 = float(far.get("start") or 0.0), float(far.get("end") or 0.0)
    return m0 < f1 + WINDOW_S and f0 < m1 + WINDOW_S


def echo_source(mic_seg: dict[str, Any], far_segs: list[dict[str, Any]],
                far_words: dict[str, list[str]] | None = None) -> dict[str, Any] | None:
    """Фраза собеседников, эхом которой является реплика микрофона, либо None.

    far_words — заранее разобранные слова фраз собеседников по их id: при
    проходе по всей стенограмме одни и те же фразы иначе разбирались бы
    заново для каждой реплики микрофона. Результат тот же.
    """
    mic_words = _words(mic_seg.get("text"))
    if not mic_words:
        return None
    need = SHORT_THRESHOLD if len(mic_words) < SHORT_WORDS else _threshold()

    best = None
    best_score = 0.0
    pool: list[str] = []
    near = 0
    for far in far_segs:
        if not _overlaps(mic_seg, far):
            continue
        words = far_words.get(str(far.get("id"))) if far_words is not None else None
        if words is None:
            words = _words(far.get("text"))
        near += 1
        pool.extend(words)
        score = _contained(mic_words, words)
        if best is None or score > best_score:
            best, best_score = far, score
    if best is None:
        return None
    if best_score >= need:
        return best
    # Тишину в двух дорожках Silero режет в разных местах, поэтому одна реплика
    # микрофона нередко накрывает ДВЕ фразы собеседников сразу: с каждой по
    # отдельности совпадение ниже порога, и эхо проходило в текст (случай 17.09
    # — «Готовы её привести оперативно…» и продолжение). Поэтому
    # вторым заходом сверяемся со всей речью собеседников в окне.
    if near > 1 and _contained(mic_words, pool) >= need:
        return best
    return None


def enabled() -> bool:
    return bool(config.get("echo_filter"))


# ------------------------------------------------------- отсев по голосу
#
# Сравнение текстов спотыкается там, где дорожки расслышали по-разному:
# «неделя 2665» у собеседника против «недели две, 665» в микрофоне — звуки те
# же, слова разные (случай 17.09). Голос в такой ловушки не попадает.
#
# Судим только «своими силами записи», без базы голосов: реплики микрофона, у
# которых собеседники в это время молчали, — это заведомо владелец, они и дают
# образец его голоса. Если реплика ближе к голосу собеседника, чем к этому
# образцу, — это эхо.

#: Насколько увереннее должно быть сходство с собеседником, чтобы счесть эхом.
MARGIN = 0.05
#: Меньше этого числа чистых реплик владельца — судить не по чему.
MIN_OWN = 2


def voice_enabled() -> bool:
    return bool(config.get("echo_filter")) and bool(config.get("echo_by_voice"))


def _margin() -> float:
    """Запас из настроек. Ноль — это «без запаса», а не «настройка не задана»."""
    raw = config.get("echo_voice_margin")
    if raw is None or raw == "":
        return MARGIN
    try:
        return max(0.0, min(0.5, float(raw)))
    except (TypeError, ValueError):
        return MARGIN


def _cos(a: list[float], b: list[float]) -> float:
    import numpy as np

    va, vb = np.asarray(a, dtype=np.float32), np.asarray(b, dtype=np.float32)
    if va.shape != vb.shape or not va.size:
        return -1.0
    na, nb = float(np.linalg.norm(va)), float(np.linalg.norm(vb))
    if na <= 0 or nb <= 0:
        return -1.0
    return float(va.dot(vb) / (na * nb))


def _mean(vectors: list[list[float]]) -> list[float] | None:
    import numpy as np

    good = [np.asarray(v, dtype=np.float32) for v in vectors if v]
    if not good or len({v.shape for v in good}) != 1:
        return None
    return np.mean(np.stack(good), axis=0).tolist()


def mark_by_voice(rec_id: str, embed_fn: Any = None) -> int:
    """Отсеять эхо по голосу. Возвращает число вновь помеченных реплик.

    Зовётся после разметки говорящих: к этому времени понятно, где чья речь, а
    звук уже прочитан. Сам подсчёт отпечатков — десятки миллисекунд на реплику,
    поэтому к длительности разметки добавляются секунды, а не минуты.
    """
    if not voice_enabled():
        return 0
    from . import audio_io, diarize

    embed_fn = embed_fn or diarize.embed_spans
    with store._lock_for(rec_id):
        data = store.load_transcript(rec_id)
        segments = data.get("segments") or []
        far_segs = [s for s in segments if s.get("track") == store.TRACK_FAR]
        mic_segs = [s for s in segments if s.get("track") == store.TRACK_MIC
                    and not s.get("echo_locked")]
        if not far_segs or not mic_segs:
            return 0

        suspect = [s for s in mic_segs if any(_overlaps(s, f) for f in far_segs)]
        own = [s for s in mic_segs
               if not s.get("echo") and not any(_overlaps(s, f) for f in far_segs)]
        suspect = [s for s in suspect if not s.get("echo")]
        if not suspect or len(own) < MIN_OWN:
            return 0

        wav = store.track_path(rec_id, store.TRACK_MIC)
        if not wav.exists():
            return 0
        pcm, sr = audio_io.read_wav(wav)
        spans = [{"start": float(s.get("start") or 0.0), "end": float(s.get("end") or 0.0)}
                 for s in own + suspect]
        vecs = embed_fn(pcm, spans, sr)
        mine = _mean(vecs[:len(own)])
        if mine is None:
            return 0

        # Голоса собеседников — из разметки; своей дорожки у них нет, поэтому
        # берём отпечатки, посчитанные разметкой при обходе записи.
        result = diarize.load_result(rec_id) or {}
        theirs = [v for v in (result.get("key_embeddings")
                              or result.get("embeddings") or {}).values() if v]
        if not theirs:
            return 0

        edge = _margin()
        marked = 0
        for seg, vec in zip(suspect, vecs[len(own):]):
            if not vec:
                continue
            like_me = _cos(vec, mine)
            like_them = max(_cos(vec, t) for t in theirs)
            if like_them - like_me < edge:
                continue
            seg["echo"] = True
            seg["echo_by"] = "голос"
            marked += 1
        if marked:
            store._write_json(store.paths(rec_id)["transcript"], data)
    if marked:
        log.info("запись %s: отсеяно эхо по голосу, реплик — %d", rec_id, marked)
    return marked


def mark(rec_id: str) -> int:
    """Пройти по всей стенограмме и пометить эхо. Возвращает число помеченных.

    Вызывается после остановки записи: к этому моменту обе дорожки закончены, и
    видно даже те совпадения, которых не было видно в эфире, — когда фраза
    собеседников закрылась позже, чем её эхо в микрофоне.
    """
    if not enabled():
        return 0
    with store._lock_for(rec_id):
        data = store.load_transcript(rec_id)
        segments = data.get("segments") or []
        far_segs = [s for s in segments if s.get("track") == store.TRACK_FAR]
        if not far_segs:
            return 0
        far_words = {str(s.get("id")): _words(s.get("text")) for s in far_segs}
        marked = 0
        for seg in segments:
            if seg.get("track") != store.TRACK_MIC or seg.get("echo_locked"):
                continue
            src = echo_source(seg, far_segs, far_words)
            was = bool(seg.get("echo"))
            if src is not None:
                seg["echo"] = True
                seg["echo_of"] = src.get("id")
                if not was:
                    marked += 1
            elif was:
                seg.pop("echo", None)
                seg.pop("echo_of", None)
        store._write_json(store.paths(rec_id)["transcript"], data)
    if marked:
        log.info("запись %s: отсеяно эхо колонок, реплик — %d", rec_id, marked)
    return marked
