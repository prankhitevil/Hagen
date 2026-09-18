# -*- coding: utf-8 -*-
"""Звонок → запись: автостарт, вопрос в конце звонка, склейка с прошлой заметкой.

Правила 13.09.2026:

* звонок начался — запись идёт сразу, без вопроса; в уведомлении кнопка
  «Не писать» (запись снимается и удаляется);
* звонок, похоже, закончился — вопрос «Остановить запись?»; не ответили за
  30 с — запись останавливается сама; вернулся звонок, пока висел вопрос, —
  вопрос снимается, запись идёт дальше;
* новый звонок вскоре после прошлой записи (10 минут) — запись всё равно
  начинается сразу, чтобы не потерять начало разговора, а рядом вопрос
  «дописать в прошлую заметку или оставить новой». Программа видит только то,
  что Teams занял микрофон, и узнать «тот же ли это звонок» наверняка не
  может; если Outlook даёт одинаковую тему встречи, это подсказывается.

Всё это живёт в службе, а не в окне: окно может быть спрятано в трей или
закрыто, а запись звонка начаться обязана.

Вопросы — «подсказки» (prompt): словарь с кнопками. Их показывает окно
программы (событие ``prompt``) и уведомление Windows (слушатели
:meth:`CallAutomation.add_listener`). Ответ из любого места приходит в
:meth:`CallAutomation.answer`.
"""
from __future__ import annotations

import logging
import threading
import time
import uuid
import wave
from dataclasses import dataclass
from typing import Any, Callable

import numpy as np

from . import audio_io, config, platform, store

log = logging.getLogger("hagen.calls")

SR = audio_io.SR


@dataclass
class Hooks:
    """Что автоматика умеет делать со службой. Всё — синхронные вызовы."""

    active_recording: Callable[[], str | None]
    start_recording: Callable[[dict[str, Any] | None], str]
    stop_recording: Callable[[str], None]
    discard_recording: Callable[[str], None]
    merge_and_resume: Callable[[str, str], None]
    publish: Callable[[dict[str, Any]], None]


def _title(rec_id: str | None) -> str:
    meta = store.get(rec_id) if rec_id else None
    return str((meta or {}).get("title") or "запись")


def _subject(meeting: dict[str, Any] | None) -> str:
    subj = " ".join(str((meeting or {}).get("subject") or "").split()).strip()
    return "" if subj == "Без темы" else subj


