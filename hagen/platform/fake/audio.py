# -*- coding: utf-8 -*-
"""Заглушка розетки «Звук»: устройства выдуманы, звук — ровная тишина.

Что можно проверять на ней: выбирает ли логика верное устройство, переживает
ли отказ открытия, переключается ли за звонком, что делает при пропаже
устройства посреди записи. Чего нельзя: слышно ли что-нибудь на самом деле.
"""
from __future__ import annotations

import threading
import time
from typing import Any, Callable

import numpy as np

__all__ = [
    "list_input_devices", "list_loopback_devices", "list_ffmpeg_devices",
    "default_render_devices", "default_mic_device", "find_device",
    "recommended_input_index", "same_device", "echo_risk",
    "open_mic", "open_loopback", "open_loopback_process", "open_ffmpeg",
    "probe_input", "probe_loopback_process", "diagnose", "diag_ffmpeg",
    "mic_state", "mic_set_muted", "mic_toggle", "mic_pill",
]

SR = 16000

#: Микрофоны, которые «видит» программа.
MICS: list[dict[str, Any]] = [
    {"index": 0, "name": "Микрофон (проверочный)", "channels": 1,
     "is_default": True, "is_communications": True},
    {"index": 1, "name": "Микрофон (колонки)", "channels": 2,
     "is_default": False, "is_communications": False},
]

#: Устройства вывода, с которых можно снять петлю.
LOOPBACKS: list[dict[str, Any]] = [
    {"index": 10, "name": "Динамики (проверочные)", "is_default": True,
     "is_communications": False},
    {"index": 11, "name": "Наушники (проверочные)", "is_default": False,
     "is_communications": True},
]

#: Уровень, который отдают пробы. 0 — «устройство-пустышка».
PROBE_PEAK = 0.2

#: Имена устройств, которые «не открываются»: логика должна уйти на запасной путь.
BROKEN: set[str] = set()

#: Выключен ли микрофон.
MUTED = False

#: Всё, что открывали за проверку, — чтобы посмотреть, что и в каком порядке.
OPENED: list[dict[str, Any]] = []


def reset() -> None:
    global PROBE_PEAK, MUTED
    PROBE_PEAK = 0.2
    MUTED = False
    BROKEN.clear()
    OPENED.clear()


# ------------------------------------------------------------ какие устройства


def list_input_devices() -> list[dict[str, Any]]:
    return [dict(d) for d in MICS]


def list_loopback_devices() -> list[dict[str, Any]]:
    return [dict(d) for d in LOOPBACKS]


def list_ffmpeg_devices(refresh: bool = False) -> list[dict[str, Any]]:
    return [{"name": d["name"], "alt_name": d["name"], "kind": "mic"} for d in MICS]


def default_render_devices() -> dict[str, str]:
    return {"multimedia": LOOPBACKS[0]["name"], "communications": LOOPBACKS[1]["name"]}


def default_mic_device() -> dict[str, Any] | None:
    devs = list_ffmpeg_devices()
    return devs[0] if devs else None


def find_device(needle: str | None) -> dict[str, Any] | None:
    if not needle:
        return None
    low = str(needle).strip().lower()
    for d in list_ffmpeg_devices():
        if low in d["name"].lower():
            return d
    return None


def recommended_input_index() -> int | None:
    for d in MICS:
        if d.get("is_default"):
            return int(d["index"])
    return int(MICS[0]["index"]) if MICS else None


def same_device(a: str, b: str) -> bool:
    """Сравнение имён без хвоста «[loopback]», регистра и лишних пробелов."""
    def norm(x: str) -> str:
        return " ".join(str(x or "").replace("[loopback]", "").split()).strip().lower()

    na, nb = norm(a), norm(b)
    return bool(na) and bool(nb) and (na == nb or na in nb or nb in na)


def echo_risk(name: str, form: int | None = None) -> dict[str, Any]:
    """Опасны те, у кого в имени «динамики» или «колонки»."""
    low = str(name or "").lower()
    risky = any(w in low for w in ("динамик", "колонк", "speaker"))
    return {"risk": risky, "why": ("звук идёт в %s" % name) if risky else ""}


# ------------------------------------------------------------------- запись


