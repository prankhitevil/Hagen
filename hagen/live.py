# -*- coding: utf-8 -*-
"""Живой движок записи: две дорожки, распознавание на ходу, правило паузы.

Дорожки принципиально раздельны и никогда не смешиваются:
    mic — микрофон, это всегда «Я»;
    far — звук собеседников (системный звук звонка), это «участники».
Эта граница достоверна на 100%, поэтому разделение говорящих внутри дорожки
собеседников — отдельная задача, она решается после записи (модуль diarize).

Правило пауз (из опыта пользователя): фраза не рвётся на коротких паузах,
закрепляется только после ~2,2 с тишины, и перед закреплением делается ещё один
проход распознавания по всей фразе — иначе теряется хвост речи.

Звук берёт сама служба (DeviceCapture ниже): микрофон через WASAPI, собеседников —
процесс-помощник с петлёй вывода. Захват звука браузером на этой машине не
работал (браузеры микрофон не отдают), и от него отказались.
"""
from __future__ import annotations

import logging
import os
import queue
import threading
import time
from typing import Any, Callable

import numpy as np

from . import asr, audio_io, config, echo, platform, store, vad

log = logging.getLogger("hagen.live")

SR = audio_io.SR
DRAFT_EVERY_S = 1.5          # как часто обновлять черновик
DRAFT_TAIL_S = 12.0          # черновик считаем по последним секундам фразы
MAX_BUFFER_S = 40.0          # защита от бесконечного роста памяти
_GAP = "gap"                 # метка в очереди дорожки: перерыв, залить тишиной


def _write_silence(writer: audio_io.WavWriter, samples: int) -> None:
    """Дописать в дорожку тишину кусками по 10 с — без огромных массивов в памяти."""
    left = int(samples)
    step = 10 * SR
    while left > 0:
        n = min(step, left)
        writer.write(np.zeros(n, dtype=np.int16))
        left -= n


def _cpu_seconds() -> float:
    """Процессорное время всей программы с её запуска, секунды."""
    t = os.times()
    return float(t.user + t.system)


def drafts_enabled() -> bool:
    """Показывать ли черновик фразы, пока человек говорит.

    Черновик — перераспознавание хвоста фразы каждые DRAFT_EVERY_S, и это
    основная нагрузка эфира на процессор. Включается галочкой live_draft и
    только у быстрой модели в звонках — это решает asr.live_route.
    """
    return bool(asr.live_route().get("drafts"))


def track_frames_on_disk(rec_id: str) -> int:
    """Длина самой длинной дорожки записи на диске, в отсчётах.

    Длина берётся по файлу, а не по заголовку: после сбоя заголовок отстаёт,
    и дорожки разошлись бы на длину потерянного сеанса (см. repair_wav_header).
    """
    most = 0
    for track in (store.TRACK_MIC, store.TRACK_FAR):
        path = store.track_path(rec_id, track)
        try:
            if path.exists():
                most = max(most, int(audio_io.repair_wav_header(path)))
        except Exception:
            log.debug("не прочитал длину дорожки %s", path, exc_info=True)
    return most


