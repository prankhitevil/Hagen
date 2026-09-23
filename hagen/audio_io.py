"""Чтение/запись wav, приведение к 16 кГц моно, уровни сигнала."""
from __future__ import annotations

import logging
import math
import os
import shutil
import subprocess
import threading
import wave
from pathlib import Path

import numpy as np

from . import platform

log = logging.getLogger("hagen.audio_io")

SR = 16000

_MEDIA_EXT = {
    ".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v", ".wmv", ".flv",
    ".mp3", ".m4a", ".wav", ".ogg", ".opus", ".flac", ".aac", ".wma",
}



_ffmpeg_cache: dict[str, str] = {}


def tool_path(name: str) -> str:
    """Путь к ffmpeg или ffprobe.

    Сначала ищем в папке проекта (ffmpeg/bin), чтобы приложение не зависело от
    того, что установлено в системе, и переносилось одной папкой. Если своей
    копии нет — берём из PATH.
    """
    cached = _ffmpeg_cache.get(name)
    if cached:
        return cached
    exe = name + (".exe" if os.name == "nt" else "")
    for folder in (
        Path(__file__).resolve().parent.parent / "ffmpeg" / "bin",
        Path(__file__).resolve().parent.parent / "ffmpeg",
    ):
        cand = folder / exe
        if cand.exists():
            _ffmpeg_cache[name] = str(cand)
            return str(cand)
    found = shutil.which(name)
    result = found or name
    _ffmpeg_cache[name] = result
    return result


def ffmpeg_exe() -> str:
    return tool_path("ffmpeg")


def direct_env() -> dict[str, str]:
    """Окружение для дочернего ffmpeg, ffprobe и браузера — БЕЗ системного прокси.

    Локальный VPN-клиент выставляет http_proxy/https_proxy всем процессам
    подряд, и дочерняя программа послушно уходит через него: рвётся TLS до
    российских площадок, а преавторизованная ссылка (SharePoint, подписанный
    поток) выписана на прямой адрес и с другого получает отказ. Свои запросы
    программа шлёт напрямую (trust_env=False) — ребёнок должен вести себя так же.
    Раньше эта функция лежала в четырёх модулях, и копии успели разойтись.
    """
    env = dict(os.environ)
    for key in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY",
                "ALL_PROXY", "all_proxy"):
        env.pop(key, None)
    env["NO_PROXY"] = env["no_proxy"] = "*"
    return env


def ffprobe_exe() -> str:
    return tool_path("ffprobe")


def is_media(path) -> bool:
    return Path(path).suffix.lower() in _MEDIA_EXT


#: Размер заголовка простого PCM-wav, который пишет модуль wave: 12 байт RIFF,
#: 24 байта fmt и 8 байт заголовка data.
WAV_HEADER = 44
#: Как часто при дописывании обновлять длину в заголовке (в отсчётах): 2 с.
HEADER_EVERY = 2 * 16000


def repair_wav_header(path) -> int:
    """Сколько отсчётов в нашем wav на самом деле; заголовок при нужде чинится.

    Дорожка записи дописывается в конец файла, а длина в заголовке RIFF
    обновляется не на каждом куске. Если служба упала или выключилось питание,
    заголовок отстаёт от файла: звук на диске есть, а по заголовку его нет.
    Раньше такой хвост отрезался как «мусор». Теперь длину
    считаем по размеру файла, а заголовок подправляем.

    Только для простого моно-wav с заголовком в 44 байта, который пишет сама
    служба. Чужие файлы (ffmpeg кладёт после fmt ещё блок LIST) не трогаем и
    отвечаем длиной из заголовка. Возвращает число отсчётов, 0 — если файл не
    читается.
    """
    import struct

    p = Path(path)
    try:
        size = p.stat().st_size
    except OSError:
        return 0
    if size < WAV_HEADER:
        return 0
    try:
        with open(p, "r+b") as fh:
            head = fh.read(WAV_HEADER)
            if (head[:4] != b"RIFF" or head[8:12] != b"WAVE" or head[12:16] != b"fmt "
                    or head[36:40] != b"data"):
                return int(wav_duration(p) * SR)
            channels = struct.unpack("<H", head[22:24])[0]
            width = struct.unpack("<H", head[34:36])[0] // 8
            if channels != 1 or width != 2:
                return int(wav_duration(p) * SR)
            in_header = struct.unpack("<I", head[40:44])[0] // 2
            on_disk = (size - WAV_HEADER) // 2
            if on_disk > in_header:
                fh.seek(4)
                fh.write(struct.pack("<I", 36 + on_disk * 2))
                fh.seek(40)
                fh.write(struct.pack("<I", on_disk * 2))
                fh.flush()
                log.warning("дорожка %s: заголовок отставал от файла на %.1f c — починил",
                            p.name, (on_disk - in_header) / float(SR))
            return int(max(0, on_disk))
    except OSError:
        return int(wav_duration(p) * SR)


