# -*- coding: utf-8 -*-
"""Розетка «Звук» для Windows: микрофон, петля вывода, устройства, выключатель.

Что описывает розетка — в `hagen/platform/base.py`, класс `Audio`.

Код лежит рядом пятью файлами, как и лежал:

* `loopback.py` — свой путь записи через WASAPI, устройства, пробы, разбор;
* `capture.py` — запасной путь через ffmpeg и процесс-помощник петли;
* `loopback_worker.py` — сам помощник, отдельная программа;
* `micmute.py` — выключатель микрофона Windows;
* `miccapsule.py` — кружок микрофона поверх окон.

Здесь только дверь наружу. Три имени в этой двери появились потому, что до
переезда логика лезла в приватную часть звука: `same_device` — это бывший
`loopback._similar`, которым сторож устройств сверял имена. Две другие
приватные зацепки (`_audio_thread` и `_default_render_devices`) остались
внутри: ими пользуются соседи по этой же папке, а не логика.
"""
from __future__ import annotations

from typing import Any, Callable

from . import capture, loopback, micmute

__all__ = [
    "list_input_devices", "list_loopback_devices", "list_ffmpeg_devices",
    "default_render_devices", "default_mic_device", "find_device",
    "recommended_input_index", "same_device", "echo_risk",
    "open_mic", "open_loopback", "open_loopback_process", "open_ffmpeg",
    "probe_input", "probe_loopback_process", "diagnose", "diag_ffmpeg",
    "mic_state", "mic_set_muted", "mic_toggle", "mic_pill",
]


# ------------------------------------------------------------ какие устройства


def list_input_devices() -> list[dict[str, Any]]:
    """Микрофоны: номер, имя, каналы, «по умолчанию», «устройство связи»."""
    return loopback.list_input_devices()


def list_loopback_devices() -> list[dict[str, Any]]:
    """Устройства вывода, с которых можно снять петлю."""
    return loopback.list_devices()


def list_ffmpeg_devices(refresh: bool = False) -> list[dict[str, Any]]:
    """Запасной список — глазами ffmpeg."""
    return capture.list_devices(refresh)


def default_render_devices() -> dict[str, str]:
    """Устройства вывода по умолчанию: мультимедиа и связь."""
    return loopback.default_render_devices()


def default_mic_device() -> dict[str, Any] | None:
    """Микрофон по умолчанию для запасного пути."""
    return capture.default_mic_device()


def find_device(needle: str | None) -> dict[str, Any] | None:
    """Устройство по куску имени."""
    return capture.find_device(needle)


def recommended_input_index() -> int | None:
    """Какой микрофон писать, если номер в настройках не закреплён."""
    return loopback.recommended_input_index()


def same_device(a: str, b: str) -> bool:
    """Одно ли это устройство, хотя имена написаны по-разному."""
    return loopback._similar(a, b)


def echo_risk(name: str, form: int | None = None) -> dict[str, Any]:
    """Услышит ли микрофон собеседников из этого устройства."""
    return loopback.echo_risk(name, form)


# ------------------------------------------------------------------- запись


def open_mic(device_index: int | None,
             on_audio: Callable[[Any], Any]) -> loopback.MicRecorder:
    """Микрофон своими силами (WASAPI в этом процессе)."""
    return loopback.MicRecorder(device_index=device_index, on_audio=on_audio)


def open_loopback(device_index: int | None, on_audio: Callable[[Any], Any],
                  keep_alive: bool = True, open_retries: int = 1,
                  want_name: str | None = None) -> loopback.LoopbackRecorder:
    """Петля вывода в этом процессе. Так работает процесс-помощник."""
    return loopback.LoopbackRecorder(device_index=device_index, on_audio=on_audio,
                                     keep_alive=keep_alive, open_retries=open_retries,
                                     want_name=want_name)


def open_loopback_process(device_index: int | None, on_audio: Callable[[Any], Any],
                          want_name: str | None = None) -> capture.LoopbackProcess:
    """Петля вывода через отдельный процесс-помощник."""
    return capture.LoopbackProcess(device_index=device_index, on_audio=on_audio,
                                   want_name=want_name)


def open_ffmpeg(device: dict[str, Any], on_audio: Callable[[Any], Any],
                label: str) -> capture.FfmpegCapture:
    """Запасной путь: ffmpeg пишет в канал, мы читаем куски по 100 мс."""
    return capture.FfmpegCapture(device, on_audio=on_audio, label=label)


# --------------------------------------------------------- пробы и разбор


def probe_input(device_index: int | None = None, seconds: float = 0.8) -> dict[str, Any]:
    """Короткая проба микрофона."""
    return loopback.probe_input(device_index, seconds)


def probe_loopback_process(device_index: int | None = None,
                           seconds: float = 1.2) -> dict[str, Any]:
    """Проба петли тем же путём, которым идёт запись."""
    return capture.probe_loopback_process(device_index, seconds)


def diagnose() -> dict[str, Any]:
    """Что служба видит в звуковом стеке."""
    return loopback.diagnose()


def diag_ffmpeg(seconds: float = 0.6) -> dict[str, Any]:
    """Полный протокол запуска ffmpeg — для разбора отказов."""
    return capture.diag_ffmpeg(seconds)


# ------------------------------------------------------ выключатель и кружок


def mic_state() -> dict[str, Any]:
    """Выключен ли микрофон Windows."""
    return micmute.state()


def mic_set_muted(muted: bool) -> dict[str, Any]:
    """Выключить или включить микрофон Windows целиком."""
    return micmute.set_muted(muted)


def mic_toggle() -> dict[str, Any]:
    """Переключить микрофон."""
    return micmute.toggle()


def mic_pill(on_click: Callable[[], Any],
             on_change: Callable[[dict[str, Any]], Any]) -> Any:
    """Кружок микрофона поверх всех окон.

    Подключается прямо здесь, а не наверху файла: кружок поднимает своё окно и
    свой цикл сообщений, и пока он не понадобился, его лучше не трогать.
    """
    from . import miccapsule

    return miccapsule.MicPill(on_click=on_click, on_change=on_change)
