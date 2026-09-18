# -*- coding: utf-8 -*-
"""Запись звука собеседников через петлю вывода Windows (WASAPI loopback).

Берём цифровой поток, который Windows отдаёт на устройство вывода, — то есть
именно то, что прислал собеседник, а не то, что микрофон услышал из динамика.

Важная тонкость: у Windows два «устройства по умолчанию» — мультимедийное и
устройство СВЯЗИ. Teams часто играет звонок именно на второе. Мы определяем
устройство связи через COM (роль eCommunications) и предлагаем его первым.
"""
from __future__ import annotations

import logging
import queue
import re
import threading
import time
from typing import Any, Callable

import numpy as np

from ... import audio_io

log = logging.getLogger("hagen.loopback")

SR = audio_io.SR
EDATAFLOW_RENDER = 0
EDATAFLOW_CAPTURE = 1
ROLE_CONSOLE = 0
ROLE_MULTIMEDIA = 1
ROLE_COMMUNICATIONS = 2

_LOOPBACK_SUFFIX = re.compile(r"\s*\[loopback\]\s*$", re.I)


def _norm(name: str) -> str:
    s = _LOOPBACK_SUFFIX.sub("", str(name or "")).strip().lower()
    return re.sub(r"\s+", " ", s)


def _similar(a: str, b: str) -> bool:
    """Имена из PortAudio обрезаются, поэтому сравниваем по вхождению."""
    na, nb = _norm(a), _norm(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    short, long = (na, nb) if len(na) <= len(nb) else (nb, na)
    return len(short) >= 12 and short in long





class _AudioThread:
    """Выделенный поток, который владеет всей работой с PortAudio и COM.

    Зачем так: PortAudio через WASAPI привязывает состояние COM к тому потоку,
    где он был инициализирован. Если перечислить устройства в одном потоке, а
    открыть запись в другом (а в службе так и выходит: запросы обрабатываются
    в рабочих потоках), открытие падает с невнятным «Invalid device» (-9996).
    Поэтому ВСЯ работа со звуком выполняется строго в одном потоке, а вызывающий
    просто дожидается результата.
    """

    def __init__(self) -> None:
        self._queue: "queue.Queue[tuple]" = queue.Queue()
        self._thread = threading.Thread(target=self._run, name="audio-owner", daemon=True)
        self._started = False
        self._lock = threading.Lock()

    def _ensure(self) -> None:
        with self._lock:
            if not self._started:
                self._started = True
                self._thread.start()

    def _run(self) -> None:
        try:
            import comtypes

            try:
                comtypes.CoInitialize()
            except Exception:
                pass
        except Exception:
            pass
        while True:
            fn, args, kwargs, box, done = self._queue.get()
            try:
                box.append(("ok", fn(*args, **kwargs)))
            except BaseException as err:      # noqa: BLE001 — пробрасываем как есть
                box.append(("err", err))
            finally:
                done.set()

    def call(self, fn, *args, **kwargs):
        """Выполнить функцию в звуковом потоке и вернуть её результат."""
        if threading.current_thread() is self._thread:
            return fn(*args, **kwargs)
        self._ensure()
        box: list = []
        done = threading.Event()
        self._queue.put((fn, args, kwargs, box, done))
        if not done.wait(timeout=60):
            raise RuntimeError("звуковой поток не ответил за 60 секунд")
        kind, value = box[0]
        if kind == "err":
            raise value
        return value


_audio_thread = _AudioThread()


_com_threads: set[int] = set()
_com_lock = threading.Lock()


def ensure_com() -> None:
    """Инициализировать COM в ТЕКУЩЕМ потоке.

    КРИТИЧНО и неочевидно: WASAPI работает через COM, и COM инициализируется
    отдельно для каждого потока. Если открыть звуковой поток из рабочего потока,
    где COM не поднят, PortAudio отвечает «Invalid device» (-9996) — причём
    ровно так же, как при неверном формате, поэтому ошибка сбивает с толку.
    Именно это и давало «перемежающиеся» отказы: из главного потока скрипта
    запись работала, а из рабочего потока службы — нет.
    """
    tid = threading.get_ident()
    with _com_lock:
        if tid in _com_threads:
            return
    try:
        import comtypes

        try:
            comtypes.CoInitialize()
        except Exception:
            # уже инициализирован в другом режиме — это не ошибка
            pass
        with _com_lock:
            _com_threads.add(tid)
    except Exception as err:
        log.debug("COM не инициализирован в потоке %s: %s", tid, err)


_pa_lock = threading.Lock()
_pa = None


def _pyaudio():
    """Один экземпляр PyAudio на весь процесс.

    КРИТИЧНО: повторная инициализация PortAudio ломает WASAPI — после
    PyAudio().terminate() следующее открытие потока отдаёт «Invalid device»
    (проверено: перечисление устройств перед записью гарантированно ломало
    запись). Поэтому экземпляр создаётся один раз и не закрывается.
    """
    ensure_com()
    global _pa
    with _pa_lock:
        if _pa is None:
            import pyaudiowpatch as pyaudio

            _pa = pyaudio.PyAudio()
        return _pa


# ---------------------------------------------------------------- роли Windows


def _default_endpoints(dataflow: int) -> dict[str, str]:
    """Имена устройств по умолчанию для направления: мультимедиа и связь."""
    out: dict[str, str] = {}
    try:
        import comtypes
        import pycaw.pycaw as pc
        from pycaw.api.mmdeviceapi import IMMDeviceEnumerator
        from pycaw.constants import CLSID_MMDeviceEnumerator
    except Exception as err:
        log.debug("pycaw недоступен: %s", err)
        return out

    # CoUninitialize здесь НЕ вызываем: WASAPI внутри PortAudio опирается на
    # живой COM в этом потоке, и его закрытие ломает открытие петли
    # («Invalid device» на ровном месте — проверено).
    try:
        try:
            comtypes.CoInitialize()
        except Exception:
            pass
        enumerator = comtypes.CoCreateInstance(
            CLSID_MMDeviceEnumerator, IMMDeviceEnumerator, comtypes.CLSCTX_INPROC_SERVER
        )
        for key, role in (("multimedia", ROLE_MULTIMEDIA), ("communications", ROLE_COMMUNICATIONS)):
            try:
                dev = enumerator.GetDefaultAudioEndpoint(int(dataflow), role)
                out[key] = pc.AudioUtilities.CreateDevice(dev).FriendlyName
            except Exception as err:
                log.debug("роль %s не прочиталась: %s", key, err)
    except Exception as err:
        log.debug("не удалось прочитать устройства по умолчанию: %s", err)
    return out


def _default_render_devices() -> dict[str, str]:
    """Имена устройств ВЫВОДА по умолчанию: мультимедиа и связь."""
    return _default_endpoints(EDATAFLOW_RENDER)


def _default_capture_devices() -> dict[str, str]:
    """Имена устройств ЗАПИСИ по умолчанию: мультимедиа и связь."""
    return _default_endpoints(EDATAFLOW_CAPTURE)


# Windows сама знает, что это за устройство: PKEY_AudioEndpoint_FormFactor.
# Судить по названию ненадёжно — «Динамики (Jabra SPEAK 510)» это спикерфон с
# собственным подавлением эха, а «Динамики (Realtek)» — колонки ноутбука.
_FORM_FACTOR_KEY = "{1DA5D803-D492-4EDD-8C23-E0C0FFEE7F0E} 0"
#: Формы, при которых звук уходит в воздух и возвращается в микрофон.
_OPEN_AIR = {1, 2, 8, 9}          # колонки, линейный выход, S/PDIF, звук по HDMI
#: Формы, при которых эха не будет.
_CLOSED = {3, 5, 6}               # наушники, гарнитура, трубка
#: Спикерфоны со своим подавлением эха — по ним судим по названию: форма у них
#: «колонки», а эха они не дают.
_SPEAKERPHONES = ("jabra", "poly ", "plantronics", "yealink", "konftel", "sennheiser",
                  "logitech", "anker", "emeet", "owl")


def _form_factors() -> dict[str, int]:
    """Форма каждого устройства вывода по имени: колонки, наушники, гарнитура…"""
    out: dict[str, int] = {}
    try:
        import comtypes
        import pycaw.pycaw as pc
        from pycaw.constants import DEVICE_STATE, EDataFlow
    except Exception as err:
        log.debug("формы устройств недоступны: %s", err)
        return out
    try:
        try:
            comtypes.CoInitialize()
        except Exception:
            pass
        for dev in pc.AudioUtilities.GetAllDevices(
                data_flow=EDataFlow.eRender.value,
                device_state=DEVICE_STATE.ACTIVE.value):
            # Windows отдаёт ключи заглавными, но полагаться на регистр не будем.
            value = next((v for k, v in (dev.properties or {}).items()
                          if k.upper() == _FORM_FACTOR_KEY), None)
            if value is None or not dev.FriendlyName:
                continue
            try:
                out[_norm(str(dev.FriendlyName))] = int(value)
            except (TypeError, ValueError):
                continue
    except Exception as err:
        log.debug("формы устройств не прочитались: %s", err)
    return out


def echo_risk(name: str, form: int | None = None) -> dict[str, Any]:
    """Услышит ли микрофон собеседников из этого устройства.

    Возвращает ``{"risk": bool, "why": str}``. risk=True — звук пойдёт в
    воздух, и в стенограмме появятся двойники (случай 17.09:
    встроенные динамики Realtek вместо Jabra).
    """
    low = _norm(name or "")
    if any(mark in low for mark in _SPEAKERPHONES):
        return {"risk": False, "why": "спикерфон со своим подавлением эха"}
    if form in _CLOSED:
        return {"risk": False, "why": "наушники или гарнитура"}
    if form in _OPEN_AIR:
        return {"risk": True, "why": "звук идёт в динамики"}
    if any(mark in low for mark in ("наушник", "headphone", "headset", "гарнитур")):
        return {"risk": False, "why": "наушники или гарнитура"}
    if any(mark in low for mark in ("динамик", "speaker", "колонк")):
        return {"risk": True, "why": "звук идёт в динамики"}
    return {"risk": False, "why": ""}


def _list_devices() -> list[dict[str, Any]]:
    """Список петлевых устройств с пометками «по умолчанию» и «устройство связи»."""
    try:
        import pyaudiowpatch as pyaudio
    except Exception as err:
        log.warning("pyaudiowpatch недоступен: %s", err)
        return []

    pa_ready = _pyaudio()          # PortAudio поднимаем первым
    forms = _form_factors()
    roles = _default_render_devices()
    comm_name = roles.get("communications") or ""
    multi_name = roles.get("multimedia") or ""

    devices: list[dict[str, Any]] = []
    pa = None
    try:
        pa = _pyaudio()
        default_loop_index = None
        try:
            default_loop_index = int(pa.get_default_wasapi_loopback()["index"])
        except Exception:
            pass
        for info in pa.get_loopback_device_info_generator():
            name = str(info["name"])
            clean = _LOOPBACK_SUFFIX.sub("", name).strip()
            risk = echo_risk(clean, forms.get(_norm(clean)))
            devices.append({
                "index": int(info["index"]),
                "name": clean,
                "raw_name": name,
                "echo_risk": bool(risk["risk"]),
                "echo_why": risk["why"],
                "sample_rate": int(info["defaultSampleRate"]),
                "channels": int(info["maxInputChannels"]),
                "is_default_output": _similar(name, multi_name) or int(info["index"]) == default_loop_index,
                "is_communications": _similar(name, comm_name),
            })
    except Exception as err:
        log.warning("не удалось перечислить петлевые устройства: %s", err)
    # экземпляр PyAudio намеренно не закрываем, см. _pyaudio()

    # рекомендуем устройство связи, иначе — стандартное
    rec = next((d for d in devices if d["is_communications"]), None)
    if rec is None:
        rec = next((d for d in devices if d["is_default_output"]), None)
    for d in devices:
        d["recommended"] = bool(rec is not None and d["index"] == rec["index"])
    return devices



# ---- публичные обёртки: всё исполняется в звуковом потоке -------------------


def default_render_devices() -> dict[str, str]:
    """Имена устройств вывода по умолчанию: мультимедиа и связь."""
    return _audio_thread.call(_default_render_devices)


def list_devices() -> list[dict[str, Any]]:
    """Петлевые устройства с пометками «по умолчанию» и «устройство связи»."""
    return _audio_thread.call(_list_devices)


def list_input_devices() -> list[dict[str, Any]]:
    """Микрофоны WASAPI с их родным числом каналов."""
    return _audio_thread.call(_list_input_devices)


def recommended_device_index() -> int | None:
    devs = _list_devices()
    if not devs:
        return None
    for d in devs:
        if d.get("recommended"):
            return int(d["index"])
    return int(devs[0]["index"])


def index_for_name(name: str | None, devices: list[dict[str, Any]]) -> int | None:
    """Номер петлевого устройства по имени устройства вывода (как его зовёт Windows)."""
    if not name:
        return None
    for d in devices:
        if _norm(str(d.get("name") or "")) == _norm(name):
            return int(d["index"])
    for d in devices:
        if _similar(str(d.get("name") or ""), name):
            return int(d["index"])
    return None


def _list_input_devices() -> list[dict[str, Any]]:
    """Микрофоны WASAPI с их РОДНЫМ числом каналов.

    Число каналов важно: WASAPI в общем режиме принимает только формат самого
    устройства. Запрос «моно» у четырёхканальной гарнитуры отвергается с
    невнятной ошибкой «Invalid device» — это и сбивало с толку.
    """
    devices: list[dict[str, Any]] = []
    try:
        pa = _pyaudio()
        # «По умолчанию» спрашиваем у самой Windows по имени. Номер от
        # get_default_input_device_info() приходит из другого набора устройств
        # (MME), среди номеров WASAPI его нет — и пометка не доставалась никому.
        roles = _default_capture_devices()
        comm_name = roles.get("communications") or ""
        multi_name = roles.get("multimedia") or ""
        wasapi_host = None
        for i in range(pa.get_host_api_count()):
            try:
                import pyaudiowpatch as pyaudio

                if pa.get_host_api_info_by_index(i)["type"] == pyaudio.paWASAPI:
                    wasapi_host = i
                    break
            except Exception:
                continue
        for i in range(pa.get_device_count()):
            d = pa.get_device_info_by_index(i)
            if int(d.get("maxInputChannels") or 0) <= 0 or d.get("isLoopbackDevice"):
                continue
            if wasapi_host is not None and d.get("hostApi") != wasapi_host:
                continue
            name = str(d.get("name") or "").strip()
            devices.append({
                "index": i,
                "name": name,
                "sample_rate": int(d.get("defaultSampleRate") or 48000),
                "channels": int(d.get("maxInputChannels") or 1),
                "is_default": _similar(name, multi_name),
                "is_communications": _similar(name, comm_name),
            })
    except Exception as err:
        log.warning("не удалось перечислить микрофоны: %s", err)
    return devices


def recommended_input_index() -> int | None:
    """Какой микрофон писать, если в настройках не закреплён номер устройства.

    Сначала имя из настроек, и только потом устройство по умолчанию. Номера
    устройств PortAudio плавают: стоит подключить или выключить гарнитуру — и
    номер уезжает на чужое устройство. Имя переживает переподключение, а
    «по умолчанию» в Windows запросто оказывается виртуальным устройством
    (Steam, SteelSeries) или микрофонным входом колонок, который отдаёт ровные
    нули — запись при этом идёт, а в файле тишина.
    """
    devs = _list_input_devices()
    if not devs:
        return None
    from ... import config

    want = (config.get("mic_device_name") or "").strip()
    if want:
        for d in devs:
            if _similar(str(d.get("name") or ""), want):
                return int(d["index"])
        log.warning("микрофон «%s» из настроек не найден, беру по умолчанию", want)
    # Устройство СВЯЗИ вперёд обычного: именно его Windows отдаёт звонкам, и
    # ровно так же выбирается устройство для дорожки собеседников.
    for key in ("is_communications", "is_default"):
        for d in devs:
            if d.get(key):
                return int(d["index"])
    return int(devs[0]["index"])




# ---------------------------------------------------------------- запись



def render_index_for(loopback_index: int) -> int | None:
    """Подобрать устройство ВЫВОДА, которому соответствует петлевое устройство."""
    try:
        pa = _pyaudio()
        info = pa.get_device_info_by_index(int(loopback_index))
        want = _norm(str(info.get("name") or ""))
        host = info.get("hostApi")
        best = None
        for i in range(pa.get_device_count()):
            d = pa.get_device_info_by_index(i)
            if d.get("hostApi") != host or int(d.get("maxOutputChannels") or 0) <= 0:
                continue
            if _similar(str(d.get("name") or ""), want):
                return i
            if best is None:
                best = i
        return best
    except Exception as err:
        log.debug("не нашёл устройство вывода для петли %s: %s", loopback_index, err)
        return None


class SilenceKeeper:
    """Подаёт на устройство вывода тишину, чтобы оно было активно.

    Зачем: WASAPI отдаёт петлю только пока устройство ЧТО-ТО играет. На простое
    устройство открыть петлю нельзя — драйвер отвечает «Invalid device»
    (проверено на Jabra SPEAK 510). Неслышная тишина держит устройство живым,
    и запись собеседников начинается с первой секунды, ещё до того как кто-то
    заговорил. Для вывода звука запрета нет — режется только захват.
    """

    def __init__(self, render_index: int):
        self.render_index = int(render_index)
        self._stream = None
        self.error: str | None = None

    def start(self) -> bool:
        import pyaudiowpatch as pyaudio

        try:
            pa = _pyaudio()
            info = pa.get_device_info_by_index(self.render_index)
            rate = int(info.get("defaultSampleRate") or 48000)
            ch = max(1, int(info.get("maxOutputChannels") or 2))
            frames = max(256, rate // 20)
            silence = bytes(frames * ch * 2)   # нули = тишина

            def cb(in_data, frame_count, time_info, status):
                return (silence[: frame_count * ch * 2], pyaudio.paContinue)

            self._stream = pa.open(
                format=pyaudio.paInt16, channels=ch, rate=rate, output=True,
                output_device_index=self.render_index, frames_per_buffer=frames,
                stream_callback=cb,
            )
            self._stream.start_stream()
            log.debug("тишина подаётся на устройство вывода [%d] %s",
                      self.render_index, info.get("name"))
            return True
        except Exception as err:
            self.error = str(err)
            log.debug("не удалось подать тишину на [%d]: %s", self.render_index, err)
            self._stream = None
            return False

    def stop(self) -> None:
        st = self._stream
        self._stream = None
        if st is None:
            return
        try:
            st.stop_stream()
        except Exception:
            pass
        try:
            st.close()
        except Exception:
            pass


class LoopbackRecorder:
    """Пишет петлю вывода и отдаёт куски 16 кГц моно float32 в callback.

    Callback вызывается из нашего потока, а не из потока звуковой подсистемы,
    поэтому в нём можно спокойно делать тяжёлую работу.
    """

    SILENCE_PEAK = 2.0e-4
    SILENCE_AFTER_S = 4.0
    STALL_AFTER_S = 2.5

    def __init__(
        self,
        device_index: int | None = None,
        on_audio: Callable[[np.ndarray], None] | None = None,
        block_ms: int = 100,
        keep_alive: bool = True,
        open_retries: int = 3,
        want_name: str | None = None,
    ):
        self.device_index = device_index
        # Имя устройства, куда программа звонка реально выводит звук. Сильнее
        # «устройства по умолчанию»: 13.09 Windows держала по умолчанию выход
        # монитора Dell, запись собеседников шла с него — и весь звонок была
        # ровная тишина, хотя Teams играл в колонки.
        self.want_name = want_name
        self.choice = ""
        self.on_audio = on_audio
        self.block_ms = int(block_ms)
        self.keep_alive = bool(keep_alive)
        self.open_retries = int(open_retries)

        self._keeper: "SilenceKeeper | None" = None
        self._pa = None
        self._stream = None
        self._queue: queue.Queue = queue.Queue(maxsize=400)
        self._worker: threading.Thread | None = None
        self._stop = threading.Event()
        self._resampler: audio_io.Resampler | None = None

        self.native_sr = 0
        self.channels = 0
        self.device_name = ""
        self.level = 0.0
        self.peak = 0.0
        self.error: str | None = None
        self._last_data_ts = 0.0
        self._last_loud_ts = 0.0
        self._started_ts = 0.0
        self._frames_out = 0

    # ---------------------------------------- запуск и остановка
    def start(self) -> None:
        """Открыть поток. Выполняется в звуковом потоке, см. _AudioThread."""
        _audio_thread.call(self._start_impl)

    def stop(self) -> None:
        """Закрыть поток. Тоже строго в звуковом потоке."""
        _audio_thread.call(self._stop_impl)

    def _start_impl(self) -> None:
        import pyaudiowpatch as pyaudio

        idx = self.device_index
        if idx is None and self.want_name and self.keep_alive:
            idx = index_for_name(self.want_name, _list_devices())
            self.choice = ("звонок выводит звук в «%s»" % self.want_name if idx is not None
                           else "«%s» среди устройств петли не нашлось" % self.want_name)
        if idx is None:
            idx = recommended_device_index()
            try:
                roles = _default_render_devices()
                self.choice = ((self.choice + "; ") if self.choice else "") + (
                    "по умолчанию в Windows: звук — «%s», связь — «%s»"
                    % (roles.get("multimedia") or "?", roles.get("communications") or "?"))
            except Exception:
                pass
        elif self.device_index is not None:
            self.choice = "номер устройства задан в настройках"
        if idx is None:
            raise RuntimeError("не найдено ни одного устройства вывода для петли")
        self.device_index = int(idx)

        self._pa = _pyaudio()
        info = self._pa.get_device_info_by_index(self.device_index)
        if not int(info.get("maxInputChannels") or 0):
            raise RuntimeError("устройство %s нельзя писать как петлю" % info.get("name"))
        self.native_sr = int(info["defaultSampleRate"])
        self.channels = max(1, int(info["maxInputChannels"]))
        self.device_name = _LOOPBACK_SUFFIX.sub("", str(info["name"])).strip()
        self._resampler = audio_io.Resampler(self.native_sr, SR)

        frames = max(256, int(self.native_sr * self.block_ms / 1000.0))

        def cb(in_data, frame_count, time_info, status):
            try:
                self._queue.put_nowait(in_data)
            except queue.Full:
                pass
            return (None, pyaudio.paContinue)

        # Устройство вывода отдаёт петлю ТОЛЬКО пока оно что-то играет. Поэтому
        # сначала подаём на него неслышную тишину — тогда запись собеседников
        # начинается с первой секунды, а не с первого чужого слова.
        if self._keeper is None and self.keep_alive:
            ri = render_index_for(self.device_index)
            if ri is not None:
                keeper = SilenceKeeper(ri)
                if keeper.start():
                    self._keeper = keeper
                    time.sleep(0.25)   # даём устройству проснуться

        def try_open() -> Exception | None:
            err_last: Exception | None = None
            for fmt in (pyaudio.paFloat32, pyaudio.paInt16):
                try:
                    self._stream = self._pa.open(
                        format=fmt,
                        channels=self.channels,
                        rate=self.native_sr,
                        input=True,
                        input_device_index=self.device_index,
                        frames_per_buffer=frames,
                        stream_callback=cb,
                    )
                    self._fmt = fmt
                    return None
                except Exception as err:
                    err_last = err
                    self._stream = None
            return err_last

        last_err = try_open()
        # несколько повторов: устройство может просыпаться не мгновенно
        attempts = 0
        while self._stream is None and attempts < self.open_retries:
            attempts += 1
            time.sleep(0.4)
            last_err = try_open()
        if self._stream is None:
            if self._keeper is not None:
                self._keeper.stop()
                self._keeper = None
            self._pa = None      # общий экземпляр не закрываем
            if self.keep_alive:
                raise RuntimeError(
                    "не удалось открыть петлю на устройстве %s: %s. "
                    "Петля доступна только когда на устройстве идёт звук — "
                    "начните запись после начала разговора либо проверьте, "
                    "что звонок выводится на выбранное устройство."
                    % (self.device_name, last_err))
            raise RuntimeError(
                "не удалось открыть микрофон «%s»: %s. "
                "Возможные причины: устройство занято другой программой, "
                "либо выключен доступ к микрофону в параметрах Windows."
                % (self.device_name, last_err))

        self._stop.clear()
        now = time.time()
        self._started_ts = now
        self._last_data_ts = now
        self._last_loud_ts = now
        self._worker = threading.Thread(target=self._run, name="loopback-worker", daemon=True)
        self._worker.start()
        self._stream.start_stream()
        log.info("петля открыта: [%d] %s, %d Гц, %d кан.",
                 self.device_index, self.device_name, self.native_sr, self.channels)

    def _stop_impl(self) -> None:
        self._stop.set()
        st = self._stream
        self._stream, self._pa = None, None
        if st is not None:
            try:
                st.stop_stream()
            except Exception:
                pass
            try:
                st.close()
            except Exception:
                pass
        # общий экземпляр PyAudio остаётся жить, см. _pyaudio()
        keeper = self._keeper
        self._keeper = None
        if keeper is not None:
            keeper.stop()
        w = self._worker
        if w is not None and w.is_alive():
            w.join(timeout=2.0)
        self._worker = None

    # ---------------------------------------- обработка
    def _run(self) -> None:
        import pyaudiowpatch as pyaudio

        dtype = np.float32 if getattr(self, "_fmt", None) == pyaudio.paFloat32 else np.int16
        while not self._stop.is_set():
            try:
                raw = self._queue.get(timeout=0.25)
            except queue.Empty:
                continue
            try:
                block = np.frombuffer(raw, dtype=dtype)
                mono = audio_io.downmix(block, self.channels)
                if mono.size == 0:
                    continue
                pcm = self._resampler(mono) if self._resampler else mono
                now = time.time()
                self._last_data_ts = now
                pk = audio_io.peak_level(mono)
                self.peak = pk
                self.level = audio_io.rms_level(mono)
                if pk > self.SILENCE_PEAK:
                    self._last_loud_ts = now
                if pcm.size:
                    self._frames_out += int(pcm.size)
                    if self.on_audio is not None:
                        self.on_audio(pcm)
            except Exception as err:
                self.error = str(err)
                log.warning("ошибка обработки петли: %s", err)

    # ---------------------------------------- состояние для интерфейса
    @property
    def seconds(self) -> float:
        return self._frames_out / float(SR)

    @property
    def silent(self) -> bool:
        """Давно нет звука — значит, выбрано не то устройство вывода."""
        if not self._started_ts:
            return False
        return (time.time() - self._last_loud_ts) > self.SILENCE_AFTER_S

    @property
    def stalled(self) -> bool:
        """Поток перестал отдавать данные: наушники выдернули или сменилось устройство."""
        if not self._started_ts:
            return False
        return (time.time() - self._last_data_ts) > self.STALL_AFTER_S

    @property
    def alive(self) -> bool:
        st = self._stream
        if st is None:
            return False
        try:
            return bool(st.is_active())
        except Exception:
            return False

    def status(self) -> dict[str, Any]:
        return {
            "device_index": self.device_index,
            "device_name": self.device_name,
            "native_sr": self.native_sr,
            "channels": self.channels,
            "level": round(self.level, 4),
            "peak": round(self.peak, 4),
            "seconds": round(self.seconds, 2),
            "silent": self.silent,
            "stalled": self.stalled,
            "alive": self.alive,
            "error": self.error,
        }


class MicRecorder(LoopbackRecorder):
    """Запись микрофона тем же механизмом, что и петля.

    Отличия: устройство берём из списка микрофонов, тишину на вывод не подаём.
    Число каналов всегда родное для устройства — иначе WASAPI отказывает.
    """

    def __init__(
        self,
        device_index: int | None = None,
        on_audio: Callable[[np.ndarray], None] | None = None,
        block_ms: int = 100,
    ):
        super().__init__(
            device_index=device_index, on_audio=on_audio, block_ms=block_ms,
            keep_alive=False, open_retries=3,
        )

    def _start_impl(self) -> None:
        if self.device_index is None:
            self.device_index = recommended_input_index()
            if self.device_index is None:
                raise RuntimeError("микрофон не найден")
        super()._start_impl()


def probe_input(device_index: int | None = None, seconds: float = 0.8) -> dict[str, Any]:
    """Короткая проба микрофона: открывается ли и есть ли сигнал."""
    got: list[np.ndarray] = []
    rec = MicRecorder(device_index=device_index, on_audio=lambda p: got.append(p.copy()))
    try:
        rec.start()
    except Exception as err:
        return {"ok": False, "reason": str(err)[:200]}
    try:
        time.sleep(max(0.2, float(seconds)))
        pcm = np.concatenate(got) if got else np.zeros(0, dtype=np.float32)
        return {
            "ok": True,
            "device": rec.device_name,
            "device_index": rec.device_index,
            "seconds": round(pcm.size / float(SR), 2),
            "peak": round(float(np.max(np.abs(pcm))) if pcm.size else 0.0, 5),
            "level": round(audio_io.rms_level(pcm), 4),
        }
    finally:
        rec.stop()


def _diag_impl() -> dict[str, Any]:
    """Подробная диагностика звукового стека — что именно видит этот процесс."""
    import pyaudiowpatch as pyaudio

    out: dict[str, Any] = {"thread": threading.current_thread().name}
    try:
        import comtypes

        out["com_module"] = True
    except Exception as err:
        out["com_module"] = str(err)

    pa = _pyaudio()
    out["portaudio"] = pyaudio.get_portaudio_version_text()
    out["device_count"] = pa.get_device_count()
    apis = []
    for i in range(pa.get_host_api_count()):
        h = pa.get_host_api_info_by_index(i)
        apis.append({"index": i, "type": h["type"], "name": h["name"],
                     "devices": h["deviceCount"],
                     "default_input": h["defaultInputDevice"],
                     "default_output": h["defaultOutputDevice"]})
    out["host_apis"] = apis

    inputs = []
    for i in range(pa.get_device_count()):
        d = pa.get_device_info_by_index(i)
        if int(d.get("maxInputChannels") or 0) <= 0:
            continue
        entry = {
            "index": i, "name": str(d.get("name"))[:60],
            "host_api": d.get("hostApi"),
            "channels": int(d.get("maxInputChannels") or 0),
            "rate": int(d.get("defaultSampleRate") or 0),
            "loopback": bool(d.get("isLoopbackDevice")),
        }
        try:
            entry["format_supported"] = bool(pa.is_format_supported(
                entry["rate"], input_device=i, input_channels=entry["channels"],
                input_format=pyaudio.paFloat32))
        except Exception as err:
            entry["format_supported"] = "нет: %s" % str(err)[:60]

        attempts = []
        for fmt, fname in ((pyaudio.paFloat32, "float32"), (pyaudio.paInt16, "int16")):
            try:
                st = pa.open(format=fmt, channels=entry["channels"], rate=entry["rate"],
                             input=True, input_device_index=i, frames_per_buffer=1024)
                try:
                    st.start_stream()
                    st.stop_stream()
                finally:
                    st.close()
                attempts.append({"format": fname, "ok": True})
                break
            except Exception as err:
                attempts.append({"format": fname, "ok": False,
                                 "error": "%s: %s" % (type(err).__name__, str(err)[:80])})
        entry["open"] = attempts
        inputs.append(entry)
    out["inputs"] = inputs
    return out


def diagnose() -> dict[str, Any]:
    return _audio_thread.call(_diag_impl)