class TrackPipeline:
    """Одна дорожка: запись в wav, поиск границ фраз, распознавание."""

    def __init__(
        self,
        rec_id: str,
        track: str,
        on_segment: Callable[[str, dict], None] | None = None,
        on_draft: Callable[[str, str, float], None] | None = None,
        on_level: Callable[[str, float], None] | None = None,
        align_to: int = 0,
    ):
        self.rec_id = rec_id
        self.track = track
        self.on_segment = on_segment
        self.on_draft = on_draft
        self.on_level = on_level

        path = store.track_path(rec_id, track)
        # append=True: в одной заметке «Старт» и «Стоп» можно нажимать много раз,
        # и каждый следующий сеанс продолжает ту же дорожку.
        self.writer = audio_io.WavWriter(path, append=True)
        # Время реплики — это номер отсчёта в файле дорожки, поэтому дорожки
        # одной заметки обязаны идти в ногу. Если в прошлом сеансе одна из них
        # писалась меньше (собеседники не открылись, режим «только микрофон»,
        # обрыв), новый сеанс начинаем с общей точки и заливаем разницу
        # тишиной. Иначе реплики собеседников встают на минуты раньше, чем их
        # сказали, — ровно так было на звонке 12.09.
        lag = int(align_to) - self.writer.frames
        if lag > 0:
            _write_silence(self.writer, lag)
            log.info("запись %s: дорожка %s короче другой на %.1f c — выровнял тишиной",
                     rec_id, track, lag / float(SR))
        self.detector = vad.StreamingPhraseDetector(
            silence_ms=int(config.get("silence_finalize_ms") or 2200),
            max_phrase_s=float(config.get("max_phrase_seconds") or 24.0),
            start_sample=self.writer.frames,
        )

        self._q: queue.Queue = queue.Queue(maxsize=2000)
        self._stop = threading.Event()
        self._drain = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name="live-%s-%s" % (track, rec_id[-4:]), daemon=True
        )

        # буфер текущей фразы: держим с запасом, чтобы финальный проход видел всё
        # Счётчик начинаем НЕ с нуля, а с того, что уже записано в дорожке:
        # тогда время реплик второго и следующих сеансов продолжает первый, а не
        # накладывается на него.
        already = self.writer.frames
        self._buf = np.zeros(0, dtype=np.float32)
        self._buf_origin = already    # номер отсчёта, которому соответствует _buf[0]
        self._total = already         # сколько отсчётов в дорожке всего
        self._phrase_start: int | None = None
        self._drafts = drafts_enabled()
        self._last_draft_ts = 0.0
        self._last_draft_text = ""
        # Сколько отсчётов пришло от устройства — вместе с тем, что ещё ждёт в
        # очереди. Звук приходит в темпе речи, поэтому «пришло после конца
        # фразы» — это и есть, сколько секунд человек ждал её в стенограмме.
        self._fed = already
        #: Через сколько секунд после конца речи фразы появлялись в стенограмме.
        self.delays: list[float] = []
        self.level = 0.0
        self.segments_count = 0
        self.error: str | None = None
        self._thread.start()

    # ------------------------------------------------ приём звука
    def feed(self, pcm: np.ndarray) -> None:
        """Вызывается потоком захвата устройства. Только кладёт в очередь."""
        if self._stop.is_set():
            return
        arr = np.asarray(pcm, dtype=np.float32).reshape(-1).copy()
        try:
            self._q.put_nowait(arr)
            self._fed += int(arr.size)
        except queue.Full:
            log.warning("очередь дорожки %s переполнена, кусок отброшен", self.track)

    def feed_gap(self, samples: int) -> bool:
        """Перерыв в звуке: дорожку дополнит тишина той же длины. True — принято.

        Идёт через ту же очередь, что и звук, — тишина встаёт строго между
        последним куском до обрыва и первым после.
        """
        n = int(samples)
        if n <= 0 or self._stop.is_set():
            return False
        try:
            self._q.put((_GAP, n), timeout=2.0)
            self._fed += n
            return True
        except queue.Full:
            log.warning("очередь дорожки %s переполнена, перерыв %.1f c не записан",
                        self.track, n / float(SR))
            return False

    @property
    def seconds(self) -> float:
        return self._total / float(SR)

    @property
    def pending(self) -> int:
        return self._q.qsize()

    # ------------------------------------------------ рабочий поток
    def _run(self) -> None:
        while True:
            try:
                item = self._q.get(timeout=0.2)
            except queue.Empty:
                if self._stop.is_set():
                    break
                if self._drain.is_set():
                    break
                continue
            try:
                if isinstance(item, tuple) and item and item[0] == _GAP:
                    self._skip(int(item[1]))
                else:
                    self._process(item)
            except Exception as err:
                self.error = str(err)
                log.error("дорожка %s: ошибка обработки: %s", self.track, err, exc_info=True)

    def _skip(self, samples: int) -> None:
        """Перерыв: дописать начатую фразу, залить тишиной, перескочить счётчики."""
        pend = self.detector.pending_phrase()
        if pend is not None:
            self._finalize(int(pend["start"]), min(int(pend["end"]), self._total), "gap")
        elif self._last_draft_text and self.on_draft is not None:
            self.on_draft(self.track, "", 0.0)
        self._phrase_start = None
        self._last_draft_text = ""
        _write_silence(self.writer, samples)
        self._total += int(samples)
        self._buf = np.zeros(0, dtype=np.float32)
        self._buf_origin = self._total
        self.detector.jump_to(self._total)
        self.level = 0.0

    def _process(self, pcm: np.ndarray) -> None:
        if pcm.size == 0:
            return
        self.writer.write(pcm)
        self.level = audio_io.rms_level(pcm)
        if self.on_level is not None:
            self.on_level(self.track, self.level)

        self._buf = np.concatenate([self._buf, pcm]) if self._buf.size else pcm
        self._total += int(pcm.size)
        self._trim_buffer()

        events = self.detector.feed(pcm)
        for ev in events:
            if ev["type"] == "speech_start":
                self._phrase_start = int(ev["start"])
                self._last_draft_text = ""
            elif ev["type"] == "phrase_end":
                self._finalize(int(ev["start"]), int(ev["end"]), ev.get("reason", "silence"))

        if self._drafts and self.detector.in_speech and self._phrase_start is not None:
            self._maybe_draft()

    def _trim_buffer(self) -> None:
        """Держим в памяти только то, что может понадобиться текущей фразе."""
        keep_from = self._phrase_start if self._phrase_start is not None else self._total
        keep_from = min(keep_from, self._total)
        limit = self._total - int(MAX_BUFFER_S * SR)
        start = max(self._buf_origin, min(keep_from, max(0, limit)))
        if start > self._buf_origin:
            cut = start - self._buf_origin
            if cut < self._buf.size:
                self._buf = self._buf[cut:]
                self._buf_origin = start

    def _slice(self, a: int, b: int) -> np.ndarray:
        a = max(a, self._buf_origin)
        b = min(b, self._buf_origin + self._buf.size)
        if b <= a:
            return np.zeros(0, dtype=np.float32)
        return self._buf[a - self._buf_origin: b - self._buf_origin]

    # ------------------------------------------------ черновик и закрепление
    def _maybe_draft(self) -> None:
        now = time.time()
        if now - self._last_draft_ts < DRAFT_EVERY_S:
            return
        self._last_draft_ts = now
        start = max(self._phrase_start or 0, self._total - int(DRAFT_TAIL_S * SR))
        piece = self._slice(start, self._total)
        if piece.size < int(0.4 * SR):
            return
        try:
            res = asr.transcribe_live(piece)
        except Exception as err:
            log.debug("черновик не посчитался: %s", err)
            return
        text = (res.text or "").strip()
        if text and text != self._last_draft_text:
            self._last_draft_text = text
            if self.on_draft is not None:
                self.on_draft(self.track, text, start / float(SR))

    def _finalize(self, start: int, end: int, reason: str) -> None:
        """Закрепить фразу: отдельный финальный проход по всей её длине."""
        start = max(0, start)
        end = min(end, self._total)
        if end - start < int(0.25 * SR):
            self._phrase_start = None
            return
        piece = self._slice(start, end)
        if piece.size < int(0.25 * SR):
            self._phrase_start = None
            return
        try:
            res = asr.transcribe_live(piece)
        except Exception as err:
            self.error = str(err)
            log.error("не удалось распознать фразу дорожки %s: %s", self.track, err)
            self._phrase_start = None
            return

        text = (res.text or "").strip()
        # Время слов есть, только если его дала модель эфира (режим одной
        # модели). Отсчитано оно от начала куска, а в реплике — от начала записи.
        words = res.words_at(start / float(SR))
        # Словарь из ручных правок: имена и термины, которые человек уже
        # правил руками, сразу пишем так, как он их поправил (17.09). В словах
        # те же замены, иначе текст и слова разойдутся.
        try:
            from . import fixes

            text = fixes.apply(text)
            if words:
                words = fixes.apply_words(words)
        except Exception:
            log.debug("замены из словаря не применились", exc_info=True)
        self._phrase_start = None
        self._last_draft_text = ""
        if self.on_draft is not None:
            self.on_draft(self.track, "", 0.0)
        if not text:
            return

        seg = store.make_segment(
            track=self.track,
            start=start / float(SR),
            end=end / float(SR),
            text=text,
        )
        seg["reason"] = reason
        # Со временем слов разметка говорящих режет реплику по словам, а не
        # отдаёт её целиком тому, кто говорил дольше. Нет слов — ключа нет.
        if words:
            seg["words"] = words

        # Эхо колонок: если человек слушает совещание не в наушниках, микрофон
        # повторяет слова собеседников. Здесь ловим то, что уже видно в эфире;
        # остальное доберёт полный проход после остановки записи — фраза
        # собеседников может закрыться позже, чем её эхо.
        if self.track == store.TRACK_MIC and echo.enabled():
            try:
                far_segs = [s for s in store.load_transcript(self.rec_id).get("segments") or []
                            if s.get("track") == store.TRACK_FAR]
                if echo.echo_source(seg, far_segs) is not None:
                    seg["echo"] = True
            except Exception:
                log.debug("проверка на эхо не удалась", exc_info=True)

        store.append_segment(self.rec_id, seg)
        self.segments_count += 1
        if seg.get("echo"):
            return
        # Фразу, дописанную по «Стоп» или перерыву, никто не ждал — не в счёт.
        if reason in ("silence", "too_long"):
            self.delays.append(round(max(0, self._fed - end) / float(SR), 2))
        if self.on_segment is not None:
            self.on_segment(self.track, seg)

    # ------------------------------------------------ остановка
    def finish(self, timeout: float = 25.0) -> None:
        """Дожидаемся обработки всего принятого звука и дописываем последнюю фразу."""
        deadline = time.time() + timeout
        self._drain.set()
        while not self._q.empty() and time.time() < deadline:
            time.sleep(0.05)
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=max(1.0, deadline - time.time()))

        # последняя незакрытая фраза: «Стоп» не должен обрывать речь
        pend = self.detector.pending_phrase()
        if pend is not None:
            try:
                self._finalize(int(pend["start"]), min(int(pend["end"]), self._total), "stop")
            except Exception as err:
                log.error("последняя фраза дорожки %s не дописалась: %s", self.track, err)
        self.writer.close()

    def status(self) -> dict[str, Any]:
        return {
            "track": self.track,
            "seconds": round(self.seconds, 2),
            "level": round(self.level, 4),
            "in_speech": self.detector.in_speech,
            "silence_s": round(self.detector.silence_seconds, 2),
            "threshold": round(self.detector.threshold, 3),
            "segments": self.segments_count,
            "pending_chunks": self.pending,
            "error": self.error,
        }