class _Recorder:
    """Отдаёт ровную тишину кусками по 100 мс, пока его не остановят."""

    BLOCK = 1600

    def __init__(self, kind: str, device_index: int | None, on_audio: Callable[[Any], Any],
                 device_name: str = "", want_name: str | None = None) -> None:
        self.kind = kind
        self.device_index = device_index
        self.on_audio = on_audio
        self.device_name = device_name or "устройство %s" % device_index
        self.want_name = want_name
        self.choice = "заглушка"
        self.running = False
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self.device_name in BROKEN:
            raise RuntimeError("устройство «%s» не открылось" % self.device_name)
        OPENED.append({"kind": self.kind, "device": self.device_name,
                       "want": self.want_name, "at": time.time()})
        self.running = True
        self._thread = threading.Thread(target=self._run, name="fake-audio", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        block = np.zeros(self.BLOCK, dtype=np.float32)
        while self.running:
            try:
                self.on_audio(block.copy())
            except Exception:
                break
            time.sleep(0.1)

    def stop(self) -> None:
        self.running = False


def _pick(device_index: int | None, devices: list[dict[str, Any]],
          want_name: str | None) -> tuple[int | None, str]:
    """Тот же порядок выбора, что и на настоящей розетке: номер, имя, умолчание."""
    if device_index is not None:
        for d in devices:
            if int(d["index"]) == int(device_index):
                return int(d["index"]), str(d["name"])
        return int(device_index), "устройство %s" % device_index
    if want_name:
        for d in devices:
            if same_device(d["name"], want_name):
                return int(d["index"]), str(d["name"])
    for d in devices:
        if d.get("is_default"):
            return int(d["index"]), str(d["name"])
    return (int(devices[0]["index"]), str(devices[0]["name"])) if devices else (None, "")


def open_mic(device_index: int | None, on_audio: Callable[[Any], Any]) -> _Recorder:
    idx, name = _pick(device_index, MICS, None)
    return _Recorder("mic", idx, on_audio, name)


def open_loopback(device_index: int | None, on_audio: Callable[[Any], Any],
                  keep_alive: bool = True, open_retries: int = 1,
                  want_name: str | None = None) -> _Recorder:
    idx, name = _pick(device_index, LOOPBACKS, want_name)
    return _Recorder("loopback", idx, on_audio, name, want_name)


def open_loopback_process(device_index: int | None, on_audio: Callable[[Any], Any],
                          want_name: str | None = None) -> _Recorder:
    idx, name = _pick(device_index, LOOPBACKS, want_name)
    return _Recorder("loopback-process", idx, on_audio, name, want_name)


def open_ffmpeg(device: dict[str, Any], on_audio: Callable[[Any], Any],
                label: str) -> _Recorder:
    return _Recorder("ffmpeg", None, on_audio, str(device.get("name") or label))


# --------------------------------------------------------- пробы и разбор


def probe_input(device_index: int | None = None, seconds: float = 0.8) -> dict[str, Any]:
    return {"ok": True, "peak": float(PROBE_PEAK), "seconds": seconds}


def probe_loopback_process(device_index: int | None = None,
                           seconds: float = 1.2) -> dict[str, Any]:
    return {"ok": True, "peak": float(PROBE_PEAK), "seconds": seconds}


def diagnose() -> dict[str, Any]:
    return {"fake": True, "mics": list_input_devices(), "loopback": list_loopback_devices()}


def diag_ffmpeg(seconds: float = 0.6) -> dict[str, Any]:
    return {"fake": True, "ok": True, "seconds": seconds}


# ------------------------------------------------------ выключатель и кружок


def mic_state() -> dict[str, Any]:
    return {"available": True, "muted": bool(MUTED), "device": MICS[0]["name"]}


def mic_set_muted(muted: bool) -> dict[str, Any]:
    global MUTED
    MUTED = bool(muted)
    return mic_state()


def mic_toggle() -> dict[str, Any]:
    return mic_set_muted(not MUTED)


class _Pill:
    """Кружок, который никуда не показывается, но помнит, что ему велели."""

    def __init__(self, on_click: Callable[[], Any],
                 on_change: Callable[[dict[str, Any]], Any]) -> None:
        self.on_click = on_click
        self.on_change = on_change
        self.shown = False
        self.muted = False
        self.recording = False
        self.hwnd = 1          # логика проверяет, что окно «есть»
        self.error = None

    def show(self, muted: bool = False, recording: bool = False) -> None:
        self.shown = True
        self.muted = bool(muted)
        self.recording = bool(recording)

    def hide(self) -> None:
        self.shown = False

    def set_muted(self, muted: bool) -> None:
        self.muted = bool(muted)

    def visible(self) -> bool:
        return self.shown

    def click(self) -> None:
        """Щелчок по кружку — рукой проверки."""
        out = self.on_click()
        self.set_muted(bool((out or {}).get("muted")))
        self.on_change(out)


def mic_pill(on_click: Callable[[], Any],
             on_change: Callable[[dict[str, Any]], Any]) -> _Pill:
    return _Pill(on_click, on_change)
