# -*- coding: utf-8 -*-
"""Распознавание файлов в облаке через OpenAI-совместимый эндпоинт.

Зачем он нужен, если локальное распознавание бесплатное и на русском точнее:
облако не занимает процессор. Пока идёт расшифровка часовой записи, все шесть
ядер заняты, и работать на этой машине неприятно. Поэтому это переключатель, а
не замена: по умолчанию всё считается здесь.

Живой микрофон сюда не попадает НИКОГДА: непрерывно лить звук в чужой сервис
нельзя, да и смысла нет — эфир и так распознаётся мгновенно.

Адрес, ключ и модель — своё подключение «asr» (providers.connection), запасная
модель — asr_fallback.
"""
from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Any, Callable

from . import audio_io, config, platform, providers, store

log = logging.getLogger("hagen.asr_cloud")

#: Предел размера одного запроса. У polza в документации «около 15 МБ», при этом
#: на другой странице 50 МБ — ориентируемся на меньшее. Кусок 15 минут в mp3
#: моно 24 кбит/с весит около 2,7 МБ, так что запас пятикратный.
MAX_BODY_MB = 14.0

#: Модели, которым можно просить отметки времени ПО СЛОВАМ. Параметр
#: timestamp_granularities поддерживает только whisper-1: у gpt-4o-transcribe он
#: даёт прямую ошибку, у остальных поведение не описано. Поэтому пробуем и,
#: если сервис ругается именно на этот параметр, повторяем без него.
_WORD_HINT = ("whisper-1",)


def _direct_env() -> dict[str, str]:
    """Окружение для дочернего ffmpeg: без прокси.

    При включённом локальном VPN унаследованные http_proxy/https_proxy рвут
    ffmpeg, хотя он никуда и не ходит по сети в нашем случае. Дешевле убрать.
    """
    import os

    env = dict(os.environ)
    for key in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY",
                "all_proxy", "ALL_PROXY"):
        env.pop(key, None)
    env["NO_PROXY"] = "*"
    return env


def available() -> tuple[bool, str]:
    """Готов ли облачный путь, и если нет — почему, человеческим языком."""
    problem = providers.problem("asr")
    if problem:
        return False, problem + " Настройки → Модели → «Облачное распознавание файлов»."
    conn = providers.connection("asr")
    if not conn["model"]:
        return False, "Не указана модель распознавания в настройках."
    return True, "Готово: %s, модель %s." % (conn["service"], conn["model"])


def estimate_cost(seconds: float) -> str:
    """Во сколько обойдётся эта запись. Цену берём из списка моделей сервиса."""
    model = str(config.get("asr_model") or "")
    cached = providers.cached_models("asr") or {}
    for item in cached.get("stt") or []:
        if item.get("id") != model:
            continue
        price = item.get("price") or {}
        per_min = price.get("per_minute")
        if per_min:
            cur = "₽" if price.get("currency") == "RUB" else (price.get("currency") or "")
            return "примерно %.1f %s" % (float(per_min) * seconds / 60.0, cur)
    return "цена неизвестна: обновите список моделей в настройках"


# ---------------------------------------------------------------- нарезка


