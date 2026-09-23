# -*- coding: utf-8 -*-
"""Выключатель системного микрофона — тот же, что кнопка на клавиатуре.

Зачем в программе. На ноутбуке микрофон гасится клавишей, но по ней не видно,
в каком он состоянии, и рукой до неё не всегда удобно (решение 17.09: крупная
кнопка в окне, а лучше — плавающий кружок).

Что именно выключается. Микрофон Windows целиком, а не «запись Hagen»:
ровно то, что делает клавиша. Поэтому, пока микрофон выключен, вас не слышат
ни собеседники, ни программа — в записи будет тишина. Это и есть смысл
кнопки: сказать что-то в стороне, не попав ни в звонок, ни в стенограмму.

Управляем устройством СВЯЗИ (то же, что берёт Teams), а не мультимедийным:
в жизни это одно устройство, но когда они разные, гасить надо то, в которое
говорят.
"""
from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger("hagen.micmute")

EDATAFLOW_CAPTURE = 1
ROLE_COMMUNICATIONS = 2


def _endpoint() -> Any:
    """Устройство записи «для связи» с его регулятором громкости."""
    import comtypes
    import pycaw.pycaw as pc
    from pycaw.api.endpointvolume import IAudioEndpointVolume
    from pycaw.api.mmdeviceapi import IMMDeviceEnumerator
    from pycaw.constants import CLSID_MMDeviceEnumerator

    try:
        comtypes.CoInitialize()
    except Exception:
        pass
    enumerator = comtypes.CoCreateInstance(
        CLSID_MMDeviceEnumerator, IMMDeviceEnumerator, comtypes.CLSCTX_INPROC_SERVER)
    dev = enumerator.GetDefaultAudioEndpoint(EDATAFLOW_CAPTURE, ROLE_COMMUNICATIONS)
    iface = dev.Activate(IAudioEndpointVolume._iid_, comtypes.CLSCTX_ALL, None)
    name = ""
    try:
        name = str(pc.AudioUtilities.CreateDevice(dev).FriendlyName or "")
    except Exception:
        pass
    return iface.QueryInterface(IAudioEndpointVolume), name


def _in_audio_thread(fn, *args):
    """Всё общение с COM — в звуковом потоке программы, как и вся работа с WASAPI."""
    from . import loopback

    return loopback.audio_thread.call(fn, *args)


def _read() -> dict[str, Any]:
    vol, name = _endpoint()
    return {"available": True, "muted": bool(vol.GetMute()), "device": name}


def _write(muted: bool) -> dict[str, Any]:
    vol, name = _endpoint()
    vol.SetMute(1 if muted else 0, None)
    return {"available": True, "muted": bool(vol.GetMute()), "device": name}


def state() -> dict[str, Any]:
    """Выключен ли сейчас микрофон. Пустой ответ — управлять нечем."""
    try:
        return _in_audio_thread(_read)
    except Exception as err:                      # noqa: BLE001
        log.debug("состояние микрофона не прочиталось: %s", err)
        return {"available": False, "muted": False, "device": "", "why": str(err)[:200]}


def set_muted(muted: bool) -> dict[str, Any]:
    """Выключить или включить микрофон Windows."""
    try:
        out = _in_audio_thread(_write, bool(muted))
    except Exception as err:                      # noqa: BLE001
        log.warning("микрофон не переключился: %s", err)
        return {"available": False, "muted": False, "device": "", "why": str(err)[:200]}
    log.info("микрофон Windows %s (%s)",
             "выключен" if out["muted"] else "включён", out.get("device") or "устройство связи")
    return out


def toggle() -> dict[str, Any]:
    """Переключить микрофон. Если состояние неизвестно — считаем, что включён."""
    now = state()
    if not now.get("available"):
        return now
    return set_muted(not now.get("muted"))
