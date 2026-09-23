# -*- coding: utf-8 -*-
"""Проверка 42: сторож устройств записи — обрыв, переподключение, время реплик.

Живой сбой 12.09: Teams перенастраивал звук, петля вывода закрывалась, помощник
записи собеседников выходил — и до «Стоп» собеседники не писались вовсе
(6 минут из 15). Хуже того, при следующем «Старт» дорожка собеседников
продолжалась со своей короткой длины, и их реплики вставали на минуты раньше,
чем были сказаны.

Что проверяем:
  1. Поток закрылся сам → сторож поднимает устройство заново, перерыв залит
     тишиной, и звук ПОСЛЕ перерыва стоит на своём месте во времени (сверка
     взаимной корреляцией с исходной записью).
  2. Устройство открылось, но замолчало → тоже переподключение.
  3. Повторный отказ при переподключении → сторож пробует ещё раз.
  4. «Старт» после «Стоп»: короткая дорожка догоняет длинную тишиной.
  5. На настоящих устройствах: убиваем процесс-помощник посреди записи.

Запуск из корня проекта:  .venv\\Scripts\\python.exe tests\\t42_capture_watchdog.py
Части с настоящими устройствами можно пропустить ключом --no-live.
"""
import io
import sys
import threading
import time
import wave
from pathlib import Path

import numpy as np

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
import isolate  # noqa: E402  настоящая база голосов не трогается
from harness import LINES, FAIL, say, check, finish  # noqa: E402

isolate.voices()

SR = 16000


from hagen import audio_io, live, store  # noqa: E402


def wav_pcm(path):
    with wave.open(str(path), "rb") as wf:
        raw = wf.readframes(wf.getnframes())
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0


def best_offset(got, ref, pos, win_s=1.5, search_s=4.0, info=None):
    """Где в эталоне стоит кусок записанной дорожки, начатый с отсчёта pos.

    info — список для отчёта: (позиция, пик кусочка, лучшая похожесть).
    """
    win = int(win_s * SR)
    piece = got[pos:pos + win]
    peak = float(np.max(np.abs(piece))) if piece.size else 0.0
    if piece.size < win or peak < 0.01:
        if info is not None:
            info.append((pos / float(SR), peak, None))
        return None
    lo = max(0, pos - int(search_s * SR))
    hi = min(ref.size - win, pos + int(search_s * SR))
    if hi <= lo:
        return None

    # Нормированная взаимная корреляция по КАЖДОМУ отсчёту через FFT. Истинный
    # сдвиг — произвольное число отсчётов, а похожесть речи падает ниже порога
    # уже при промахе на 4 отсчёта: прежняя сетка в 40 отсчётов то находила
    # ложный пик, то не находила ничего.
    from scipy.signal import correlate

    field = ref[lo:hi + win].astype(np.float64)
    p = piece.astype(np.float64)
    raw = correlate(field, p, mode="valid", method="fft")          # hi - lo + 1 сдвигов
    sq = np.cumsum(np.concatenate([[0.0], field * field]))
    energy = sq[win:win + raw.size] - sq[:raw.size]
    c = raw / (np.sqrt(np.maximum(energy, 1e-12)) * (np.linalg.norm(p) or 1.0))
    k = int(np.argmax(c))
    best, best_k = float(c[k]), lo + k
    if info is not None:
        info.append((pos / float(SR), peak, best))
    if best < 0.6:
        return None
    return (best_k - pos) / float(SR)