class LiveSession:
    """Запись целиком: дорожки, состояние, корректная остановка."""

    def __init__(
        self,
        rec_id: str,
        mode: str = "online",
        on_event: Callable[[dict], None] | None = None,
    ):
        self.rec_id = rec_id
        self.mode = mode
        self.on_event = on_event
        self.started_at = time.time()
        self.stopped_at: float | None = None
        self.tracks: dict[str, TrackPipeline] = {}
        self.drafts: dict[str, str] = {}
        self._lock = threading.RLock()
        self._stopping = False
        # Общая точка старта дорожек этого сеанса, в отсчётах. Считается один
        # раз, до первой записи: пока файлы закрыты, их длина на диске честная.
        self._base_frames: int | None = None
        # Процессорное время всей программы к началу записи — для сводки стенда.
        self._cpu0 = _cpu_seconds()

    # ------------------------------------------------ события наружу
    def _emit(self, payload: dict[str, Any]) -> None:
        if self.on_event is None:
            return
        try:
            self.on_event(payload)
        except Exception:
            log.debug("слушатель событий записи упал", exc_info=True)

    def _on_segment(self, track: str, seg: dict[str, Any]) -> None:
        self._emit({"type": "segment", "rec_id": self.rec_id, "segment": seg})
        store.update(self.rec_id, {"duration_s": round(self.duration, 2),
                                   "tracks": sorted(self.tracks.keys())})

    def _on_draft(self, track: str, text: str, start_s: float) -> None:
        with self._lock:
            if text:
                self.drafts[track] = text
            else:
                self.drafts.pop(track, None)
            snapshot = dict(self.drafts)
        self._emit({"type": "draft", "rec_id": self.rec_id, "drafts": snapshot})

    def _on_level(self, track: str, level: float) -> None:
        # уровни шлём не на каждый кусок, частоту ограничивает сервер
        self._emit({"type": "level", "rec_id": self.rec_id, "track": track,
                    "level": round(level, 4)})

    # ------------------------------------------------ дорожки
    def ensure_track(self, track: str) -> TrackPipeline:
        with self._lock:
            if self._stopping:
                raise RuntimeError("запись уже останавливается")
            tp = self.tracks.get(track)
            if tp is None:
                tp = TrackPipeline(
                    self.rec_id, track,
                    on_segment=self._on_segment,
                    on_draft=self._on_draft,
                    on_level=self._on_level,
                    align_to=self.base_frames,
                )
                self.tracks[track] = tp
                log.info("запись %s: открыта дорожка %s", self.rec_id, track)
                store.update(self.rec_id, {"tracks": sorted(self.tracks.keys())})
            return tp

    def feed(self, track: str, pcm: np.ndarray) -> None:
        self.ensure_track(track).feed(pcm)

    @property
    def base_frames(self) -> int:
        """С какого отсчёта начинаются все дорожки этого сеанса."""
        with self._lock:
            if self._base_frames is None:
                self._base_frames = track_frames_on_disk(self.rec_id)
            return self._base_frames

    def emit(self, payload: dict[str, Any]) -> None:
        """Событие в интерфейс от устройств записи."""
        self._emit(payload)

    def note_gap(self, track: str, at_s: float, gap_s: float) -> None:
        """Запомнить в карточке записи, где звук дорожки прерывался."""
        try:
            meta = store.get(self.rec_id) or {}
            gaps = list(meta.get("capture_gaps") or [])
            gaps.append({"track": track, "at_s": round(float(at_s), 1),
                         "gap_s": round(float(gap_s), 1)})
            store.update(self.rec_id, {"capture_gaps": gaps[-50:]})
        except Exception:
            log.debug("перерыв в звуке не записан в карточку", exc_info=True)

    @property
    def duration(self) -> float:
        with self._lock:
            if not self.tracks:
                return max(0.0, (self.stopped_at or time.time()) - self.started_at)
            return max(tp.seconds for tp in self.tracks.values())

    @property
    def active(self) -> bool:
        return self.stopped_at is None

    def status(self) -> dict[str, Any]:
        with self._lock:
            tracks = {k: v.status() for k, v in self.tracks.items()}
            drafts = dict(self.drafts)
        return {
            "rec_id": self.rec_id,
            "mode": self.mode,
            "active": self.active,
            "duration_s": round(self.duration, 2),
            "started_at": self.started_at,
            "tracks": tracks,
            "drafts": drafts,
        }

    # ------------------------------------------------ остановка
    def stop(self) -> dict[str, Any]:
        with self._lock:
            if self._stopping:
                return self.status()
            self._stopping = True
            tracks = list(self.tracks.values())

        log.info("запись %s: останавливаю, дописываю последние фразы", self.rec_id)
        for tp in tracks:
            try:
                tp.finish()
            except Exception as err:
                log.error("дорожка %s не закрылась чисто: %s", tp.track, err)

        self.stopped_at = time.time()
        with self._lock:
            self.drafts.clear()
        duration = self.duration
        store.update(self.rec_id, {
            "status": "recorded",
            "duration_s": round(duration, 2),
            "tracks": sorted(self.tracks.keys()),
            "asr_stand": self._stand_summary(tracks),
        })
        store.refresh_participants(self.rec_id)
        self._emit({"type": "draft", "rec_id": self.rec_id, "drafts": {}})
        log.info("запись %s: остановлена, длительность %.1f c", self.rec_id, duration)
        return self.status()

    def _stand_summary(self, tracks: list[TrackPipeline]) -> dict[str, Any]:
        """Какой моделью распознавалась запись и как она себя вела.

        Модели распознавания владелец сравнивает на настоящих звонках (стенд
        18.09, выбор параметрами с 21.09); эта сводка — то, по чему сравнивать:
        сколько процессора занимала программа (в ядрах, работающих на полную, в
        среднем за запись) и через сколько секунд после конца речи фраза
        появлялась в стенограмме. Процессор — всей программы, вместе с тем, что
        шло параллельно записи.
        """
        route = asr.live_route()
        wall = max(1.0, (self.stopped_at or time.time()) - self.started_at)
        cores = max(0.0, _cpu_seconds() - self._cpu0) / wall
        delays = sorted(d for tp in tracks for d in tp.delays)
        live_title = asr.ENGINE_TITLES.get(route.get("live"), route.get("live") or "")
        if route.get("drafts"):
            live_title += ", с бегущим текстом"
        title = live_title[:1].upper() + live_title[1:]
        out: dict[str, Any] = {"key": route.get("key"), "title": title,
                               "cpu_cores": round(cores, 2), "phrases": len(delays)}
        text = "%s. Эфир — %s: процессор %.1f ядра в среднем" % (
            route.get("title"), live_title, cores)
        if delays:
            out["delay_median_s"] = delays[len(delays) // 2]
            out["delay_max_s"] = delays[-1]
            text += ("; фраза появлялась через %.1f с после конца речи, самая долгая — "
                     "через %.1f с" % (out["delay_median_s"], out["delay_max_s"]))
        if route.get("why"):
            out["why"] = route["why"]
            text += ". " + route["why"]
        out["text"] = text
        log.info("запись %s: %s", self.rec_id, text)
        return out


class SessionManager:
    """Держит активные записи. Запись переживает закрытие и перезагрузку окна."""

    def __init__(self, on_event: Callable[[dict], None] | None = None):
        self.on_event = on_event
        self._sessions: dict[str, LiveSession] = {}
        self._lock = threading.RLock()

    def start(self, rec_id: str, mode: str = "online") -> LiveSession:
        with self._lock:
            sess = self._sessions.get(rec_id)
            if sess is not None and sess.active:
                return sess
            sess = LiveSession(rec_id, mode=mode, on_event=self.on_event)
            self._sessions[rec_id] = sess
            return sess

    def get(self, rec_id: str) -> LiveSession | None:
        with self._lock:
            return self._sessions.get(rec_id)

    def active_session(self) -> LiveSession | None:
        with self._lock:
            for sess in self._sessions.values():
                if sess.active:
                    return sess
        return None

    def stop(self, rec_id: str) -> dict[str, Any] | None:
        sess = self.get(rec_id)
        if sess is None:
            return None
        return sess.stop()

    def stop_all(self) -> None:
        with self._lock:
            ids = [k for k, v in self._sessions.items() if v.active]
        for rec_id in ids:
            try:
                self.stop(rec_id)
            except Exception as err:
                log.error("не удалось остановить запись %s: %s", rec_id, err)

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {k: v.status() for k, v in self._sessions.items() if v.active}


class _Feed:
    """Вход дорожки от устройства: подаёт звук и помнит, сколько подано.

    Первый кусок после запуска устройства сверяется с часами: если дорожка
    отстала, недостающее заливается тишиной (см. DeviceCapture._align).
    """

    def __init__(self, owner: "DeviceCapture", track: str, pipeline: TrackPipeline):
        self.owner = owner
        self.track = track
        self.pipeline = pipeline
        self.fed = 0             # отсчётов подано в этом сеансе, вместе с тишиной
        self.fresh = True        # следующий кусок — первый после запуска устройства
        self.after_gap = False   # устройство поднято заново после обрыва
        self.restarts = 0

    def __call__(self, pcm: np.ndarray) -> None:
        n = int(np.asarray(pcm).size)
        if self.fresh:
            self.fresh = False
            self.owner._align(self, n)
        self.pipeline.feed(pcm)
        self.fed += n


class DeviceCapture:
    """Привязывает устройства записи к сеансу и держит их живыми.

    Откуда берётся звук:
        микрофон     — WASAPI средствами самой службы, запасной путь — ffmpeg;
        собеседники  — процесс hagen.loopback_worker, петля вывода WASAPI.

    Сторож. У каждой дорожки свой поток, который раз в полсекунды проверяет:
    жив ли поток записи и идёт ли из него звук. Оборвалось — поднимает
    устройство заново, а пропавшее время заливает тишиной, чтобы реплики не
    съехали. Так было нужно после звонка 12.09: Teams перенастраивал звук, петля
    вывода закрывалась, помощник выходил — и до «Стоп» собеседники не писались
    вовсе (6 минут из 15), а их последние реплики встали на 6 минут раньше.
    """

    WATCH_EVERY_S = 0.5
    STRIKES_DEAD = 2       # поток закрылся: столько проверок подряд, ≈1 с
    STRIKES_STALL = 6      # поток жив, но молчит: ещё ≈3 с сверх порога устройства
    RETRY_DELAYS_S = (0.5, 1.0, 2.0, 4.0, 8.0, 15.0)
    ALIGN_MIN_S = 0.3      # расхождение меньше — это дрожь буферов, не перерыв

    LABELS = {store.TRACK_MIC: "микрофон", store.TRACK_FAR: "звук собеседников"}

    def __init__(self, session: "LiveSession"):
        self.session = session
        self.mic = None
        self.far = None
        self.mic_error: str | None = None
        self.far_error: str | None = None
        self._feeds: dict[str, _Feed] = {}
        self._lost: dict[str, dict[str, float]] = {}
        self._lock = threading.RLock()
        self._halt = threading.Event()
        self._watchers: list[threading.Thread] = []
        self._t_zero: float | None = None

    # ------------------------------------------------ открыть устройство
    def _open_mic(self, feed: _Feed):
        """Микрофон: сначала своими силами (WASAPI), потом ffmpeg.

        Свой путь лучше: для доступа к звуку достаточно доверять ОДНОМУ файлу —
        python.exe из папки приложения.
        """
        rec_id = self.session.rec_id
        errors: list[str] = []
        try:
            idx = config.get("mic_device_index")
            rec = platform.audio().open_mic(
                int(idx) if idx is not None else None, feed)
            rec.start()
            log.info("запись %s: микрофон «%s» (служба)", rec_id, rec.device_name)
            return rec
        except Exception as err:
            errors.append("свой путь: %s" % err)
            log.info("микрофон через службу не открылся: %s", err)
        try:
            want = config.get("mic_device_name")
            sound = platform.audio()
            dev = sound.find_device(want) if want else None
            if dev is None:
                dev = sound.default_mic_device()
            if dev is None:
                raise RuntimeError("микрофон не найден")
            rec = sound.open_ffmpeg(dev, feed, "микрофон")
            rec.start()
            log.info("запись %s: микрофон «%s» (ffmpeg)", rec_id, dev.get("name"))
            return rec
        except Exception as err:
            errors.append("ffmpeg: %s" % err)
        raise RuntimeError(" | ".join(errors)[:400])

    def _call_device(self) -> str | None:
        """Куда программа звонка выводит звук (None — звонка не видно)."""
        try:
            found = platform.desktop().call_output_device()
        except Exception:
            log.debug("устройство звонка не определилось", exc_info=True)
            return None
        return str(found["device"]) if found else None

    def _open_far(self, feed: _Feed):
        """Собеседники: процесс-помощник с петлёй вывода.

        Устройство — то, куда программа звонка реально выводит звук; если звонка
        не видно — устройство связи Windows, как раньше.
        """
        idx = config.get("far_device_index")
        want = self._call_device() if idx is None else None
        rec = platform.audio().open_loopback_process(
            int(idx) if idx is not None else None, feed, want_name=want)
        rec.start()
        log.info("запись %s: собеседники «%s»%s", self.session.rec_id, rec.device_name,
                 (" — %s" % rec.choice) if rec.choice else "")
        # Разбирать потом жалобы на двойники проще, когда в журнале сразу
        # сказано, что звук шёл в динамики (случай 17.09).
        try:
            risk = platform.audio().echo_risk(rec.device_name or "")
            if risk["risk"]:
                log.warning("запись %s: %s — микрофон услышит собеседников, будут двойники",
                            self.session.rec_id, risk["why"])
        except Exception:
            log.debug("проверка устройства на эхо не удалась", exc_info=True)
        return rec

    FOLLOW_EVERY = 6        # раз в столько проверок сторожа (≈3 с) сверяем устройство звонка
    FOLLOW_STRIKES = 2      # и переключаемся, только если расхождение держится

    def _device_mismatch(self, rec) -> str | None:
        """Звонок выводит звук не туда, откуда пишем? Вернёт имя нужного устройства."""
        if config.get("far_device_index") is not None:
            return None
        want = self._call_device()
        have = str(getattr(rec, "device_name", "") or "")
        if not want or not have:
            return None
        return None if platform.audio().same_device(want, have) else want

    def _get(self, track: str):
        return self.mic if track == store.TRACK_MIC else self.far

    def _set(self, track: str, rec) -> None:
        if track == store.TRACK_MIC:
            self.mic = rec
        else:
            self.far = rec

    # ------------------------------------------------ запуск
    def start(self, want_far: bool = True) -> dict[str, Any]:
        rec_id = self.session.rec_id

        feed = _Feed(self, store.TRACK_MIC, self.session.ensure_track(store.TRACK_MIC))
        self._feeds[store.TRACK_MIC] = feed
        try:
            self.mic = self._open_mic(feed)
        except Exception as err:
            self.mic = None
            self.mic_error = str(err)[:400]
            log.error("микрофон не открылся: %s", self.mic_error)
            self._mark_lost(store.TRACK_MIC, tried=True)

        if want_far:
            try:
                pipeline = self.session.ensure_track(store.TRACK_FAR)
                feed = _Feed(self, store.TRACK_FAR, pipeline)
                self._feeds[store.TRACK_FAR] = feed
                self.far = self._open_far(feed)
            except Exception as err:
                self.far = None
                self.far_error = str(err)
                log.warning("петля вывода не открылась: %s", err)
                if store.TRACK_FAR in self._feeds:
                    self._mark_lost(store.TRACK_FAR, tried=True)

        # Сторож нужен, только если запись вообще пошла: когда не открылось
        # ничего, служба сама снимает пустую запись.
        if self.mic is not None or self.far is not None:
            for track in list(self._feeds):
                th = threading.Thread(target=self._watch, args=(track,),
                                      name="watch-%s-%s" % (track, rec_id[-4:]),
                                      daemon=True)
                self._watchers.append(th)
                th.start()
        return self.status()

    # ------------------------------------------------ сторож
    def _mark_lost(self, track: str, tried: bool = False) -> None:
        now = time.time()
        self._lost[track] = {
            "since": now,
            "attempts": 1 if tried else 0,
            "next": now + (self.RETRY_DELAYS_S[0] if tried else 0.0),
        }
        feed = self._feeds.get(track)
        if feed is not None:
            feed.after_gap = True

    @staticmethod
    def _trouble(rec) -> str | None:
        try:
            if not rec.alive:
                return "поток записи закрылся"
            if rec.stalled:
                return "устройство перестало отдавать звук"
        except Exception as err:
            return "устройство не отвечает: %s" % err
        return None

    def _watch(self, track: str) -> None:
        strikes = 0
        ticks = 0
        moved = 0
        while not self._halt.wait(self.WATCH_EVERY_S):
            ticks += 1
            try:
                if track in self._lost:
                    strikes = 0
                    self._reopen(track)
                    continue
                rec = self._get(track)
                if rec is None:
                    continue
                trouble = self._trouble(rec)
                if trouble is None and track == store.TRACK_FAR and ticks % self.FOLLOW_EVERY == 0:
                    # Звонок вывели на другое устройство (или мы с начала пишем
                    # не то) — переходим за ним.
                    want = self._device_mismatch(rec)
                    moved = moved + 1 if want else 0
                    if want and moved >= self.FOLLOW_STRIKES:
                        moved = 0
                        self._move_far(rec, want)
                        continue
                if trouble is None:
                    strikes = 0
                    continue
                strikes += 1
                need = self.STRIKES_STALL if "перестало" in trouble else self.STRIKES_DEAD
                if strikes < need:
                    continue
                strikes = 0
                self._drop(track, rec, trouble)
                self._reopen(track)
            except Exception:
                log.warning("сторож дорожки %s споткнулся", track, exc_info=True)

    def _move_far(self, rec, want: str) -> None:
        """Перейти записью собеседников на устройство, куда играет звонок."""
        with self._lock:
            if self._halt.is_set() or self.far is not rec:
                return
            self.far = None
            self._mark_lost(store.TRACK_FAR)
        log.warning("запись %s: звонок выводит звук в «%s», а собеседники писались с «%s» — "
                    "переключаю", self.session.rec_id, want, getattr(rec, "device_name", "?"))
        self.session.emit({"type": "notice", "level": "ok",
                           "text": "Звук звонка идёт в «%s» — переключил туда запись собеседников."
                                   % want})
        try:
            rec.stop()
        except Exception:
            log.debug("прежнее устройство не закрылось чисто", exc_info=True)
        self._reopen(store.TRACK_FAR)

    def _drop(self, track: str, rec, trouble: str) -> None:
        with self._lock:
            if self._halt.is_set() or self._get(track) is not rec:
                return
            self._set(track, None)
            self._mark_lost(track)
        label = self.LABELS.get(track, track)
        detail = getattr(rec, "error", None)
        log.warning("запись %s: %s — %s%s. Переподключаю.", self.session.rec_id, label,
                    trouble, (" (%s)" % detail) if detail else "")
        self.session.emit({
            "type": "notice", "level": "err",
            "text": ("Звук собеседников прервался — переподключаю."
                     if track == store.TRACK_FAR
                     else "Микрофон перестал отдавать звук — переподключаю."),
        })
        try:
            rec.stop()
        except Exception:
            log.debug("оборванное устройство не закрылось чисто", exc_info=True)

    def _reopen(self, track: str) -> None:
        info = self._lost.get(track)
        if info is None or self._halt.is_set() or time.time() < info["next"]:
            return
        info["attempts"] += 1
        attempt = int(info["attempts"])
        label = self.LABELS.get(track, track)
        feed = self._feeds[track]
        feed.fresh = True
        try:
            rec = self._open_mic(feed) if track == store.TRACK_MIC else self._open_far(feed)
        except Exception as err:
            delay = self.RETRY_DELAYS_S[min(attempt - 1, len(self.RETRY_DELAYS_S) - 1)]
            info["next"] = time.time() + delay
            # на долгом отказе журнал не засоряем: первые попытки и каждая двадцатая
            if attempt <= 2 or attempt % 20 == 0:
                log.warning("запись %s: %s не переподключился (попытка %d): %s",
                            self.session.rec_id, label, attempt, str(err)[:200])
            return

        with self._lock:
            halted = self._halt.is_set()
            if not halted:
                self._set(track, rec)
                self._lost.pop(track, None)
                feed.restarts += 1
                if track == store.TRACK_MIC:
                    self.mic_error = None
                else:
                    self.far_error = None
        if halted:
            try:
                rec.stop()
            except Exception:
                pass
            return
        log.warning("запись %s: %s снова пишется (попытка %d, без звука %.1f c)",
                    self.session.rec_id, label, attempt, time.time() - info["since"])

    def _align(self, feed: _Feed, block: int) -> None:
        """Первый кусок после запуска устройства: догнать часы тишиной.

        Сверка ТОЛЬКО на стыке, а не на каждом куске: звук приходит пачками,
        и постоянная подгонка под часы вставляла бы тишину на ровном месте.
        """
        # Часы — monotonic: стенное время прыгает при синхронизации с сервером
        # времени, и перерыв получил бы чужую длину.
        captured_at = time.monotonic() - block / float(SR)
        with self._lock:
            if self._t_zero is None:
                self._t_zero = captured_at
                return
            t_zero = self._t_zero
        after_gap = feed.after_gap
        feed.after_gap = False
        label = self.LABELS.get(feed.track, feed.track)
        gap = int(round((captured_at - t_zero) * SR)) - feed.fed
        gap_s = max(0, gap) / float(SR)

        if gap >= int(self.ALIGN_MIN_S * SR):
            at_s = (self.session.base_frames + feed.fed) / float(SR)
            # Тишина не встала в очередь — счётчик поданного не двигаем, иначе
            # дорожка считалась бы длиннее, чем она есть на диске.
            if feed.pipeline.feed_gap(gap):
                feed.fed += gap
            if after_gap:
                log.warning("запись %s: %s — перерыв %.1f c с отметки %.1f c залит тишиной",
                            self.session.rec_id, label, gap_s, at_s)
                self.session.note_gap(feed.track, at_s, gap_s)
            else:
                log.info("запись %s: %s включился на %.1f c позже — выровнял тишиной",
                         self.session.rec_id, label, gap_s)
        if after_gap:
            self.session.emit({
                "type": "notice", "level": "ok",
                "text": "%s снова пишется. Перерыв — %s." % (
                    label[:1].upper() + label[1:],
                    "меньше секунды" if gap_s < 1.0 else "%.0f с" % gap_s),
            })

    # ------------------------------------------------ остановка и состояние
    def stop(self) -> None:
        self._halt.set()
        for th in self._watchers:
            if th.is_alive() and th is not threading.current_thread():
                th.join(timeout=8.0)
        self._watchers = []
        with self._lock:
            recs = (self.mic, self.far)
            self.mic = None
            self.far = None
        for rec in recs:
            if rec is None:
                continue
            try:
                rec.stop()
            except Exception as err:
                log.warning("устройство не закрылось чисто: %s", err)

    def status(self) -> dict[str, Any]:
        mic, far = self.mic, self.far
        out: dict[str, Any] = {
            "mic": mic.status() if mic is not None else None,
            "far": far.status() if far is not None else None,
            "mic_error": self.mic_error,
            "far_error": self.far_error,
            "mic_lost": store.TRACK_MIC in self._lost,
            "far_lost": store.TRACK_FAR in self._lost,
            "restarts": {t: f.restarts for t, f in self._feeds.items()},
        }
        return out

    @property
    def levels(self) -> dict[str, float]:
        return {
            "mic": round(self.mic.level, 4) if self.mic is not None else 0.0,
            "far": round(self.far.level, 4) if self.far is not None else 0.0,
        }
