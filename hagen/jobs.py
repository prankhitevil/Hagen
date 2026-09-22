# -*- coding: utf-8 -*-
"""Очередь фоновых задач: диаризация, обработка файлов, протоколы.

Тяжёлые задачи не должны подвешивать интерфейс, поэтому они выполняются в
отдельных потоках. Одной общей очередью они шли до 20.09 — и ждали друг друга
без всякой на то причины: пока распознавался часовой ролик, саммари соседней
записи стояло в очереди, хотя считает его не наш процессор, а модель на той
стороне; второе видео не начинало скачиваться, хотя скачивание — это сеть.

Поэтому очередей теперь несколько — ДОРОЖЕК, по роду занятия (решение 20.09):

  * ``heavy`` — счёт на нашем процессоре: разметка голосов, «перечитать
    точнее», пересчёт эха. Тут по-прежнему строго по одной: модель занимает
    все ядра, и две такие задачи вместе идут не быстрее, а медленнее;
  * ``media`` — добыть файл: скачать, вытащить звук. Это сеть и диск, поэтому
    несколько сразу — нормально. Распознавание внутри такой задачи всё равно
    берёт общий пропуск на тяжёлый счёт (:meth:`JobHandle.heavy`), так что
    одновременно считается ровно одна запись;
  * ``docs`` — документы: протокол, саммари. Считает языковая модель, наш
    процессор ждёт ответа;
  * ``net`` — качаем части программы, обновляем yt-dlp.

Внутри дорожки порядок прежний — кто первым встал, тот первым и пойдёт.
Интерфейс видит очередь, прогресс и оценку времени.
"""
from __future__ import annotations

import logging
import threading
import time
import traceback
import uuid
from collections import deque
from contextlib import contextmanager
from typing import Any, Callable

log = logging.getLogger("hagen.jobs")

#: Дорожки и сколько задач каждой идёт одновременно (решение 20.09).
#: Пределы скромные намеренно: смысл не в том, чтобы запустить всё разом, а в
#: том, чтобы разнородные дела не ждали друг друга. Два скачивания — это два
#: канала и два файла на диск; больше только мешает.
LANES: dict[str, int] = {
    "heavy": 1,     # наш процессор: разметка, «перечитать точнее», эхо
    "media": 2,     # скачать файл и вытащить звук — сеть и диск
    "docs": 2,      # документы ОБЛАЧНОЙ моделью — считает чужой сервер
    "docs_one": 1,  # документы всем остальным — по одному (решение 20.09)
    "net": 2,       # части программы, обновление yt-dlp и самой программы
}

#: Какому роду занятий принадлежит задача. Незнакомый вид попадёт в «heavy» —
#: осторожность важнее скорости: лучше лишний раз подождать, чем запустить
#: вторую тяжёлую задачу и уронить обеим скорость.
KIND_LANES: dict[str, str] = {
    "diarize": "heavy",
    "split": "heavy",
    "retranscribe": "heavy",
    "echo": "heavy",
    "media": "media",
    # Документы: дорожку выбирает тот, кто ставит задачу, — она зависит не от
    # вида документа, а от того, ЧЕМ его считают (`minutes.document_lane`).
    # Здесь — осторожное значение на случай, если дорожку не указали.
    "minutes": "docs_one",
    "summary": "docs_one",
    "needs": "net",
    "ytdlp": "net",
    "update": "net",
}

DEFAULT_LANE = "heavy"

_LISTENERS: list[Callable[[dict], None]] = []
_lock = threading.RLock()
_jobs: dict[str, dict[str, Any]] = {}
_order: deque[str] = deque()
#: Своя очередь и свои рабочие потоки у каждой дорожки.
_queues: dict[str, deque[str]] = {lane: deque() for lane in LANES}
_wakes: dict[str, threading.Event] = {lane: threading.Event() for lane in LANES}
_workers: dict[str, list[threading.Thread]] = {lane: [] for lane in LANES}
_fns: dict[str, Callable[["JobHandle"], Any]] = {}