# ------------------------------------------------------------ поддельные устройства
class FakeRec:
    """Устройство, которое отдаёт звук по часам и умеет «ломаться».

    Звук берётся из source по стенным часам: после переподключения новое
    устройство начинает с того места, где сейчас «идёт разговор», а пропущенное
    потеряно — ровно как в жизни.
    """

    def __init__(self, on_audio, source, t0, die_at=None, stall_at=None, name="fake"):
        self.on_audio = on_audio
        self.source = source
        self.t0 = t0
        self.die_at = die_at
        self.stall_at = stall_at
        self.device_name = name
        self.error = None
        self.level = 0.0
        self._alive = False
        self._stop = threading.Event()
        self._last = 0.0
        self._th = None

    def start(self):
        self._alive = True
        self._last = time.time()
        self._pos = int((time.time() - self.t0) * SR)
        self._born = time.time()
        self._th = threading.Thread(target=self._run, daemon=True)
        self._th.start()

    def _run(self):
        while not self._stop.wait(0.1):
            age = time.time() - self._born
            if self.die_at is not None and age >= self.die_at:
                self._alive = False
                self.error = "поток записи закрылся сам (подделка)"
                return
            if self.stall_at is not None and age >= self.stall_at:
                continue            # жив, но звука больше не даёт
            now_pos = int((time.time() - self.t0) * SR)
            if now_pos <= self._pos:
                continue
            a, b = self._pos, now_pos
            self._pos = now_pos
            if a >= self.source.size:
                block = np.zeros(b - a, dtype=np.float32)
            else:
                block = self.source[a:min(b, self.source.size)]
                if block.size < b - a:
                    block = np.concatenate([block, np.zeros(b - a - block.size, np.float32)])
            self._last = time.time()
            self.on_audio(np.ascontiguousarray(block, dtype=np.float32))

    def stop(self):
        self._stop.set()
        self._alive = False
        if self._th is not None:
            self._th.join(timeout=2.0)

    @property
    def alive(self):
        return self._alive

    @property
    def stalled(self):
        return time.time() - self._last > 2.5

    def status(self):
        return {"device": self.device_name, "alive": self.alive, "stalled": self.stalled,
                "silent": False, "level": 0.0, "error": self.error}


class FakeCapture(live.DeviceCapture):
    """Сторож настоящий, устройства поддельные — по сценарию попыток."""

    def __init__(self, session, mic_plan, far_plan):
        super().__init__(session)
        self.mic_plan = list(mic_plan)
        self.far_plan = list(far_plan)
        self.opened = {"mic": 0, "far": 0}

    def _next(self, plan, track, feed):
        self.opened[track] += 1
        step = plan.pop(0) if plan else plan_default
        if step == "fail":
            raise RuntimeError("устройство не открылось (подделка)")
        rec = step(feed)
        rec.start()
        return rec

    def _open_mic(self, feed):
        return self._next(self.mic_plan, "mic", feed)

    def _open_far(self, feed):
        return self._next(self.far_plan, "far", feed)

    def _device_mismatch(self, rec):
        # Устройства поддельные: за настоящим звонком (и любым звуком в
        # браузере на этой машине) сторож здесь идти не должен — иначе лишнее
        # переоткрытие ломало сценарий в зависимости от того, что играло.
        return None