def split_to_chunks(src: Path, dst_dir: Path, minutes: int | None = None) -> list[Path]:
    """Нарезать звук на куски mp3 моно 16 кГц. Возвращает пути по порядку.

    Резать надо не от жадности, а потому что у сервисов есть предел размера
    запроса и времени обработки. Куски идут встык: перекрытия нет, зато нет и
    двойного текста на стыках.
    """
    minutes = int(minutes or config.get("chunk_min") or 15)
    minutes = max(1, min(60, minutes))
    dst_dir.mkdir(parents=True, exist_ok=True)
    for old in dst_dir.glob("chunk_*.mp3"):
        old.unlink(missing_ok=True)
    cmd = [
        audio_io.ffmpeg_exe(), "-hide_banner", "-nostdin", "-loglevel", "error", "-y",
        "-i", str(src),
        "-vn", "-map", "0:a:0", "-ac", "1", "-ar", "16000", "-b:a", "24k",
        "-f", "segment", "-segment_time", str(minutes * 60), "-reset_timestamps", "1",
        str(dst_dir / "chunk_%03d.mp3"),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=3 * 60 * 60,
                          creationflags=platform.system().hidden_process_flags(),
                          env=_direct_env())
    if proc.returncode != 0:
        detail = providers.first_lines(proc.stderr, 2, 200)
        raise RuntimeError("Не удалось подготовить звук для отправки: %s" % detail)
    chunks = sorted(dst_dir.glob("chunk_*.mp3"))
    if not chunks:
        raise RuntimeError("Звук не удалось нарезать: получилось ноль кусков.")
    too_big = [c for c in chunks if c.stat().st_size > MAX_BODY_MB * 1024 * 1024]
    if too_big:
        raise RuntimeError(
            "Кусок звука получился больше %.0f МБ — сервис его не примет. "
            "Уменьшите «кусок аудио» в настройках." % MAX_BODY_MB)
    return chunks


# ---------------------------------------------------------------- запрос


def _lang() -> str:
    lang = str(config.get("asr_lang") or "ru").strip().lower()
    return lang if lang in ("ru", "en") else "ru"


def _post_chunk(path: Path, model: str, want_words: bool,
                fmt: str = "verbose_json", lang: str | None = None) -> dict[str, Any]:
    """Один кусок в сервис. Возвращает разобранный ответ.

    lang — язык задания; пусто — из общих настроек (раньше язык из формы
    «Видео» в облако не доходил).
    """
    conn = providers.connection("asr")
    key = conn["key"]
    url = "%s/audio/transcriptions" % conn["base_url"]
    data: list[tuple[str, str]] = [("model", model), ("response_format", fmt)]
    lang = lang if lang in ("ru", "en") else _lang()
    if lang:
        data.append(("language", lang))
    if want_words:
        # Массив в multipart передаётся повторением поля с квадратными
        # скобками в имени — это соглашение OpenAI.
        data.append(("timestamp_granularities[]", "word"))
        data.append(("timestamp_granularities[]", "segment"))
    with open(path, "rb") as fh:
        return providers.http_post(
            url,
            headers={"Authorization": "Bearer %s" % key},
            data=data,
            files={"file": (path.name, fh, "audio/mpeg")},
            service=conn["service"],
            secrets=[key],
            timeout=600.0,
            attempts=3,
        )


def _is_granularity_error(err: Exception) -> bool:
    blob = str(err).lower()
    return "timestamp_granularities" in blob or "granularit" in blob


def _transcribe_chunk(path: Path, model: str, fallback: str,
                      handle: Any = None, lang: str | None = None
                      ) -> tuple[dict[str, Any], str, bool, bool]:
    """Кусок с откатами. Возвращает (ответ, модель, есть ли слова, грубо ли)."""
    want_words = any(m in model for m in _WORD_HINT)
    try:
        data = _post_chunk(path, model, want_words, lang=lang)
        return data, model, want_words, False
    except RuntimeError as err:
        if want_words and _is_granularity_error(err):
            log.info("сервис не принял отметки по словам, повторяю без них")
            data = _post_chunk(path, model, False, lang=lang)
            return data, model, False, False
        if not fallback or fallback == model:
            raise
        log.info("основная модель не ответила (%s), пробую запасную %s",
                 providers.first_lines(str(err), 1, 120), fallback)
        if handle is not None:
            try:
                handle.log("основная модель не ответила, пробую запасную")
            except Exception:
                pass
    # Запасная: сперва тоже с подробным ответом, и только потом «только текст».
    try:
        data = _post_chunk(path, fallback, False, lang=lang)
        return data, fallback, False, False
    except RuntimeError:
        data = _post_chunk(path, fallback, False, fmt="json", lang=lang)
        return data, fallback, False, True


# ---------------------------------------------------------------- разбор ответа


