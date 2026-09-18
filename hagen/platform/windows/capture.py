# -*- coding: utf-8 -*-
"""Захват звука ОТДЕЛЬНЫМИ ПРОЦЕССАМИ.

Почему так. На этой машине захват звука внутри долгоживущего процесса
ненадёжен: один и тот же код то открывает устройство, то отвечает «Invalid
device» (-9996) — и сразу для всех устройств и всех звуковых API, включая
старые. Короткоживущий процесс открывает устройство стабильно: ffmpeg в таком
режиме не отказал ни разу.

Отсюда схема (кто её вызывает — live.DeviceCapture):
    собеседники — процесс-помощник петли (WASAPI loopback), читаем stdout;
    микрофон    — запасной путь: процесс ffmpeg (DirectShow). Основной путь
                  микрофона — WASAPI в самой службе (loopback.MicRecorder).
Из процессов служба читает готовый поток 16 кГц моно float32.
Обхода защиты здесь нет: оба процесса просят звук у Windows обычным образом.

Имя устройства для ffmpeg берём в «альтернативном» виде: оно состоит только из
латиницы и цифр, поэтому не ломается на кодировках.
"""
from __future__ import annotations

import logging
import re
import shutil
import subprocess
import threading
import time
from typing import Any, Callable

import numpy as np

from ... import audio_io, config

log = logging.getLogger("hagen.capture")

SR = audio_io.SR
from .app import NO_WINDOW  # noqa: E402  дочерние программы без окна консоли
BLOCK_SAMPLES = 1600            # 100 мс
BLOCK_BYTES = BLOCK_SAMPLES * 4  # float32

_STEREO_MIX_WORDS = ("стерео микшер", "стереомикшер", "stereo mix", "what you hear")
_devices_cache: tuple[float, list[dict[str, Any]]] | None = None
_devices_lock = threading.Lock()


def ffmpeg_path() -> str:
    """Своя копия в папке проекта имеет приоритет над системной."""
    return audio_io.ffmpeg_exe()


def _run(cmd: list[str], timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=timeout, creationflags=NO_WINDOW,
    )


def list_devices(refresh: bool = False) -> list[dict[str, Any]]:
    """Устройства записи, которые видит ffmpeg.

    Возвращает список словарей:
      name        — как показывать пользователю
      alt_name    — устойчивое имя для ffmpeg (латиница)
      kind        — "mic" либо "mix" (стереомикшер, то есть звук из колонок)
    """
    global _devices_cache
    with _devices_lock:
        if not refresh and _devices_cache is not None and time.time() - _devices_cache[0] < 20:
            return list(_devices_cache[1])

        out: list[dict[str, Any]] = []
        try:
            res = _run([ffmpeg_path(), "-hide_banner", "-list_devices", "true",
                        "-f", "dshow", "-i", "dummy"])
        except Exception as err:
            log.warning("не удалось спросить у ffmpeg список устройств: %s", err)
            _devices_cache = (time.time(), out)
            return []

        text = res.stderr or ""
        current: dict[str, Any] | None = None
        for line in text.splitlines():
            m = re.search(r'"([^"]+)"\s+\(audio\)', line)
            if m:
                nm = m.group(1)
                low = nm.lower()
                current = {
                    "name": nm,
                    "alt_name": None,
                    "kind": "mix" if any(w in low for w in _STEREO_MIX_WORDS) else "mic",
                }
                out.append(current)
                continue
            m = re.search(r'Alternative name\s+"([^"]+)"', line)
            if m and current is not None:
                current["alt_name"] = m.group(1)
                current = None
        _devices_cache = (time.time(), out)
        return list(out)


def default_mic_device() -> dict[str, Any] | None:
    devs = [d for d in list_devices() if d.get("kind") == "mic"]
    return devs[0] if devs else None


def find_device(needle: str | None) -> dict[str, Any] | None:
    """Найти устройство по имени или альтернативному имени, частичное совпадение."""
    if not needle:
        return None
    want = str(needle).strip().lower()
    devs = list_devices()
    for d in devs:
        if (d.get("alt_name") or "").lower() == want:
            return d
    for d in devs:
        if (d.get("name") or "").lower() == want:
            return d
    for d in devs:
        nm = (d.get("name") or "").lower()
        if want and (want in nm or nm in want):
            return d
    return None