#: Пропуск на тяжёлый счёт. Его держит задача дорожки «heavy» всё своё время, а
#: задача «media» — только пока распознаёт. Так скачивание идёт параллельно
#: чему угодно, а считает процессор всё равно одну запись за раз.
_heavy_pass = threading.BoundedSemaphore(1)

MAX_KEEP = 60


def lane_of(kind: str) -> str:
    return KIND_LANES.get(str(kind), DEFAULT_LANE)


def on_change(fn: Callable[[dict], None]) -> None:
    """Подписка сервера: вызывается при каждом изменении задачи."""
    with _lock:
        if fn not in _LISTENERS:
            _LISTENERS.append(fn)


def _notify(job: dict[str, Any]) -> None:
    snapshot = dict(job)
    for fn in list(_LISTENERS):
        try:
            fn(snapshot)
        except Exception:
            log.debug("слушатель задач упал", exc_info=True)


class JobHandle:
    """То, что видит сама задача: отчёт о прогрессе и проверка отмены."""

    def __init__(self, job_id: str):
        self.job_id = job_id
        self._last_push = 0.0

    def _patch(self, patch: dict[str, Any], force: bool = False) -> None:
        now = time.time()
        with _lock:
            job = _jobs.get(self.job_id)
            if job is None:
                return
            job.update(patch)
            job["updated_at"] = now
            # не заливаем интерфейс сообщениями чаще 4 раз в секунду
            if force or (now - self._last_push) > 0.25:
                self._last_push = now
                snapshot = dict(job)
            else:
                snapshot = None
        if snapshot is not None:
            _notify(snapshot)

    def progress(self, value: float, note: str = "") -> None:
        try:
            v = float(value)
        except (TypeError, ValueError):
            return
        v = max(0.0, min(1.0, v))
        patch: dict[str, Any] = {"progress": round(v, 4)}
        if note:
            patch["note"] = note
        self._patch(patch)

    def eta(self, seconds: float | None) -> None:
        self._patch({"eta_s": None if seconds is None else max(0.0, round(float(seconds), 1))})

    def log(self, msg: str) -> None:
        log.info("[%s] %s", self.job_id[:8], msg)
        with _lock:
            job = _jobs.get(self.job_id)
            if job is not None:
                lines = job.setdefault("lines", [])
                lines.append(str(msg))
                del lines[:-12]
        self._patch({"note": str(msg)}, force=True)

    @property
    def cancelled(self) -> bool:
        with _lock:
            job = _jobs.get(self.job_id)
            return bool(job and job.get("cancel_requested"))

    @contextmanager
    def heavy(self, note: str = "жду очереди на распознавание"):
        """Взять пропуск на тяжёлый счёт на время этого куска работы (20.09).

        Нужен там, где задача лёгкой дорожки доходит до настоящего счёта на
        процессоре: скачали ролик — и распознаём. Пока пропуск занят соседней
        записью, здесь честно пишем, что стоим в очереди, — иначе человек видел
        бы «идёт обработка» и недоумевал, почему ничего не происходит.
        """
        if _heavy_pass.acquire(blocking=False):
            taken = True
        else:
            if note:
                self.log(note)
            _heavy_pass.acquire()
            taken = True
        try:
            yield
        finally:
            if taken:
                try:
                    _heavy_pass.release()
                except ValueError:      # уже отпущен — отпускать второй раз нельзя
                    log.debug("пропуск на тяжёлый счёт уже был отпущен")


