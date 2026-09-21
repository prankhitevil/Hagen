# -*- coding: utf-8 -*-
"""Заглушка розетки «Окна»: звонок и встреча задаются проверкой.

Настоящая розетка спрашивает Windows про аудиосессии и Outlook про календарь.
Здесь и то и другое — переменные модуля: проверка ставит, что хочет увидеть,
и смотрит, как поступит логика.
"""
from __future__ import annotations

import threading
from typing import Any, Callable

__all__ = [
    "list_sessions", "list_capture_sessions", "detect_call", "call_output_device",
    "watch_calls", "outlook_kind", "compose_mail", "current_meeting", "suggest_title",
    "attendee_names", "suggest_max_speakers",
]

#: Письма, «открытые» через compose_mail: проверка смотрит, что в них попало.
MAILS: list[dict[str, Any]] = []

#: Удаётся ли собрать письмо. False — как будто классического Outlook нет.
MAIL_OK = True

#: Встреча, которую отдаёт `current_meeting`. None — встречи нет.
MEETING: dict[str, Any] | None = None

#: Что отдаёт `detect_call`.
CALL: dict[str, Any] = {"active": False}

#: Куда «звонок» выводит звук. None — звонка не видно.
OUTPUT: dict[str, Any] | None = None

#: Аудиосессии, которые видны розетке.
SESSIONS: list[dict[str, Any]] = []
CAPTURE_SESSIONS: list[dict[str, Any]] = []

#: Каким Outlook притворяемся.
OUTLOOK: dict[str, Any] = {"classic": True, "new": False, "com_available": True}

#: Заведённые сторожа — чтобы проверка могла подтолкнуть их руками.
WATCHERS: list["Watcher"] = []


def reset() -> None:
    global MEETING, CALL, OUTPUT, OUTLOOK, MAIL_OK
    MEETING = None
    CALL = {"active": False}
    OUTPUT = None
    OUTLOOK = {"classic": True, "new": False, "com_available": True}
    MAIL_OK = True
    SESSIONS.clear()
    CAPTURE_SESSIONS.clear()
    WATCHERS.clear()
    MAILS.clear()


def list_sessions() -> list[dict[str, Any]]:
    return list(SESSIONS)


def list_capture_sessions() -> list[dict[str, Any]]:
    return list(CAPTURE_SESSIONS)


def detect_call() -> dict[str, Any]:
    return dict(CALL)


def call_output_device(sessions: list[dict[str, Any]] | None = None,
                       holders: set[str] | None = None) -> dict[str, Any] | None:
    return dict(OUTPUT) if OUTPUT else None


def outlook_kind() -> dict[str, Any]:
    return dict(OUTLOOK)


def compose_mail(subject: str, body: str,
                 attachments: list[str] | None = None,
                 to: list[str] | None = None) -> bool:
    """Письмо не открывается, а записывается: проверка смотрит, что в нём."""
    if not MAIL_OK:
        return False
    MAILS.append({"subject": str(subject or ""), "body": str(body or ""),
                  "attachments": [str(a) for a in (attachments or [])],
                  "to": [str(t) for t in (to or [])]})
    return True


def current_meeting(window_minutes: int | None = None,
                    start_outlook: bool = False) -> dict[str, Any] | None:
    return dict(MEETING) if MEETING else None


def suggest_title(meeting: dict[str, Any] | None) -> str:
    """Тема встречи, а без встречи — пусто: имя придумает логика."""
    if not meeting:
        return ""
    return " ".join(str(meeting.get("subject") or "").split()).strip()


def attendee_names(meeting: dict[str, Any] | None) -> list[str]:
    if not meeting:
        return []
    return [str(n).strip() for n in (meeting.get("attendees") or []) if str(n).strip()]


def suggest_max_speakers(meeting: dict[str, Any] | None) -> int | None:
    n = len(attendee_names(meeting))
    return max(2, min(n, 10)) if n else None


class Watcher:
    """Сторож звонков, которого двигает проверка, а не Windows."""

    def __init__(self, on_call_start: Callable[[dict[str, Any]], Any],
                 on_call_end: Callable[[dict[str, Any]], Any]) -> None:
        self.on_call_start = on_call_start
        self.on_call_end = on_call_end
        self.running = False
        self._lock = threading.Lock()

    def start(self) -> bool:
        with self._lock:
            self.running = True
        return True

    def stop(self) -> None:
        with self._lock:
            self.running = False

    # -------- руки проверки
    def fire_start(self, info: dict[str, Any] | None = None) -> None:
        """Сказать логике, что звонок начался."""
        self.on_call_start(dict(info or {"process": "ms-teams.exe", "pid": 1234}))

    def fire_end(self, info: dict[str, Any] | None = None) -> None:
        """Сказать логике, что звонок закончился."""
        self.on_call_end(dict(info or {"process": "ms-teams.exe", "pid": 1234}))


def watch_calls(on_call_start: Callable[[dict[str, Any]], Any],
                on_call_end: Callable[[dict[str, Any]], Any]) -> Watcher:
    w = Watcher(on_call_start, on_call_end)
    WATCHERS.append(w)
    return w