class WavWriter:
    """Потоковая запись 16 кГц моно int16. Безопасна из нескольких потоков.

    Умеет ДОПИСЫВАТЬ в готовый файл (append=True). Это нужно, чтобы в одной
    заметке можно было нажимать «Старт» и «Стоп» много раз: каждый новый сеанс
    продолжает ту же дорожку, а не затирает её. Модуль wave дописывать не умеет,
    поэтому в этом режиме файл открывается напрямую, данные пишутся в конец, а
    размеры в заголовке RIFF пересчитываются при закрытии.
    """

    def __init__(self, path, sr: int = SR, append: bool = False):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._sr = int(sr)
        self._closed = False
        self._written = 0          # отсчётов записано в этом сеансе
        self._base = 0             # отсчётов было в файле до этого сеанса
        self._wf = None            # обычная запись через модуль wave
        self._raw = None           # дописывание: файл открыт напрямую
        self._header_at = 0        # при скольких записанных отсчётах чинили заголовок

        if append and self._can_append():
            return

        self._wf = wave.open(str(self.path), "wb")
        self._wf.setnchannels(1)
        self._wf.setsampwidth(2)
        self._wf.setframerate(self._sr)

    def _can_append(self) -> bool:
        """Подготовиться к дописыванию. False — если файл не годится."""
        try:
            if not self.path.exists() or self.path.stat().st_size <= WAV_HEADER:
                return False
            # После сбоя заголовок может отставать от файла: сначала чиним его,
            # иначе дописанный в прошлый раз звук был бы отрезан ниже.
            repair_wav_header(self.path)
            with wave.open(str(self.path), "rb") as rd:
                fits = (rd.getnchannels() == 1 and rd.getsampwidth() == 2
                        and rd.getframerate() == self._sr)
                existing = rd.getnframes()
            if not fits or existing <= 0:
                return False
            self._base = int(existing)
            self._raw = open(self.path, "r+b")
            self._raw.seek(WAV_HEADER + self._base * 2)
            self._raw.truncate()     # отрезаем возможный хвост от прошлого сбоя
            return True
        except Exception:
            self._raw = None
            self._base = 0
            return False

    def write(self, pcm) -> None:
        if self._closed:
            return
        data = to_int16(pcm)
        with self._lock:
            if self._closed:
                return
            if self._raw is not None:
                self._raw.write(data.tobytes())
                self._written += int(data.shape[0])
                # Заголовок подправляем раз в пару секунд звука: два коротких
                # seek+write, зато после краха в заголовке не старая длина.
                if self._written - self._header_at >= HEADER_EVERY:
                    self._fix_header()
            else:
                self._wf.writeframes(data.tobytes())
                self._written += int(data.shape[0])

    @property
    def frames(self) -> int:
        """Сколько отсчётов в файле всего, вместе с записанными раньше."""
        return self._base + self._written

    @property
    def seconds(self) -> float:
        return self.frames / float(SR)

    def _fix_header(self) -> None:
        """Пересчитать размеры в заголовке RIFF после дописывания."""
        import struct

        data_bytes = self.frames * 2
        self._raw.seek(4)
        self._raw.write(struct.pack("<I", 36 + data_bytes))   # размер RIFF
        self._raw.seek(40)
        self._raw.write(struct.pack("<I", data_bytes))        # размер data
        self._raw.flush()
        self._raw.seek(0, 2)          # обратно в конец: дальше дописываем туда
        self._header_at = self._written

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            try:
                if self._raw is not None:
                    self._fix_header()
                    self._raw.close()
                else:
                    self._wf.close()
            except Exception:
                pass


def to_int16(pcm):
    a = np.asarray(pcm)
    if a.dtype == np.int16:
        return np.ascontiguousarray(a)
    a = to_float32(a)
    a = np.clip(a, -1.0, 1.0)
    return np.ascontiguousarray((a * 32767.0).astype(np.int16))


def to_float32(pcm):
    a = np.asarray(pcm)
    if a.dtype == np.float32:
        return a
    if a.dtype == np.int16:
        return a.astype(np.float32) / 32768.0
    if a.dtype == np.int32:
        return a.astype(np.float32) / 2147483648.0
    if a.dtype == np.uint8:
        return (a.astype(np.float32) - 128.0) / 128.0
    return a.astype(np.float32, copy=False)