def _words_in(words: list[dict[str, Any]], start: float, end: float,
              offset: float) -> list[dict[str, Any]]:
    out = []
    for w in words:
        try:
            ws = float(w.get("start"))
            we = float(w.get("end"))
        except (TypeError, ValueError):
            continue
        if ws < start - 0.05 or ws > end + 0.05:
            continue
        text = str(w.get("word") or w.get("text") or "").strip()
        if text:
            out.append({"text": text, "start": offset + ws, "end": offset + we})
    return out


def _segments_from(data: dict[str, Any], offset: float) -> list[dict[str, Any]]:
    """Из ответа сервиса — реплики в формате хранилища «Hagen»."""
    raw_segments = data.get("segments")
    words = [w for w in (data.get("words") or []) if isinstance(w, dict)]
    out: list[dict[str, Any]] = []
    if isinstance(raw_segments, list) and raw_segments:
        for seg in raw_segments:
            if not isinstance(seg, dict):
                continue
            text = str(seg.get("text") or "").strip()
            if not text:
                continue
            try:
                start = float(seg.get("start") or 0.0)
                end = float(seg.get("end") or start)
            except (TypeError, ValueError):
                start, end = 0.0, 0.0
            item = store.make_segment(store.TRACK_FILE, offset + start, offset + end,
                                      text, speaker="Участник", speaker_key="file")
            item["words"] = _words_in(words, start, end, offset)
            out.append(item)
        return out
    # Формат «только текст»: одна реплика на весь кусок, времени внутри нет.
    text = str(data.get("text") or "").strip()
    if text:
        out.append(store.make_segment(store.TRACK_FILE, offset, offset, text,
                                      speaker="Участник", speaker_key="file"))
    return out


# ---------------------------------------------------------------- публичное


def transcribe_file(path: Path, handle: Any = None, lang: str | None = None,
                    progress: Callable[[float, str], None] | None = None) -> dict[str, Any]:
    """Распознать файл в облаке. Возвращает реплики и что именно получилось."""
    ok, why = available()
    if not ok:
        raise RuntimeError(why)
    src = Path(path)
    if not src.exists():
        raise RuntimeError("Файл для распознавания не найден: %s" % src.name)

    model = str(config.get("asr_model") or "").strip()
    fallback = str(config.get("asr_fallback") or "").strip()
    # Те же пределы, что в split_to_chunks: иначе смещение времени кусков
    # считалось бы по незажатому числу и таймкоды уезжали.
    minutes = max(1, min(60, int(config.get("chunk_min") or 15)))
    work = src.parent / "_cloud"
    chunks = split_to_chunks(src, work, minutes)
    if handle is not None:
        try:
            handle.log("кусков для отправки: %d (по %d мин)" % (len(chunks), minutes))
        except Exception:
            pass

    segments: list[dict[str, Any]] = []
    used_model = model
    any_words = False
    degraded = False
    try:
        for i, chunk in enumerate(chunks):
            if handle is not None:
                try:
                    if handle.cancelled:
                        raise RuntimeError("Распознавание отменено.")
                except AttributeError:
                    pass
            if progress is not None:
                try:
                    progress(i / float(len(chunks)),
                             "отправляю кусок %d из %d" % (i + 1, len(chunks)))
                except Exception:
                    pass
            data, used_model, words_ok, rough = _transcribe_chunk(
                chunk, model, fallback, handle=handle, lang=lang)
            any_words = any_words or words_ok
            degraded = degraded or rough
            offset = float(i * minutes * 60)
            segments.extend(_segments_from(data, offset))
    finally:
        for chunk in chunks:
            chunk.unlink(missing_ok=True)
        try:
            work.rmdir()
        except OSError:
            pass

    if not segments:
        raise RuntimeError("Сервис распознавания вернул пустой текст.")
    duration = max(float(s.get("end") or 0.0) for s in segments)
    log.info("облачное распознавание: реплик %d, модель %s, слова %s, грубо %s",
             len(segments), used_model, any_words, degraded)
    return {
        "segments": segments,
        "model": used_model,
        "words": any_words,
        "degraded": degraded,
        "chunks": len(chunks),
        "cost": estimate_cost(duration),
    }
