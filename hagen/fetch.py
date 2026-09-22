# -*- coding: utf-8 -*-
"""Скачивание звуковой дорожки видео по ссылке (YouTube и прочие площадки).

Качаем ТОЛЬКО звук: видеоряд для расшифровки не нужен, а вес отличается в
разы. Дальше файл идёт ровно тем же путём, что и видео, перетащенное в окно:
ffmpeg приводит его к 16 кГц моно, затем обычное распознавание.

yt-dlp подключается БИБЛИОТЕКОЙ, а не отдельной программой. Причина простая:
каждый новый .exe в папке — это ещё один вопрос антивируса, а у библиотеки
исполняемым файлом остаётся всё тот же python.exe, уже доверенный.

При установке ставится версия yt-dlp из описи выпуска (lock.json), но кнопка
«Обновить yt-dlp» берёт свежую: площадки часто меняют выдачу, и рабочим
остаётся только свежий. Обновление программы такой yt-dlp назад не откатывает.
Если ссылки перестали открываться — сначала обновить.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Callable

from . import audio_io, config

log = logging.getLogger("hagen.fetch")

#: Языки субтитров, которые нас интересуют, в порядке предпочтения.
#: Только ОРИГИНАЛЬНЫЕ дорожки: автопереводы отсекаются тем, что в списке нет
#: шаблонов вроде "ru.*" — иначе YouTube подсунет машинный перевод с любого языка.
#: У авторских субтитров оригинал лежит в "ru", у автоматических — в "ru-orig",
#: поэтому для двух проходов порядок разный.
SUB_LANGS_MANUAL = ["ru", "en"]
SUB_LANGS_AUTO = ["ru-orig", "ru", "en-orig", "en"]

#: Типовые локальные порты VPN-клиентов. Перебираем их, когда в настройках
#: стоит «auto»: YouTube открывается не всегда напрямую.
_VPN_PORTS = [10809, 10808, 12334, 2080]

# Ссылка должна быть http(s). Всё остальное — в том числе file:// — отвергаем:
# служба слушает только себя, и давать ей читать произвольные пути по «ссылке»
# незачем.
_URL_RE = re.compile(r"^https?://", re.I)


def version() -> str:
    """Версия установленного yt-dlp. Пусто — библиотеки нет."""
    try:
        import yt_dlp

        return str(getattr(yt_dlp.version, "__version__", "") or "")
    except Exception:
        return ""


def pip_install(packages: list[str], note: Callable[[str], Any] | None = None,
                upgrade: bool = False) -> None:
    """Поставить библиотеку из PyPI pip-ом, но БИБЛИОТЕКОЙ, в своём процессе.

    Kaspersky не даёт программе порождать фоновые процессы, поэтому «python -m
    pip» здесь не годится (запуск — lockfile.pip_run). Пакет ложится в .venv,
    а не в папку python\\, из которой программа запущена. После установки
    убираем лишние лаунчеры .exe, как после любой установки (грабли 13.09).
    """
    from . import lockfile

    def say(msg: str) -> None:
        log.info("pip: %s", msg)
        if note is not None:
            try:
                note(msg)
            except Exception:
                pass

    args = ["install"] + (["--upgrade"] if upgrade else []) + [
        "--no-input", "--disable-pip-version-check", "--no-warn-script-location",
        "--prefix", str(config.PROJECT_DIR / ".venv")]
    code, tail = lockfile.pip_run(args + list(packages))
    if code != 0:
        raise FetchError("Поставить не вышло (pip вернул %s). Обычно это нет доступа к "
                         "интернету или его закрывает корпоративная сеть. %s"
                         % (code, " | ".join(tail[-3:])))
    dropped = lockfile.drop_new_launchers(config.PROJECT_DIR)
    if dropped:
        say("убраны лишние лаунчеры: %s" % ", ".join(dropped))


def update(note: Callable[[str], Any] | None = None) -> dict[str, Any]:
    """Обновить yt-dlp из PyPI. Возвращает {"before", "after", "changed"}.

    pip зовём БИБЛИОТЕКОЙ, в своём процессе: Kaspersky не даёт программе
    порождать фоновые процессы, и «python -m pip» в корпоративной сети просто
    не запустился бы. После установки чистим лишние лаунчеры .exe и выкидываем
    старый yt_dlp из памяти, чтобы следующая ссылка пошла уже через новый.
    """
    import sys

    def say(msg: str) -> None:
        log.info("yt-dlp: %s", msg)
        if note is not None:
            try:
                note(msg)
            except Exception:
                pass

    before = version()
    say("обновляю, сейчас %s" % (before or "не установлен"))
    try:
        pip_install(["yt-dlp"], note=note, upgrade=True)
    except FetchError as err:
        raise FetchError(str(err).replace("Поставить не вышло", "Обновить не вышло")) from err
    for name in [n for n in list(sys.modules) if n == "yt_dlp" or n.startswith("yt_dlp.")]:
        sys.modules.pop(name, None)
    after = version()
    say("готово: %s" % (after or "версия не определилась"))
    return {"before": before, "after": after, "changed": bool(after and after != before)}


class FetchError(RuntimeError):
    """Понятная пользователю ошибка скачивания."""


def looks_like_url(text: str) -> bool:
    return bool(_URL_RE.match((text or "").strip()))


def _human_error(err: Exception) -> str:
    """Перевести ошибку yt-dlp на человеческий язык."""
    raw = str(err)
    low = raw.lower()
    if "private" in low or "members-only" in low:
        return ("Видео закрытое — его нельзя скачать без входа в аккаунт. "
                "Скачайте файл вручную и перетащите его в окно.")
    if "age" in low and "restrict" in low:
        return ("Видео с возрастным ограничением: площадка не отдаёт его без "
                "входа в аккаунт. Скачайте файл вручную и перетащите в окно.")
    if "unavailable" in low or "removed" in low:
        return "Видео недоступно или удалено."
    if "sign in" in low or "cookies" in low or "bot" in low:
        return ("Площадка требует войти в аккаунт и подтвердить, что вы не робот. "
                "Скачайте файл вручную и перетащите его в окно.")
    if "unsupported url" in low:
        return "Не понимаю эту ссылку — площадка не поддерживается."
    if "network" in low or "timed out" in low or "connection" in low:
        return "Не получилось связаться с площадкой. Проверьте интернет."
    return "Не удалось скачать: " + raw[:300]


def proxy_candidates(raw: str | None = None) -> list[str | None]:
    """Прокси для перебора. None означает «напрямую».

    Пусто в настройке — только напрямую. «auto» — типовые порты VPN-клиентов
    плюс напрямую. Иначе список адресов через запятую, как их написал человек.
    """
    text = str(raw if raw is not None else (config.get("yt_proxy") or "")).strip()
    if not text:
        return [None]
    if text.lower() == "auto":
        return [None] + ["socks5://127.0.0.1:%d" % p for p in _VPN_PORTS]
    out: list[str | None] = []
    for part in text.split(","):
        part = part.strip()
        if part:
            out.append(part)
    return out or [None]


def _info_via(url: str, proxy: str | None, extra: dict[str, Any] | None = None) -> Any:
    """Прочитать описание ссылки через конкретный прокси. Бросает как есть."""
    import yt_dlp

    opts: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "skip_download": True,
        "socket_timeout": 20,
    }
    if proxy:
        opts["proxy"] = proxy
    opts.update(extra or {})
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=False)


def _flatten(info: Any) -> dict[str, Any]:
    if isinstance(info, dict) and info.get("_type") == "playlist":
        entries = [e for e in (info.get("entries") or []) if e]
        if not entries:
            raise FetchError("По ссылке не нашлось ни одного видео.")
        return entries[0]
    return info or {}


def _sub_langs(info: dict[str, Any]) -> dict[str, list[str]]:
    """Какие субтитры есть у ролика: авторские и автоматические, по языкам."""
    def langs(raw: Any) -> list[str]:
        return sorted(str(k) for k in (raw or {}).keys()) if isinstance(raw, dict) else []
    return {
        "manual": langs(info.get("subtitles")),
        "auto": langs(info.get("automatic_captions")),
    }


def _heights(info: dict[str, Any]) -> list[int]:
    out = set()
    for f in info.get("formats") or []:
        h = f.get("height")
        if isinstance(h, int) and h > 0:
            out.add(h)
    return sorted(out, reverse=True)


def probe(url: str, proxy: str | None = None) -> dict[str, Any]:
    """Узнать название, длительность, субтитры и качества, ничего не скачивая.

    Рабочий прокси выбирается по тому, через какой вообще прочиталось название:
    проверять доступность иначе нечем, а перебор занимает секунды.
    """
    url = (url or "").strip()
    if not looks_like_url(url):
        raise FetchError("Это не похоже на ссылку. Нужен адрес, начинающийся с http.")
    try:
        import yt_dlp  # noqa: F401
    except Exception as err:
        raise FetchError("Не установлен yt-dlp: %s" % err) from err

    tries = [proxy] if proxy is not None else proxy_candidates()
    last: Exception | None = None
    for candidate in tries:
        try:
            info = _flatten(_info_via(url, candidate))
        except FetchError:
            raise
        except Exception as err:
            last = err
            if candidate is not None:
                log.info("через прокси %s не вышло: %s", candidate, str(err)[:120])
            continue
        subs = _sub_langs(info)
        if candidate:
            log.info("ссылка читается через прокси %s", candidate)
        return {
            "title": (info.get("title") or "").strip() or "Видео по ссылке",
            "duration_s": float(info.get("duration") or 0.0),
            "uploader": (info.get("uploader") or "").strip(),
            "webpage_url": info.get("webpage_url") or url,
            "proxy": candidate or "",
            "subs_manual": subs["manual"],
            "subs_auto": subs["auto"],
            "heights": _heights(info),
        }
    log.info("не удалось прочитать ссылку: %s", last)
    raise FetchError(_human_error(last or RuntimeError("ссылка не читается")))


def _picked_path(info: dict[str, Any]) -> Path | None:
    for cand in (info.get("requested_downloads") or []):
        if cand.get("filepath"):
            return Path(cand["filepath"])
    got = info.get("_filename")
    return Path(got) if got else None


def download_subs(url: str, dest_dir: Path, auto_ok: bool = True,
                  proxy: str | None = None) -> dict[str, Any]:
    """Скачать готовые субтитры. Возвращает {"path", "lang", "kind"} либо пустое.

    Два прохода по убыванию качества: сначала авторские (человек их писал или
    правил), потом автоматические (распознаны площадкой — без пунктуации и с
    ошибками в терминах, но всё равно лучше, чем платить за распознавание).
    """
    import yt_dlp

    dest_dir.mkdir(parents=True, exist_ok=True)
    base = {
        "quiet": True, "no_warnings": True, "noplaylist": True,
        "skip_download": True, "socket_timeout": 30,
        "outtmpl": str(dest_dir / "subs.%(ext)s"),
        "subtitlesformat": "vtt",
        "ffmpeg_location": str(Path(audio_io.ffmpeg_exe()).parent),
    }
    if proxy:
        base["proxy"] = proxy

    passes = [("manual", SUB_LANGS_MANUAL, {"writesubtitles": True,
                                            "writeautomaticsub": False})]
    if auto_ok:
        passes.append(("auto", SUB_LANGS_AUTO, {"writesubtitles": False,
                                                "writeautomaticsub": True}))
    for kind, langs, flags in passes:
        # Остатки прошлой попытки убираем: иначе старый файл сошёл бы за
        # только что скачанные «авторские» субтитры.
        for stale in list(dest_dir.glob("subs*.vtt")) + list(dest_dir.glob("subs*.srt")):
            try:
                stale.unlink()
            except OSError:
                pass
        opts = dict(base, subtitleslangs=list(langs), **flags)
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.extract_info(url, download=True)
        except Exception as err:
            log.info("субтитры (%s) не скачались: %s", kind, str(err)[:150])
            continue
        found = sorted(dest_dir.glob("subs*.vtt")) + sorted(dest_dir.glob("subs*.srt"))
        found = [f for f in found if f.stat().st_size > 32]
        # yt-dlp качает ВСЕ перечисленные языки; берём по порядку предпочтения
        # (русский раньше английского), а не первый по алфавиту (находка 13).
        by_lang = {f.stem.split(".")[-1]: f for f in reversed(found) if "." in f.stem}
        found = [by_lang[lg] for lg in langs if lg in by_lang] + [f for f in found if f not in by_lang.values()]
        if found:
            path = found[0]
            lang = path.stem.split(".")[-1] if "." in path.stem else ""
            log.info("взяты субтитры: %s (%s)", path.name, kind)
            return {"path": path, "lang": lang, "kind": kind}
    return {}


def download_media(
    url: str,
    dest_dir: Path,
    mode: str = "audio",
    max_height: int | None = None,
    proxy: str | None = None,
    progress: Callable[[float, str], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Скачать ссылку. mode: audio — только звук, video — видео со звуком.

    Видео качается ОДИН раз: звук для распознавания потом вынимается из этого
    же файла локально, второй заход на площадку не нужен.
    """
    url = (url or "").strip()
    if not looks_like_url(url):
        raise FetchError("Это не похоже на ссылку. Нужен адрес, начинающийся с http.")
    try:
        import yt_dlp
    except Exception as err:
        raise FetchError("Не установлен yt-dlp: %s" % err) from err

    dest_dir.mkdir(parents=True, exist_ok=True)
    height = int(max_height or config.get("max_height") or 720)

    def hook(d: dict[str, Any]) -> None:
        # Отмена пробрасывается исключением: у yt-dlp нет другого способа
        # остановить скачивание изнутри.
        if cancelled is not None and cancelled():
            raise FetchError("Скачивание отменено.")
        if progress is None:
            return
        if d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            done = d.get("downloaded_bytes") or 0
            frac = (done / total) if total else 0.0
            what = "видео" if (d.get("info_dict") or {}).get("vcodec") not in (None, "none") \
                else "звук"
            try:
                progress(min(0.99, float(frac)), "скачиваю %s: %.0f%%" % (what, frac * 100))
            except Exception:
                pass
        elif d.get("status") == "finished":
            try:
                progress(1.0, "скачано, собираю файл")
            except Exception:
                pass

    if mode == "video":
        fmt = "bv*[height<=%d]+ba/b[height<=%d]/bv*+ba/b" % (height, height)
    else:
        fmt = "bestaudio/best"

    opts: dict[str, Any] = {
        "format": fmt,
        "outtmpl": str(dest_dir / "%(title).80B [%(id)s].%(ext)s"),
        "noplaylist": True,        # одна ссылка — один ролик, а не весь плейлист
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "progress_hooks": [hook],
        "ffmpeg_location": str(Path(audio_io.ffmpeg_exe()).parent),
        "retries": 10,             # обрывы сети на длинном видео — обычное дело
        "fragment_retries": 10,
        "socket_timeout": 30,
    }
    if mode == "video":
        opts["merge_output_format"] = "mp4"
    if proxy:
        opts["proxy"] = proxy

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = _flatten(ydl.extract_info(url, download=True))
    except FetchError:
        raise
    except Exception as err:
        log.info("скачивание не удалось: %s", err)
        raise FetchError(_human_error(err)) from err

    path = _picked_path(info)
    if path is None or not path.exists():
        raise FetchError("Файл скачался, но не найден на диске — попробуйте ещё раз.")

    return {
        "path": path,
        "mode": mode,
        "title": (info.get("title") or "").strip() or "Видео по ссылке",
        "duration_s": float(info.get("duration") or 0.0),
        "uploader": (info.get("uploader") or "").strip(),
        "webpage_url": info.get("webpage_url") or url,
        "height": info.get("height") or 0,
    }
