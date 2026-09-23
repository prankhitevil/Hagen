# -*- coding: utf-8 -*-
"""Разметка говорящих в отдельной программе с низким приоритетом (решение 22.09).

Разметка длинной встречи занимает процессор на десятки минут. Внутри самой
программы она делила его с живой записью звонка и с Teams: у звонка запаздывал
текст, не сразу поднималась запись собеседников. В отдельной программе ей
ставится приоритет «ниже среднего» — Windows сначала отдаёт процессор всем
остальным, — а пока идёт запись, ещё и «режим эффективности»: экономичные ядра
на пониженной частоте. Звонок кончился — приоритет возвращается.

Помощник только считает: читает звук с диска и зовёт ту же
`diarize.diarize_pcm`, что и программа. Результат сохраняет, подписывает
голоса и ищет эхо сама программа — в базу голосов не пишут две программы
сразу.

Разговор — строками JSON. Программа пишет помощнику задачу одной строкой и
закрывает ему вход; помощник отвечает строками
{"type": "progress" | "eta" | "log" | "result" | "cancelled" | "error", ...}.
Свой журнал он пишет в stderr, программа переносит его в свой.

Отмена — файлом-флажком, путь к нему в задаче: помощник смотрит на него при
каждом отчёте о ходе. Ждать «cancel» на входе отдельным потоком нельзя: пока
поток сидит в чтении трубы, на Windows вставал весь помощник. Программы не
стало — помощник узнаёт по её номеру процесса и бросает счёт сам.

Запуск: `python -m hagen.diarize_worker --parent-pid N` (так его запускает `run`).
"""
from __future__ import annotations

import json
import logging
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger("hagen.diarize")

#: Как часто программа смотрит, идёт ли запись, и присылает ли помощник что-нибудь.
POLL_S = 0.5

#: Как уровни приоритета звучат в журнале.
LEVEL_NOTES = {"low": "приоритет ниже среднего",
               "lowest": "идёт запись — режим эффективности"}


class NotStarted(RuntimeError):
    """Помощник не запустился — разметку можно сделать в самой программе."""


# ---------------------------------------------------------------- сторона программы


def run(path: str | Path, min_speakers: int | None = None, max_speakers: int | None = None,
        handle: Any = None, busy: Callable[[], Any] | None = None) -> dict[str, Any]:
    """Разметить дорожку помощником. Ответ — как у `diarize.diarize_pcm`.

    `busy` отвечает, идёт ли сейчас запись (по умолчанию — очередь задач):
    пока идёт, помощник считает в режиме эффективности. Отмена задачи
    передаётся помощнику, и он бросает счёт на ближайшем отчёте о ходе.
    """
    from . import config, jobs, platform

    busy = busy or jobs.yield_reason
    cmd = [sys.executable, "-u", "-m", "hagen.diarize_worker", "--parent-pid", str(os.getpid())]
    try:
        proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            cwd=str(config.PROJECT_DIR), creationflags=platform.system().hidden_process_flags())
    except OSError as err:
        raise NotStarted(str(err)) from err

    tail: list[str] = []
    msgs: queue.Queue[dict[str, Any] | None] = queue.Queue()

    def read_out() -> None:
        for raw in iter(proc.stdout.readline, b""):
            try:
                msgs.put(json.loads(raw.decode("utf-8", "replace")))
            except ValueError:
                log.debug("помощник разметки: непонятная строка %r", raw[:200])
        msgs.put(None)

    def read_err() -> None:
        for raw in iter(proc.stderr.readline, b""):
            line = raw.decode("utf-8", "replace").rstrip()
            if not line:
                continue
            tail.append(line)
            del tail[:-8]
            log.info("помощник разметки: %s", line)

    threading.Thread(target=read_out, name="diar-helper-out", daemon=True).start()
    threading.Thread(target=read_err, name="diar-helper-err", daemon=True).start()
    log.info("разметка — в помощнике (процесс %d)", proc.pid)

    level = ""
    flag = Path(tempfile.gettempdir()) / ("hagen-diarize-cancel-%d-%d" % (os.getpid(), proc.pid))
    try:
        level = _set_level(proc.pid, "lowest" if busy() else "low", level)
        task = {"path": str(path), "min_speakers": min_speakers, "max_speakers": max_speakers,
                "cancel_flag": str(flag)}
        proc.stdin.write((json.dumps(task) + "\n").encode("ascii"))
        proc.stdin.close()
        while True:
            try:
                msg = msgs.get(timeout=POLL_S)
            except queue.Empty:
                msg = {}
            if handle is not None and not flag.exists() and getattr(handle, "cancelled", False):
                flag.touch()
            level = _set_level(proc.pid, "lowest" if busy() else "low", level)
            if msg is None:
                break
            kind = msg.get("type")
            if kind == "result":
                return msg.get("result") or {}
            if kind == "cancelled":
                from .diarize import DiarizeCancelled

                raise DiarizeCancelled("Диаризация отменена")
            if kind == "error":
                raise RuntimeError("разметка в помощнике не удалась: %s" % msg.get("text"))
            if handle is None:
                continue
            if kind == "progress":
                handle.progress(msg.get("value") or 0.0, msg.get("note") or "")
            elif kind == "eta":
                handle.eta(msg.get("seconds"))
            elif kind == "log":
                handle.log(str(msg.get("text") or ""))
        try:
            code = proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            code = None
        raise RuntimeError("помощник разметки закрылся без ответа (код %s): %s"
                           % (code, " / ".join(tail[-2:]) or "без сообщений"))
    finally:
        _stop(proc, flag)
        try:
            flag.unlink(missing_ok=True)
        except OSError:
            pass