def probe_device(device: dict[str, Any] | str, seconds: float = 1.0) -> dict[str, Any]:
    """Короткая проба: доступно ли устройство и есть ли в нём сигнал."""
    dev = device if isinstance(device, dict) else find_device(device)
    if dev is None:
        return {"ok": False, "reason": "устройство не найдено"}
    spec = "audio=%s" % (dev.get("alt_name") or dev.get("name"))
    cmd = [ffmpeg_path(), "-hide_banner", "-loglevel", "error", "-nostdin",
           "-f", "dshow", "-audio_buffer_size", "80", "-i", spec,
           "-t", "%.2f" % seconds, "-ac", "1", "-ar", str(SR),
           "-f", "f32le", "pipe:1"]
    try:
        res = subprocess.run(cmd, capture_output=True, timeout=int(seconds) + 25,
                            creationflags=NO_WINDOW)
    except Exception as err:
        return {"ok": False, "reason": str(err)[:200], "device": dev["name"]}
    if res.returncode != 0 or not res.stdout:
        tail = (res.stderr or b"").decode("utf-8", "replace").strip().splitlines()[-1:]
        return {"ok": False, "reason": " / ".join(tail) or "нет данных", "device": dev["name"]}
    pcm = np.frombuffer(res.stdout, dtype="<f4")
    return {
        "ok": True,
        "device": dev["name"],
        "kind": dev.get("kind"),
        "seconds": round(len(pcm) / float(SR), 2),
        "peak": round(float(np.max(np.abs(pcm))) if pcm.size else 0.0, 5),
        "level": round(audio_io.rms_level(pcm), 4),
    }