def submit(
    kind: str,
    fn: Callable[[JobHandle], Any],
    title: str,
    rec_id: str | None = None,
    eta_s: float | None = None,
    extra: dict[str, Any] | None = None,
    lane: str | None = None,
) -> str:
    """Поставить задачу в очередь своей дорожки. Возвращает её идентификатор.

    ``extra`` — дополнительные поля карточки задачи. Например ``retry``:
    куда и с чем обратиться, чтобы собрать тот же документ другим движком.
    ``lane`` задаётся только в редких случаях: обычно род занятия понятен по
    виду задачи (``KIND_LANES``).
    """
    job_id = uuid.uuid4().hex[:12]
    now = time.time()
    lane = lane if lane in LANES else lane_of(kind)
    job = {
        "id": job_id,
        "kind": kind,
        "lane": lane,
        "title": title,
        "rec_id": rec_id,
        "status": "queued",
        "progress": 0.0,
        "eta_s": eta_s,
        "note": "в очереди",
        "created_at": now,
        "updated_at": now,
        "started_at": None,
        "finished_at": None,
        "error": None,
        "result": None,
        "cancel_requested": False,
        "lines": [],
        "error_kind": None,
    }
    if extra:
        job.update({k: v for k, v in extra.items() if k not in job or k == "error_kind"})
    with _lock:
        _jobs[job_id] = job
        _fns[job_id] = fn
        _order.append(job_id)
        _queues[lane].append(job_id)
        _trim_locked()
        _ensure_workers_locked(lane)
    _notify(job)
    _wakes[lane].set()
    return job_id


def _trim_locked() -> None:
    while len(_order) > MAX_KEEP:
        old = _order.popleft()
        j = _jobs.get(old)
        if j and j.get("status") in ("running", "queued"):
            _order.append(old)   # активные не выбрасываем
            break
        _jobs.pop(old, None)
        _fns.pop(old, None)


def _ensure_workers_locked(lane: str) -> None:
    """Поднять рабочие потоки дорожки — ровно столько, сколько ей положено.

    Потоки заводятся при первой задаче дорожки, а не при запуске программы:
    пока человек ничего не обрабатывает, лишним потокам взяться неоткуда.
    """
    alive = [t for t in _workers[lane] if t.is_alive()]
    while len(alive) < LANES[lane]:
        t = threading.Thread(target=_run_loop, args=(lane,),
                             name="hagen-jobs-%s-%d" % (lane, len(alive) + 1),
                             daemon=True)
        t.start()
        alive.append(t)
    _workers[lane] = alive


def _run_loop(lane: str) -> None:
    queue, wake = _queues[lane], _wakes[lane]
    while True:
        with _lock:
            job_id = queue.popleft() if queue else None
        if job_id is None:
            wake.wait(timeout=1.0)
            wake.clear()
            continue
        _run_one(job_id)


def _run_one(job_id: str) -> None:
    with _lock:
        job = _jobs.get(job_id)
        fn = _fns.get(job_id)
    if job is None or fn is None:
        return
    if job.get("cancel_requested"):
        _finish(job_id, status="cancelled", note="отменено до запуска")
        return

    handle = JobHandle(job_id)
    lane = str(job.get("lane") or lane_of(job.get("kind")))
    t0 = time.time()
    with _lock:
        job["status"] = "running"
        job["started_at"] = t0
        job["note"] = "выполняется"
        snapshot = dict(job)
    _notify(snapshot)

    try:
        if lane == "heavy":
            # Тяжёлая задача держит пропуск всё своё время — иначе соседняя
            # запись начала бы распознаваться прямо поверх неё.
            with handle.heavy(note="жду, пока освободится процессор"):
                result = fn(handle)
        else:
            result = fn(handle)
        if handle.cancelled:
            _finish(job_id, status="cancelled", note="отменено", elapsed=time.time() - t0)
        else:
            _finish(job_id, status="done", note="готово", result=result,
                    elapsed=time.time() - t0)
    except Exception as err:
        # Отменённая задача падает изнутри — исключением её и останавливают.
        # Показывать это красной ошибкой нечестно: человек сам нажал «стоп».
        if handle.cancelled:
            log.info("задача %s (%s) остановлена по просьбе", job_id[:8], job.get("kind"))
            _finish(job_id, status="cancelled", note="остановлено",
                    elapsed=time.time() - t0)
        else:
            log.error("задача %s (%s) упала: %s", job_id[:8], job.get("kind"), err)
            log.debug("%s", traceback.format_exc())
            # Ошибка может нести свои поля — например, «упёрлись в лимит
            # подписки» с временем сброса: окно покажет по ним нужные кнопки.
            extra = getattr(err, "job_extra", None)
            if isinstance(extra, dict):
                with _lock:
                    if job_id in _jobs:
                        _jobs[job_id].update(extra)
            _finish(job_id, status="error", note=_human_error(err), error=str(err),
                    elapsed=time.time() - t0)
    finally:
        with _lock:
            _fns.pop(job_id, None)