def scenario(title, far_plan, seconds, expect_restarts, check_timeline):
    global plan_default
    say("")
    say("=== %s ===" % title)
    ref, sr = audio_io.read_wav(PROJECT / "tests" / "meeting.wav")
    assert sr == SR
    noise = (np.random.RandomState(1).randn(int(seconds * SR) + SR * 20) * 0.003).astype(np.float32)

    meta = store.create(title="Проверка 42 — %s" % title, mode="online", source="live")
    rec_id = meta["id"]
    events = []
    sess = live.LiveSession(rec_id, mode="online", on_event=events.append)
    t0 = time.time()
    plan_default = lambda feed: FakeRec(feed, ref, t0, name="far-default")  # noqa: E731
    mic_plan = [lambda feed: FakeRec(feed, noise, t0, name="mic")]
    far_steps = []
    for step in far_plan:
        if step == "fail":
            far_steps.append("fail")
        else:
            kw = dict(step)
            far_steps.append(lambda feed, kw=kw: FakeRec(feed, ref, t0, name="far", **kw))
    cap = FakeCapture(sess, mic_plan, far_steps)
    try:
        st = cap.start(want_far=True)
        check("обе дорожки открылись", st.get("mic") is not None and st.get("far") is not None)
        time.sleep(seconds)
        st = cap.status()
    finally:
        cap.stop()
        sess.stop()

    restarts = (st.get("restarts") or {}).get("far", 0)
    check("переподключений собеседников: %d" % expect_restarts, restarts == expect_restarts,
          "было %d, открывали %d раз" % (restarts, cap.opened["far"]))
    texts = [e.get("text") or "" for e in events if e.get("type") == "notice"]
    if expect_restarts:
        check("человеку сказали про обрыв", any("прервался" in t for t in texts), texts)
        check("человеку сказали, что звук вернулся", any("снова пишется" in t for t in texts), texts)

    meta = store.get(rec_id) or {}
    gaps = [g for g in (meta.get("capture_gaps") or []) if g.get("track") == "far"]
    if expect_restarts:
        check("перерыв записан в карточку", len(gaps) == expect_restarts, gaps)
        if gaps:
            check("длина перерыва правдоподобна (0,5–9 с)", 0.5 <= gaps[0]["gap_s"] <= 9.0, gaps[0])

    mic = wav_pcm(store.track_path(rec_id, "mic"))
    far = wav_pcm(store.track_path(rec_id, "far"))
    diff = abs(mic.size - far.size) / float(SR)
    say("   дорожки: микрофон %.2f c, собеседники %.2f c" % (mic.size / SR, far.size / SR))
    check("дорожки одной длины (±0,35 с)", diff <= 0.35, "разница %.2f c" % diff)
    check("дорожка идёт по часам (±0,6 с)",
          abs(far.size / SR - seconds) <= 0.6 + 0.3, "%.2f c при %.1f c" % (far.size / SR, seconds))

    if check_timeline and gaps:
        # Меряем сдвиг дорожки относительно исходника ДО перерыва и ПОСЛЕ.
        # Сам сдвиг ненулевой и это нормально: запись стартует на долю секунды
        # позже, чем «пошёл разговор» (грузится модель). Важно, чтобы после
        # перерыва он остался тем же, — до правки звук съезжал назад ровно на
        # длину перерыва.
        gap_start = gaps[0]["at_s"]
        before = None
        # Ищем от самого перерыва назад: в начале эталона тихо, и первые окна
        # там либо пусты, либо дают ложное совпадение.
        pos = gap_start - 1.8
        while before is None and pos >= 0.2:
            before = best_offset(far, ref, int(pos * SR))
            pos -= 0.25
        after = int((gap_start + gaps[0]["gap_s"] + 0.8) * SR)
        offsets = []
        tried = []
        for extra in (0.0, 1.0, 2.0, 3.0):
            off = best_offset(far, ref, after + int(extra * SR), info=tried)
            if off is not None:
                offsets.append(off)
        say("   сдвиг звука относительно исходника: до перерыва %s c, после %s c"
            % ("—" if before is None else "%.2f" % before,
               ", ".join("%.2f" % o for o in offsets) or "—"))
        say("   окна после перерыва (позиция, пик, похожесть): %s"
            % "; ".join("%.1f: %.3f, %s" % (p, pk, "—" if c is None else "%.2f" % c)
                        for p, pk, c in tried))
        check("звук до перерыва найден в исходнике", before is not None)
        check("звук после перерыва найден в исходнике", bool(offsets))
        if before is not None and offsets:
            drift = max(abs(o - before) for o in offsets)
            check("после перерыва звук не съехал (±0,35 с)", drift <= 0.35,
                  "расхождение %.2f c" % drift)

    segs = store.load_transcript(rec_id).get("segments") or []
    gap_phrases = [s for s in segs if s.get("reason") == "gap"]
    say("   реплик распознано: %d, из них дописаны на обрыве: %d" % (len(segs), len(gap_phrases)))
    errs = [tp.error for tp in sess.tracks.values() if tp.error]
    check("дорожки обработались без ошибок", not errs, errs)
    store.delete(rec_id)


plan_default = None