class FfmpegCapture:
    """Один поток захвата: ffmpeg пишет в канал, мы читаем куски по 100 мс."""

    STALL_AFTER_S = 3.0
    SILENCE_AFTER_S = 5.0
    SILENCE_PEAK = 2.0e-4

    def __init__(
        self,
        device: dict[str, Any] | str,
        on_audio: Callable[[np.ndarray], None] | None = None,
        label: str = "",
    ):
        dev = device if isinstance(device, dict) else find_device(device)
        if dev is None:
            raise RuntimeError("устройство записи не найдено: %s" % device)
        self.device = dev
        self.on_audio = on_audio
        self.label = label or dev.get("name") or "?"

        self._proc: subprocess.Popen | None = None
        self._reader: threading.Thread | None = None
        self._errwatch: threading.Thread | None = None
        self._stop = threading.Event()

        self.level = 0.0
        self.peak = 0.0
        self.error: str | None = None
        self.stderr_tail: list[str] = []
        self._frames = 0
        self._started = 0.0
        self._last_data = 0.0
        self._last_loud = 0.0

    # ------------------------------------------------------------- запуск
    def start(self) -> None:
        spec = "audio=%s" % (self.device.get("alt_name") or self.device.get("name"))
        cmd = [
            ffmpeg_path(), "-hide_banner", "-loglevel", "warning", "-nostdin",
            "-f", "dshow",
            "-audio_buffer_size", "60",     # небольшая задержка, иначе эфир отстаёт
            "-rtbufsize", "64M",
            "-i", spec,
            "-ac", "1", "-ar", str(SR),
            "-f", "f32le", "pipe:1",
        ]
        log.info("запускаю захват «%s» -> %s", self.label, self.device.get("name"))
        self._proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL, bufsize=0, creationflags=NO_WINDOW,
        )
        now = time.time()
        self._started = now
        self._last_data = now
        self._last_loud = now
        self._stop.clear()
        self._reader = threading.Thread(target=self._read_loop, name="cap-%s" % self.label,
                                        daemon=True)
        self._reader.start()
        self._errwatch = threading.Thread(target=self._err_loop, daemon=True)
        self._errwatch.start()

        # ждём первые данные, чтобы сразу увидеть отказ устройства
        deadline = now + 6.0
        while time.time() < deadline:
            if self._frames > 0:
                return
            if self._proc.poll() is not None:
                break
            time.sleep(0.1)
        if self._frames == 0:
            msg = " / ".join(self.stderr_tail[-2:]) or "устройство не отдало звук"
            self.stop()
            raise RuntimeError("не удалось начать запись с «%s»: %s"
                               % (self.device.get("name"), msg))

    def _read_loop(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        buf = b""
        while not self._stop.is_set():
            try:
                chunk = proc.stdout.read(BLOCK_BYTES)
            except Exception as err:
                if not self._stop.is_set():
                    self.error = str(err)
                break
            if not chunk:
                break
            buf += chunk
            while len(buf) >= BLOCK_BYTES:
                block, buf = buf[:BLOCK_BYTES], buf[BLOCK_BYTES:]
                pcm = np.frombuffer(block, dtype="<f4")
                now = time.time()
                self._last_data = now
                self.peak = audio_io.peak_level(pcm)
                self.level = audio_io.rms_level(pcm)
                if self.peak > self.SILENCE_PEAK:
                    self._last_loud = now
                self._frames += int(pcm.size)
                if self.on_audio is not None:
                    try:
                        self.on_audio(pcm)
                    except Exception as err:
                        log.error("обработчик звука «%s» упал: %s", self.label, err)
        if buf and not self._stop.is_set():
            usable = (len(buf) // 4) * 4
            if usable:
                pcm = np.frombuffer(buf[:usable], dtype="<f4")
                self._frames += int(pcm.size)
                if self.on_audio is not None:
                    try:
                        self.on_audio(pcm)
                    except Exception:
                        pass

    def _err_loop(self) -> None:
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        for raw in iter(proc.stderr.readline, b""):
            if self._stop.is_set():
                break
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            self.stderr_tail.append(line)
            del self.stderr_tail[:-8]
            low = line.lower()
            if "error" in low or "i/o" in low or "denied" in low or "could not" in low:
                self.error = line[:200]
                log.warning("захват «%s»: %s", self.label, line[:200])

    # ------------------------------------------------------------- остановка
    def stop(self) -> None:
        self._stop.set()
        proc = self._proc
        self._proc = None
        if proc is not None:
            for step in ("terminate", "kill"):
                if proc.poll() is not None:
                    break
                try:
                    getattr(proc, step)()
                except Exception:
                    pass
                try:
                    proc.wait(timeout=3)
                except Exception:
                    pass
            for stream in (proc.stdout, proc.stderr):
                try:
                    if stream is not None:
                        stream.close()
                except Exception:
                    pass
        for th in (self._reader, self._errwatch):
            if th is not None and th.is_alive():
                th.join(timeout=2.0)
        self._reader = None
        self._errwatch = None

    # ------------------------------------------------------------- состояние
    @property
    def seconds(self) -> float:
        return self._frames / float(SR)

    @property
    def alive(self) -> bool:
        proc = self._proc
        return proc is not None and proc.poll() is None

    @property
    def stalled(self) -> bool:
        if not self._started:
            return False
        return (time.time() - self._last_data) > self.STALL_AFTER_S

    @property
    def silent(self) -> bool:
        if not self._started:
            return False
        return (time.time() - self._last_loud) > self.SILENCE_AFTER_S

    def status(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "device": self.device.get("name"),
            "kind": self.device.get("kind"),
            "level": round(self.level, 4),
            "peak": round(self.peak, 4),
            "seconds": round(self.seconds, 2),
            "alive": self.alive,
            "stalled": self.stalled,
            "silent": self.silent,
            "error": self.error,
        }



class LoopbackProcess:
    """Петля вывода через отдельный процесс-помощник.

    Короткоживущий процесс открывает устройство надёжно, тогда как долгоживущая
    служба на этой машине периодически получает отказ. Читаем из него поток
    16 кГц моно float32 точно так же, как из ffmpeg.
    """

    STALL_AFTER_S = 4.0
    SILENCE_AFTER_S = 6.0
    SILENCE_PEAK = 2.0e-4

    def __init__(
        self,
        device_index: int | None = None,
        on_audio: Callable[[np.ndarray], None] | None = None,
        label: str = "собеседники",
        want_name: str | None = None,
    ):
        self.device_index = device_index
        self.want_name = want_name      # куда программа звонка выводит звук
        self.choice = ""
        self.on_audio = on_audio
        self.label = label

        self._proc: subprocess.Popen | None = None
        self._reader: threading.Thread | None = None
        self._errwatch: threading.Thread | None = None
        self._stop = threading.Event()

        self.device_name = ""
        self.level = 0.0
        self.peak = 0.0
        self.error: str | None = None
        self.stderr_tail: list[str] = []
        self._frames = 0
        self._started = 0.0
        self._last_data = 0.0
        self._last_loud = 0.0

    def start(self) -> None:
        import sys

        import os

        cmd = [sys.executable, "-u", "-m", "hagen.platform.windows.loopback_worker",
               "--parent-pid", str(os.getpid())]
        if self.device_index is not None:
            cmd += ["--device", str(int(self.device_index))]
        elif self.want_name:
            cmd += ["--device-name", str(self.want_name)]
        log.info("запускаю помощник записи собеседников")
        self._proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL, bufsize=0, cwd=str(config.PROJECT_DIR),
            creationflags=NO_WINDOW,
        )
        now = time.time()
        self._started = now
        self._last_data = now
        self._last_loud = now
        self._stop.clear()
        self._reader = threading.Thread(target=self._read_loop, name="far-reader", daemon=True)
        self._reader.start()
        self._errwatch = threading.Thread(target=self._err_loop, name="far-err", daemon=True)
        self._errwatch.start()

        # ждём либо первые данные, либо внятную ошибку
        deadline = now + 6.0
        while time.time() < deadline:
            if self._frames > 0 or self.device_name:
                return
            if self._proc.poll() is not None:
                break
            time.sleep(0.1)
        if self._frames == 0 and not self.device_name:
            msg = self.error or " / ".join(self.stderr_tail[-2:]) or "устройство не открылось"
            self.stop()
            raise RuntimeError("не удалось начать запись собеседников: %s" % msg)

    def _read_loop(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        buf = b""
        while not self._stop.is_set():
            try:
                chunk = proc.stdout.read(BLOCK_BYTES)
            except Exception as err:
                if not self._stop.is_set():
                    self.error = str(err)
                break
            if not chunk:
                break
            buf += chunk
            while len(buf) >= BLOCK_BYTES:
                block, buf = buf[:BLOCK_BYTES], buf[BLOCK_BYTES:]
                pcm = np.frombuffer(block, dtype="<f4")
                now = time.time()
                self._last_data = now
                self.peak = audio_io.peak_level(pcm)
                self.level = audio_io.rms_level(pcm)
                if self.peak > self.SILENCE_PEAK:
                    self._last_loud = now
                self._frames += int(pcm.size)
                if self.on_audio is not None:
                    try:
                        self.on_audio(pcm)
                    except Exception as err:
                        log.error("обработчик дорожки собеседников упал: %s", err)

    def _err_loop(self) -> None:
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        for raw in iter(proc.stderr.readline, b""):
            if self._stop.is_set():
                break
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            self.stderr_tail.append(line)
            del self.stderr_tail[:-8]
            if line.startswith("ERR "):
                self.error = line[4:][:240]
                log.warning("помощник записи собеседников: %s", self.error)
            elif line.startswith("INFO "):
                log.info("помощник записи собеседников: %s", line[5:])
                if line.startswith("INFO выбор устройства:"):
                    self.choice = line.split(":", 1)[-1].strip()
                if "открыто устройство" in line:
                    name = line.split("]", 1)[-1].strip()
                    # хвост «, 48000 Гц, 2 кан.» к имени устройства не относится
                    self.device_name = re.sub(r",\s*\d+\s*Гц.*$", "", name).strip()

    def stop(self) -> None:
        self._stop.set()
        proc = self._proc
        self._proc = None
        if proc is not None:
            for step in ("terminate", "kill"):
                if proc.poll() is not None:
                    break
                try:
                    getattr(proc, step)()
                except Exception:
                    pass
                try:
                    proc.wait(timeout=3)
                except Exception:
                    pass
            for stream in (proc.stdout, proc.stderr):
                try:
                    if stream is not None:
                        stream.close()
                except Exception:
                    pass
        for th in (self._reader, self._errwatch):
            if th is not None and th.is_alive():
                th.join(timeout=2.0)
        self._reader = None
        self._errwatch = None

    @property
    def seconds(self) -> float:
        return self._frames / float(SR)

    @property
    def alive(self) -> bool:
        proc = self._proc
        return proc is not None and proc.poll() is None

    @property
    def stalled(self) -> bool:
        if not self._started:
            return False
        return (time.time() - self._last_data) > self.STALL_AFTER_S

    @property
    def silent(self) -> bool:
        if not self._started:
            return False
        return (time.time() - self._last_loud) > self.SILENCE_AFTER_S

    def status(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "device": self.device_name or "петля вывода",
            "kind": "loopback",
            "level": round(self.level, 4),
            "peak": round(self.peak, 4),
            "seconds": round(self.seconds, 2),
            "alive": self.alive,
            "stalled": self.stalled,
            "silent": self.silent,
            "error": self.error,
        }


def probe_loopback_process(device_index: int | None = None,
                           seconds: float = 1.2) -> dict[str, Any]:
    """Короткая проба петли вывода через процесс-помощник."""
    got: list[np.ndarray] = []
    rec = LoopbackProcess(device_index=device_index, on_audio=lambda p: got.append(p.copy()))
    try:
        rec.start()
    except Exception as err:
        return {"ok": False, "reason": str(err)[:240]}
    try:
        time.sleep(max(0.3, float(seconds)))
        pcm = np.concatenate(got) if got else np.zeros(0, dtype=np.float32)
        return {
            "ok": pcm.size > 0,
            "device": rec.device_name or "петля вывода",
            "seconds": round(pcm.size / float(SR), 2),
            "peak": round(float(np.max(np.abs(pcm))) if pcm.size else 0.0, 5),
            "level": round(audio_io.rms_level(pcm), 4),
            "reason": "" if pcm.size else (rec.error or "данных не пришло"),
        }
    finally:
        rec.stop()


def diag_ffmpeg(seconds: float = 0.6) -> dict[str, Any]:
    """Подробный запуск ffmpeg с полным протоколом — для разбора отказов."""
    import os

    devs = list_devices(refresh=True)
    out: dict[str, Any] = {
        "ffmpeg": ffmpeg_path(),
        "cwd": os.getcwd(),
        "pid": os.getpid(),
        "devices": [{"name": d["name"], "alt": d["alt_name"]} for d in devs],
        "attempts": [],
    }
    for d in devs:
        if d.get("kind") != "mic":
            continue
        for use_alt in (True, False):
            spec = "audio=%s" % ((d.get("alt_name") if use_alt else d.get("name")) or d["name"])
            cmd = [ffmpeg_path(), "-hide_banner", "-loglevel", "verbose", "-nostdin",
                   "-f", "dshow", "-i", spec, "-t", "%.2f" % seconds,
                   "-ac", "1", "-ar", str(SR), "-f", "f32le", "pipe:1"]
            try:
                res = subprocess.run(cmd, capture_output=True, timeout=40,
                                    stdin=subprocess.DEVNULL, creationflags=NO_WINDOW)
                err = (res.stderr or b"").decode("utf-8", "replace")
                out["attempts"].append({
                    "device": d["name"][:40],
                    "by": "alt" if use_alt else "name",
                    "code": res.returncode,
                    "bytes": len(res.stdout or b""),
                    "stderr": err[-1600:],
                })
            except Exception as err:
                out["attempts"].append({"device": d["name"][:40],
                                        "by": "alt" if use_alt else "name",
                                        "error": str(err)[:200]})
    return out