def _human_error(err: Exception) -> str:
    text = str(err).strip()
    if not text:
        text = type(err).__name__
    return text if len(text) <= 300 else text[:297] + "..."



def _finish(
    job_id: str,
    status: str,
    note: str,
    result: Any = None,
    error: str | None = None,
    elapsed: float | None = None,
) -> None:
    with _lock:
        job = _jobs.get(job_id)
        if job is None:
            return
        job["status"] = status
        job["note"] = note
        job["finished_at"] = time.time()
        job["updated_at"] = job["finished_at"]
        job["error"] = error
        job["eta_s"] = None
        if status == "done":
            job["progress"] = 1.0
        if elapsed is not None:
            job["elapsed_s"] = round(elapsed, 2)
        job["result"] = _safe_result(result)
        snapshot = dict(job)
    _notify(snapshot)


def _safe_result(result: Any) -> Any:
    """В интерфейс отдаём только то, что сериализуется и не содержит массивов."""
    if result is None or isinstance(result, (str, int, float, bool)):
        return result
    if isinstance(result, dict):
        out = {}
        for k, v in result.items():
            if k in ("embeddings", "pcm", "waveform", "words"):
                continue
            if isinstance(v, (str, int, float, bool)) or v is None:
                out[k] = v
            elif isinstance(v, (list, tuple)):
                out[k] = len(v)
            elif isinstance(v, dict):
                out[k] = len(v)
        return out
    if isinstance(result, (list, tuple)):
        return {"count": len(result)}
    return str(type(result).__name__)


def _public(job: dict[str, Any]) -> dict[str, Any]:
    """То, что видит страница. Признак запрошенной отмены отдаём наружу:
    страница рисует по нему «останавливаю…», и подпись переживает следующий
    опрос службы. Раньше признак вырезался, и надпись слетала обратно на
    крестик через несколько секунд, будто нажатия не было."""
    out = dict(job)
    out["lines"] = list(job.get("lines") or [])[-4:]
    return out


def get(job_id: str) -> dict[str, Any] | None:
    with _lock:
        job = _jobs.get(job_id)
        return _public(job) if job else None


def list_all(limit: int = 40) -> list[dict[str, Any]]:
    with _lock:
        ids = list(_order)[-limit:]
        return [_public(_jobs[i]) for i in reversed(ids) if i in _jobs]


def active() -> list[dict[str, Any]]:
    with _lock:
        return [_public(j) for j in _jobs.values() if j.get("status") in ("queued", "running")]


def for_recording(rec_id: str) -> list[dict[str, Any]]:
    with _lock:
        return [_public(j) for j in _jobs.values() if j.get("rec_id") == rec_id]


def cancel(job_id: str) -> bool:
    with _lock:
        job = _jobs.get(job_id)
        if job is None or job.get("status") not in ("queued", "running"):
            return False
        job["cancel_requested"] = True
        job["note"] = "запрошена отмена"
        snapshot = dict(job)
    _notify(snapshot)
    return True


def busy_with(kind: str, rec_id: str | None = None) -> bool:
    """Есть ли уже такая задача в работе — чтобы не ставить дубль."""
    with _lock:
        for j in _jobs.values():
            if j.get("kind") != kind or j.get("status") not in ("queued", "running"):
                continue
            if rec_id is None or j.get("rec_id") == rec_id:
                return True
    return False