class CallAutomation:
    def __init__(self, hooks: Hooks) -> None:
        self.hooks = hooks
        self._lock = threading.RLock()
        self.prompt: dict[str, Any] | None = None
        self._timer: threading.Timer | None = None
        self._listeners: list[Callable[[dict[str, Any] | None], None]] = []
        self.call: dict[str, Any] = {"active": False}
        self.linked_rec: str | None = None
        self.last_stopped: dict[str, Any] | None = None

    # ------------------------------------------------ подсказки
    def add_listener(self, fn: Callable[[dict[str, Any] | None], None]) -> None:
        """Уведомления Windows подписываются сюда: None — «вопрос снят»."""
        self._listeners.append(fn)

    def _show(self, kind: str, text: str, buttons: list[tuple[str, str]],
              timeout_s: float | None = None, default: str | None = None,
              **extra: Any) -> dict[str, Any]:
        prompt = {
            "id": uuid.uuid4().hex[:8],
            "kind": kind,
            "title": "Hagen",
            "text": text,
            "buttons": [{"id": b, "label": lbl} for b, lbl in buttons],
            "timeout_s": timeout_s,
            "deadline": (time.time() + timeout_s) if timeout_s else None,
            "default": default,
        }
        prompt.update(extra)
        with self._lock:
            self._cancel_timer()
            self.prompt = prompt
            if timeout_s:
                self._timer = threading.Timer(timeout_s, self._expire, args=(prompt["id"],))
                self._timer.daemon = True
                self._timer.start()
        log.info("вопрос «%s»: %s", kind, text)
        self._broadcast(prompt)
        return prompt

    def _cancel_timer(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

    def _close(self, prompt_id: str | None = None, kinds: tuple[str, ...] | None = None) -> bool:
        with self._lock:
            pr = self.prompt
            if pr is None:
                return False
            if prompt_id is not None and pr["id"] != prompt_id:
                return False
            if kinds is not None and pr["kind"] not in kinds:
                return False
            self.prompt = None
            self._cancel_timer()
        self._broadcast(None, closed=pr["id"])
        return True

    def _broadcast(self, prompt: dict[str, Any] | None, closed: str | None = None) -> None:
        try:
            self.hooks.publish({"type": "prompt", "prompt": prompt, "closed": closed})
        except Exception:
            log.debug("событие подсказки не ушло", exc_info=True)
        for fn in list(self._listeners):
            try:
                fn(prompt)
            except Exception:
                log.warning("слушатель подсказок упал", exc_info=True)

    def _expire(self, prompt_id: str) -> None:
        with self._lock:
            pr = self.prompt
            if pr is None or pr["id"] != prompt_id:
                return
            default = pr.get("default")
        if default:
            log.info("на вопрос «%s» не ответили — делаю «%s»", pr["kind"], default)
            self.answer(prompt_id, default, source="timeout")
        else:
            self._close(prompt_id)

    def notice(self, text: str, level: str = "ok") -> None:
        try:
            self.hooks.publish({"type": "notice", "level": level, "text": text})
        except Exception:
            pass

    # ------------------------------------------------ события звонка
    def on_call_start(self, info: dict[str, Any], meeting: dict[str, Any] | None = None) -> None:
        with self._lock:
            self.call = {"active": True, "info": dict(info or {}), "meeting": meeting,
                         "since": time.time()}
            pr = self.prompt
        if pr is not None and pr["kind"] == "call_ended":
            # микрофон освобождался ненадолго — звонок продолжается
            self._close(pr["id"])
            self.notice("Звонок продолжился — запись идёт дальше.")
            return

        active = self.hooks.active_recording()
        if active:
            self.linked_rec = active
            return

        if not config.get("call_watch_autostart"):
            self._show("call_offer", "Идёт звонок. Записать?",
                       [("record", "Записать"), ("no", "Не сейчас")], timeout_s=60)
            return

        prev = self._recent_stop()
        try:
            rec_id = self.hooks.start_recording(meeting)
        except Exception as err:
            log.warning("звонок начался, но запись не запустилась: %s", err)
            self.notice("Звонок начался, но запись не запустилась: %s" % err, "err")
            return
        self.linked_rec = rec_id

        if prev is not None:
            minutes = max(1, int(round((time.time() - prev["at"]) / 60.0)))
            same = _subject(meeting) and _subject(meeting) == _subject(prev.get("meeting"))
            hint = (" Похоже, та же встреча «%s»." % _subject(meeting)) if same else ""
            self._show(
                "call_resumed",
                "Новый звонок через %d мин после записи «%s». Уже пишу. Дописать в "
                "прошлую заметку или оставить новой?%s" % (minutes, prev["title"], hint),
                [("merge", "Дописать в прошлую"), ("new", "Оставить новой")],
                timeout_s=120, default="new", rec_id=rec_id, prev_id=prev["rec_id"])
        else:
            self._show("call_started", "Идёт звонок — записываю «%s»." % _title(rec_id),
                       [("discard", "Не писать")], timeout_s=45, rec_id=rec_id)

    def on_call_end(self, info: dict[str, Any]) -> None:
        with self._lock:
            self.call = {"active": False, "info": dict(info or {})}
        self._close(kinds=("call_offer", "call_started"))
        active = self.hooks.active_recording()
        if not active or active != self.linked_rec:
            return
        # Открытый вопрос «Дописать в прошлую?» раньше молча подменялся на
        # «Остановить?», и выбор «дописать» пропадал.
        # Снимаем его вслух: запись остаётся отдельной заметкой.
        with self._lock:
            pending = self.prompt
        if pending is not None and pending.get("kind") == "call_resumed":
            self._close(pending["id"])
            self.notice("Звонок закончился раньше, чем вы ответили, дописывать ли его "
                        "в прошлую заметку. Запись оставлена отдельной заметкой.")
        wait = int(config.get("call_end_confirm_s") or 30)
        self._show(
            "call_ended",
            "Звонок, похоже, закончился. Остановить запись «%s»? Без ответа "
            "остановлю через %d с." % (_title(active), wait),
            [("stop", "Остановить"), ("keep", "Писать дальше")],
            timeout_s=wait, default="stop", rec_id=active)

    def recording_stopped(self, rec_id: str) -> None:
        """Служба сообщает: запись остановлена (кнопкой или автоматикой)."""
        meta = store.get(rec_id) or {}
        if meta.get("source") == "live":
            self.last_stopped = {"rec_id": rec_id, "title": _title(rec_id),
                                 "at": time.time(), "meeting": meta.get("meeting")
                                 or (self.call or {}).get("meeting")}
        if self.linked_rec == rec_id:
            self.linked_rec = None
        with self._lock:
            pr = self.prompt
        if pr is not None and rec_id in (pr.get("rec_id"), pr.get("prev_id")):
            self._close(pr["id"])

    def _recent_stop(self) -> dict[str, Any] | None:
        prev = self.last_stopped
        if not prev:
            return None
        window = float(config.get("call_resume_window_s") or 600)
        if time.time() - prev["at"] > window:
            return None
        meta = store.get(prev["rec_id"])
        if not meta or meta.get("source") != "live" or meta.get("media_removed"):
            return None
        return prev

    # ------------------------------------------------ ответы
    def answer(self, prompt_id: str, button: str, source: str = "ui") -> dict[str, Any]:
        with self._lock:
            pr = self.prompt
            if pr is None or pr["id"] != prompt_id:
                return {"ok": False, "reason": "Вопрос уже снят."}
            if button not in {b["id"] for b in pr["buttons"]}:
                return {"ok": False, "reason": "Нет такого ответа."}
        # Снять вопрос может только один ответ: таймер «без ответа остановлю»
        # и клик по кнопке в одну секунду иначе выполнили бы действие дважды.
        if not self._close(prompt_id):
            return {"ok": False, "reason": "Вопрос уже снят."}
        kind = pr["kind"]
        rec_id = pr.get("rec_id")
        log.info("ответ на «%s»: %s (%s)", kind, button, source)
        try:
            if kind == "call_offer" and button == "record":
                self.linked_rec = self.hooks.start_recording((self.call or {}).get("meeting"))
            elif kind == "call_started" and button == "discard":
                if rec_id and self.hooks.active_recording() == rec_id:
                    self.hooks.discard_recording(rec_id)
                    self.linked_rec = None
                    self.notice("Запись звонка отменена и удалена.")
            elif kind == "call_ended" and button == "stop":
                if rec_id and self.hooks.active_recording() == rec_id:
                    self.hooks.stop_recording(rec_id)
                    self.notice("Запись остановлена: звонок закончился."
                                if source == "timeout" else "Запись остановлена.")
            elif kind == "call_ended" and button == "keep":
                self.notice("Пишу дальше. Остановить — кнопкой «Стоп».")
            elif kind == "call_resumed" and button == "merge":
                prev_id = pr.get("prev_id")
                if rec_id and prev_id and self.hooks.active_recording() == rec_id:
                    self.hooks.merge_and_resume(rec_id, prev_id)
                    self.linked_rec = prev_id
                    self.notice("Дописываю в заметку «%s»." % _title(prev_id))
        except Exception as err:
            log.error("ответ «%s» на «%s» не выполнился: %s", button, kind, err, exc_info=True)
            self.notice("Не получилось: %s" % err, "err")
            return {"ok": False, "reason": str(err)}
        return {"ok": True}

    def state(self) -> dict[str, Any]:
        with self._lock:
            return {"prompt": self.prompt, "linked_rec": self.linked_rec,
                    "call": dict(self.call or {})}


# ---------------------------------------------------------------- склейка записей


def merge_recordings(src_id: str, dst_id: str) -> dict[str, Any]:
    """Дописать живую запись src в конец записи dst и удалить src.

    Звук дорожек приклеивается после самой длинной дорожки dst (короткую
    сначала догоняем тишиной — иначе время реплик разъедется), реплики и
    отметки перерывов сдвигаются на ту же величину. Обе записи должны быть
    остановлены.
    """
    from .live import track_frames_on_disk

    src = store.get(src_id)
    dst = store.get(dst_id)
    if not src or not dst:
        raise RuntimeError("одной из записей уже нет")
    base = track_frames_on_disk(dst_id)
    offset = base / float(SR)

    moved = 0          # отсчётов самой длинной приклеенной дорожки
    for track in (store.TRACK_MIC, store.TRACK_FAR):
        s_path = store.track_path(src_id, track)
        if not s_path.exists():
            continue
        writer = audio_io.WavWriter(store.track_path(dst_id, track), append=True)
        copied = 0
        try:
            lag = base - writer.frames
            step = 10 * SR
            while lag > 0:
                n = min(step, lag)
                writer.write(np.zeros(n, dtype=np.int16))
                lag -= n
            with wave.open(str(s_path), "rb") as rd:
                while True:
                    chunk = rd.readframes(step)
                    if not chunk:
                        break
                    pcm = np.frombuffer(chunk, dtype=np.int16)
                    writer.write(pcm)
                    copied += int(pcm.size)
        finally:
            writer.close()
        moved = max(moved, copied)

    dst_segs = store.load_transcript(dst_id).get("segments") or []
    for seg in store.load_transcript(src_id).get("segments") or []:
        seg = dict(seg)
        seg["start"] = round(float(seg.get("start") or 0.0) + offset, 3)
        seg["end"] = round(float(seg.get("end") or 0.0) + offset, 3)
        if isinstance(seg.get("words"), list):
            seg["words"] = [dict(w, start=round(float(w.get("start") or 0) + offset, 3),
                                 end=round(float(w.get("end") or 0) + offset, 3))
                            for w in seg["words"] if isinstance(w, dict)]
        dst_segs.append(seg)
    store.replace_segments(dst_id, dst_segs)

    gaps = list(dst.get("capture_gaps") or [])
    for g in src.get("capture_gaps") or []:
        gaps.append(dict(g, at_s=round(float(g.get("at_s") or 0) + offset, 1)))
    screenshots = (list(dst.get("screenshots") or [])
                   + platform.system().shift_shots(src.get("screenshots") or [], offset))
    tracks = sorted(set(dst.get("tracks") or []) | set(src.get("tracks") or []))
    duration = track_frames_on_disk(dst_id) / float(SR)
    merged_from = list(dst.get("merged_from") or []) + [src_id]
    store.update(dst_id, {"tracks": tracks, "duration_s": round(duration, 2),
                          "capture_gaps": gaps[-50:], "merged_from": merged_from,
                          "screenshots": screenshots})
    store.refresh_participants(dst_id)
    store.delete(src_id)
    log.info("запись %s дописана в %s: звука %.1f c с отметки %.1f c",
             src_id, dst_id, moved / float(SR), offset)
    return {"offset_s": offset, "duration_s": duration}