def read_wav(path):
    """Читает wav как float32 моно. Ничего не ресемплит. Возвращает (pcm, sr)."""
    with wave.open(str(path), "rb") as wf:
        sr = wf.getframerate()
        ch = wf.getnchannels()
        width = wf.getsampwidth()
        raw = wf.readframes(wf.getnframes())
    if width == 2:
        data = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    elif width == 4:
        data = np.frombuffer(raw, dtype=np.int32).astype(np.float32) / 2147483648.0
    elif width == 1:
        data = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    else:
        raise ValueError("неподдерживаемая разрядность wav: %d бит" % (width * 8))
    if ch > 1:
        usable = (data.shape[0] // ch) * ch
        data = data[:usable].reshape(-1, ch).mean(axis=1)
    return np.ascontiguousarray(data.astype(np.float32)), sr


def wav_duration(path) -> float:
    try:
        with wave.open(str(path), "rb") as wf:
            return wf.getnframes() / float(wf.getframerate() or SR)
    except Exception:
        return 0.0


def write_wav(path, pcm, sr: int = SR) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(p), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(to_int16(pcm).tobytes())


def downmix(pcm, channels: int):
    """Многоканальный interleaved в моно float32."""
    a = to_float32(pcm)
    if channels <= 1:
        return a
    usable = (a.shape[0] // channels) * channels
    if usable == 0:
        return np.zeros(0, dtype=np.float32)
    return np.ascontiguousarray(a[:usable].reshape(-1, channels).mean(axis=1).astype(np.float32))


def resample(pcm, src_sr: int, dst_sr: int = SR):
    """Разовый ресемплинг. soxr, если есть; иначе polyphase из scipy."""
    a = to_float32(pcm)
    if int(src_sr) == int(dst_sr) or a.size == 0:
        return a
    try:
        import soxr

        return np.ascontiguousarray(soxr.resample(a, src_sr, dst_sr, quality="QQ").astype(np.float32))
    except Exception:
        from scipy.signal import resample_poly

        g = math.gcd(int(src_sr), int(dst_sr))
        up, down = int(dst_sr // g), int(src_sr // g)
        return np.ascontiguousarray(resample_poly(a, up, down).astype(np.float32))


class Resampler:
    """Потоковый ресемплер с состоянием: на стыках чанков нет щелчков."""

    def __init__(self, src_sr: int, dst_sr: int = SR):
        self.src_sr = int(src_sr)
        self.dst_sr = int(dst_sr)
        self._impl = None
        if self.src_sr != self.dst_sr:
            try:
                import soxr

                self._impl = soxr.ResampleStream(
                    self.src_sr, self.dst_sr, 1, dtype="float32", quality="QQ"
                )
            except Exception:
                self._impl = None

    def __call__(self, pcm):
        a = to_float32(pcm)
        if self.src_sr == self.dst_sr or a.size == 0:
            return a
        if self._impl is not None:
            try:
                return np.ascontiguousarray(self._impl.resample_chunk(a))
            except Exception:
                self._impl = None
        return resample(a, self.src_sr, self.dst_sr)


def rms_level(pcm) -> float:
    """Уровень 0..1 для полоски в интерфейсе, логарифмическая шкала от -60 дБ."""
    a = to_float32(pcm)
    if a.size == 0:
        return 0.0
    rms = float(np.sqrt(np.mean(np.square(a.astype(np.float64)))))
    if rms <= 1e-7:
        return 0.0
    db = 20.0 * math.log10(rms)
    return float(min(1.0, max(0.0, (db + 60.0) / 60.0)))


def peak_level(pcm) -> float:
    a = to_float32(pcm)
    return 0.0 if a.size == 0 else float(min(1.0, float(np.max(np.abs(a)))))


def ffmpeg_to_wav16k(src, dst) -> float:
    """Вытаскивает звук из медиафайла в 16 кГц моно wav. Возвращает длительность."""
    src, dst = Path(src), Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        ffmpeg_exe(), "-hide_banner", "-nostdin", "-loglevel", "error", "-y",
        "-i", str(src),
        "-vn", "-map", "0:a:0", "-ac", "1", "-ar", str(SR),
        "-c:a", "pcm_s16le", str(dst),
    ]
    proc = subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=3 * 60 * 60, creationflags=platform.system().hidden_process_flags(),
    )
    if proc.returncode != 0 or not dst.exists():
        tail = (proc.stderr or "").strip().splitlines()[-4:]
        raise RuntimeError("ffmpeg не смог извлечь звук: " + " / ".join(tail))
    return wav_duration(dst)


def media_duration(src) -> float:
    cmd = [
        ffprobe_exe(), "-hide_banner", "-v", "error", "-show_entries",
        "format=duration", "-of", "default=nw=1:nk=1", str(src),
    ]
    try:
        out = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=120, creationflags=platform.system().hidden_process_flags(),
        )
        return float((out.stdout or "0").strip() or 0.0)
    except Exception:
        return 0.0