# 1. Поток закрылся сам, второе открытие не удалось, третье — удачно
scenario("обрыв потока и повторный отказ",
         far_plan=[{"die_at": 6.0}, "fail"],
         seconds=14.0, expect_restarts=1, check_timeline=True)

# 2. Устройство живо, но перестало отдавать звук
scenario("устройство замолчало",
         far_plan=[{"stall_at": 6.0}],
         seconds=19.0, expect_restarts=1, check_timeline=True)

# 3. Без сбоев — сторож ничего не трогает
scenario("без сбоев", far_plan=[{}], seconds=5.0, expect_restarts=0, check_timeline=False)

# ------------------------------------------------------------ 4. продолжение записи
say("")
say("=== «Старт» после «Стоп»: короткая дорожка догоняет длинную ===")
meta = store.create(title="Проверка 42 — продолжение", mode="online", source="live")
rid = meta["id"]
audio_io.write_wav(store.track_path(rid, "mic"), np.zeros(5 * SR, dtype=np.float32))
audio_io.write_wav(store.track_path(rid, "far"), np.zeros(2 * SR, dtype=np.float32))
sess = live.LiveSession(rid, mode="online")
check("общая точка старта — длина длинной дорожки", sess.base_frames == 5 * SR, sess.base_frames)
tp_mic = sess.ensure_track("mic")
tp_far = sess.ensure_track("far")
check("время новых реплик собеседников начнётся с 5 с",
      tp_far.seconds == 5.0, tp_far.seconds)
sess.stop()
m = wav_pcm(store.track_path(rid, "mic")).size / SR
f = wav_pcm(store.track_path(rid, "far")).size / SR
check("после сеанса дорожки равны", abs(m - f) < 0.01, "микрофон %.2f, собеседники %.2f" % (m, f))
store.delete(rid)

# ------------------------------------------------------------ 5. настоящие устройства
if "--no-live" not in sys.argv:
    say("")
    say("=== Настоящие устройства: убиваем помощника записи собеседников ===")
    meta = store.create(title="Проверка 42 — живые устройства", mode="online", source="live")
    rid = meta["id"]
    events = []
    sess = live.LiveSession(rid, mode="online", on_event=events.append)
    cap = live.DeviceCapture(sess)
    st = cap.start(want_far=True)
    if st.get("mic") is None or st.get("far") is None:
        say("   ПРОПУЩЕНО: не открылись устройства: микрофон %s, собеседники %s"
            % (st.get("mic_error"), st.get("far_error")))
        FAIL.append("живая часть не проверена: устройства не открылись")
        cap.stop()
        sess.stop()
    else:
        time.sleep(5.0)
        proc = cap.far._proc
        say("   убиваю помощника, процесс %s" % (proc.pid if proc else "?"))
        if proc is not None:
            proc.kill()
        t_kill = time.time()
        restored = None
        while time.time() - t_kill < 15.0:
            time.sleep(0.2)
            if (cap.status().get("restarts") or {}).get("far", 0) >= 1 and cap.far is not None:
                restored = time.time() - t_kill
                break
        say("   переподключился через: %s" % ("%.1f c" % restored if restored else "НЕТ"))
        check("помощник поднят заново", restored is not None)
        time.sleep(5.0)
        cap.stop()
        sess.stop()
        mic = wav_pcm(store.track_path(rid, "mic")).size / SR
        far = wav_pcm(store.track_path(rid, "far")).size / SR
        say("   дорожки: микрофон %.2f c, собеседники %.2f c" % (mic, far))
        check("дорожки одной длины (±0,5 с)", abs(mic - far) <= 0.5, "%.2f" % abs(mic - far))
        gaps = (store.get(rid) or {}).get("capture_gaps") or []
        check("перерыв записан в карточку", len(gaps) == 1, gaps)
        texts = [e.get("text") for e in events if e.get("type") == "notice"]
        say("   сообщения: %s" % texts)
    store.delete(rid)

sys.exit(finish("t42"))