def _set_level(pid: int, want: str, now: str) -> str:
    """Поменять приоритет помощника, если он не тот. Не вышло — считаем как есть."""
    if want == now:
        return now
    from . import platform

    try:
        ok = platform.system().set_process_priority(pid, want)
    except Exception as err:            # noqa: BLE001
        log.warning("помощник разметки: приоритет не поменялся (%s)", err)
        return want
    if ok:
        log.info("помощник разметки: %s", LEVEL_NOTES.get(want, want))
    else:
        log.warning("помощник разметки: Windows не дала поменять приоритет на «%s»", want)
    return want


def _stop(proc: subprocess.Popen, flag: Path) -> None:
    """Закрыть помощника: сначала попросить флажком, потом силой."""
    if proc.poll() is None:
        try:
            flag.touch()
        except OSError:
            pass
    for step in ("wait", "terminate", "kill"):
        if proc.poll() is not None:
            break
        try:
            if step == "wait":
                proc.wait(timeout=3)
            else:
                getattr(proc, step)()
                proc.wait(timeout=3)
        except Exception:               # noqa: BLE001
            pass


# ---------------------------------------------------------------- сторона помощника


class _LineHandle:
    """Ход разметки — строками в stdout; отмена — флажком или уходом программы."""

    PARENT_CHECK_S = 2.0

    def __init__(self, flag: str, parent_pid: int) -> None:
        self._flag = flag
        self._parent_pid = int(parent_pid or 0)
        self._checked = 0.0
        self._gone = False
        self.paused_s = 0.0             # помощник на паузу не встаёт

    @property
    def cancelled(self) -> bool:
        if self._flag and os.path.exists(self._flag):
            return True
        now = time.monotonic()
        if self._parent_pid and now - self._checked >= self.PARENT_CHECK_S:
            self._checked = now
            from . import platform

            self._gone = not platform.system().process_alive(self._parent_pid)
        return self._gone

    def progress(self, value: float, note: str = "") -> None:
        _send({"type": "progress", "value": float(value), "note": str(note or "")})

    def eta(self, seconds: float | None) -> None:
        _send({"type": "eta", "seconds": None if seconds is None else float(seconds)})

    def log(self, msg: str) -> None:
        _send({"type": "log", "text": str(msg)})


_send_lock = threading.Lock()


def _plain(value: Any) -> Any:
    """Числа numpy — в обычные: иначе json их не запишет."""
    if hasattr(value, "tolist"):
        return value.tolist()
    return float(value)


def _send(obj: dict[str, Any]) -> None:
    line = json.dumps(obj, ensure_ascii=True, default=_plain)
    with _send_lock:
        sys.stdout.write(line + "\n")
        sys.stdout.flush()


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(description="Разметка говорящих отдельной программой")
    ap.add_argument("--parent-pid", type=int, default=0,
                    help="программа, ради которой запущен помощник: её нет — бросаем счёт")
    args, _unknown = ap.parse_known_args(argv)

    # Журнал — в stderr по-русски: программа читает его как UTF-8.
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:                   # noqa: BLE001
        pass
    logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                        format="%(levelname)s %(message)s")

    first = sys.stdin.readline()
    if not first.strip():
        return 2
    task = json.loads(first)

    from . import audio_io, diarize

    handle = _LineHandle(str(task.get("cancel_flag") or ""), args.parent_pid)
    t0 = time.time()
    try:
        pcm, sr = audio_io.read_wav(task["path"])
        result = diarize.diarize_pcm(pcm, sr=sr, min_speakers=task.get("min_speakers"),
                                     max_speakers=task.get("max_speakers"), handle=handle)
    except diarize.DiarizeCancelled:
        _send({"type": "cancelled"})
        return 0
    except Exception as err:            # noqa: BLE001
        logging.getLogger("hagen.diarize").exception("разметка в помощнике упала")
        _send({"type": "error", "text": str(err)[:500] or type(err).__name__})
        return 1
    logging.getLogger("hagen.diarize").info("помощник закончил за %.0f c", time.time() - t0)
    _send({"type": "result", "result": result})
    return 0


if __name__ == "__main__":
    sys.exit(main())
