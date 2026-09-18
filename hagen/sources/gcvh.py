# -*- coding: utf-8 -*-
"""Источник «GetCourse»: страница курса -> плеер -> HLS-поток -> mp4 в папке записи.

Путь до видео тут длинный, и каждый шаг зависит от чужой вёрстки:

    страница курса  --data-iframe-src-->  плеер (подписанная ссылка)
    плеер           --masterPlaylistUrl-->  мастер-плейлист HLS
    мастер-плейлист --список потоков-->   лучший поток в пределах max_height
    поток           --ffmpeg-->           video.mp4

Поэтому на каждом шаге своя понятная ошибка: человек должен видеть, что
сломалось не у него, а у площадки (или что видео требует входа в аккаунт).

Три вещи, которые здесь выглядят странно, но сделаны намеренно.

1. Хост потока подменяется на api1.gcvh.ru. Мастер-плейлист отдаёт ссылки с
   «управлением раздачей»: клиента уводят на зарубежные зеркала (gceuproxy,
   integros), а они из России не открываются — скачивание встаёт на первом же
   куске. Поэтому берём медиа-плейлист российской CDN и дополнительно
   прибиваем хост к api1.gcvh.ru.
2. Звук чинится фильтром aac_adtstoasc. В HLS звук лежит кусками ADTS, у
   которых заголовок повторяется перед каждым кадром, а контейнеру mp4 нужен
   один заголовок в начале дорожки. Без фильтра файл копируется «как есть» и
   получается mp4 с битым (обычно немым) звуком — для расшифровки это провал.
3. Ходим НАПРЯМУЮ, мимо системного прокси. Бывает включён локальный
   VPN, и тогда российский трафик уходит за границу: TLS к .ru рвётся, а сама
   площадка режет заграничные адреса. Отсюда trust_env=False у httpx и
   окружение без http_proxy для дочернего ffmpeg (см. _direct_env).

Модуль отдаёт словарь общего договора источников (см. fetch()).
"""
from __future__ import annotations

import html
import logging
import os
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .. import audio_io, config, platform
from ..obsidian import _clean_url

log = logging.getLogger("hagen.sources.gcvh")

#: Короткое имя источника для интерфейса.
SOURCE_NAME = "getcourse"

#: Представляемся обычным браузером: без User-Agent плеер отдаёт пустой ответ.
UA = "Mozilla/5.0"

#: Российская точка раздачи, к которой прибивается поток (причина — в шапке).
RU_ORIGIN = "api1.gcvh.ru"

_HTTP_RE = re.compile(r"^https?://", re.I)

# Плеер вшит в страницу атрибутом data-iframe-src, ссылка внутри — с &amp;.
_PLAYER_RE = re.compile(r'data-iframe-src="([^"]+)"')
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
_MASTER_RE = re.compile(r'masterPlaylistUrl"\s*:\s*"([^"]+)"')
_MEDIA_RE = re.compile(r'https?://[^\s"\'<>]+/api/playlist/media/[^\s"\'<>]+')
# Высота потока зашита в путь: /media/<id>/<что-то>/720?...
_HEIGHT_RE = re.compile(r"/media/[^/]+/[^/]+/(\d+)\?")
_EXTINF_RE = re.compile(r"^#EXTINF:\s*([\d.]+)", re.M)

# Строка отчёта ffmpeg (-progress) начинается с «ключ=». Проверяем только начало
# строки, и цифры в ключе разрешаем: иначе «stream_0_0_q=-1.0» и «bitrate= 85.7
# kbits/s» (ffmpeg выравнивает число пробелами) попадали в жалобы, и настоящая
# причина отказа вытеснялась из хвоста этим мусором каждые полсекунды.
# Искать «=» где угодно в строке нельзя: в ругани про отказ сервера «=» тоже
# есть — внутри ссылки, — и самые важные строки молча терялись бы.
_PROGRESS_LINE = re.compile(r"^[a-z][a-z0-9_]*=")

# Ссылки в сообщениях ffmpeg и сервера несут подпись доступа: в лог и в текст
# ошибки пускаем только имя хоста. Ссылка со всеми параметрами — это, по сути,
# ключ от записи, ей не место ни в логе, ни в meta.json.
_URL_IN_TEXT = re.compile(r"https?://([^\s'\"<>]+)")

#: Готовое видео меньше этого размера считаем неудачей, а не роликом.
_MIN_BYTES = 1024

#: Файл такого размера в папке — уже скачанное видео, второй раз не тянем.
_REUSE_BYTES = 1_000_000


class GcvhError(RuntimeError):
    """Понятная человеку ошибка при работе с GetCourse."""


def matches(url: str) -> bool:
    """Точно ли ссылка ведёт на GetCourse.

    Проверка нарочно узкая. Курсы живут на собственных доменах клиентов
    (cybermisha.ru и подобные), и по адресу их не отличить от любого другого
    сайта — такую ссылку человек выбирает этим источником вручную.
    """
    host = urlsplit((url or "").strip()).netloc.lower().split(":")[0]
    return bool(host) and (
        host == "gcvh.ru" or host.endswith(".gcvh.ru")
        or host.endswith(".getcourse.ru") or host.endswith(".getcourse.io")
    )


# --------------------------------------------------------------- мелочи
def _scrub(text: Any) -> str:
    """Текст без подписанных ссылок: от них остаётся только хост."""
    return _URL_IN_TEXT.sub(lambda m: "https://" + m.group(1).split("/")[0] + "/…",
                            str(text or ""))


def _origin(url: str) -> str:
    parts = urlsplit(url)
    return "%s://%s" % (parts.scheme, parts.netloc)


def _slug(url: str) -> str:
    """Последний кусок адреса — запасное название, если на странице его нет."""
    tail = url.split("#")[0].split("?")[0].rstrip("/").split("/")[-1]
    return tail or "Видео GetCourse"


def _hms(seconds: float) -> str:
    total = int(max(0.0, seconds))
    return "%d:%02d:%02d" % (total // 3600, (total % 3600) // 60, total % 60)


def _check(handle: Any) -> None:
    """Прерваться, если человек нажал «отмена»."""
    if handle is not None and getattr(handle, "cancelled", False):
        raise RuntimeError("отменено")


def _say(handle: Any, frac: float, note: str) -> None:
    if handle is None:
        return
    try:
        handle.progress(frac, note)
    except Exception:
        log.debug("не удалось показать прогресс", exc_info=True)


def _tell(handle: Any, line: str) -> None:
    if handle is None:
        log.info("%s", line)
        return
    try:
        handle.log(line)
    except Exception:
        log.info("%s", line)


def _direct_env() -> dict[str, str]:
    """Окружение для дочернего ffmpeg — без прокси.

    Локальный VPN-клиент выставляет http_proxy/https_proxy на весь компьютер, и
    ffmpeg их послушно подхватывает: запрос к российской раздаче уходит за
    границу, TLS рвётся, скачивание встаёт. Свои запросы мы уже шлём напрямую
    (trust_env=False), ребёнок должен вести себя так же.
    """
    env = dict(os.environ)
    for key in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY",
                "ALL_PROXY", "all_proxy"):
        env.pop(key, None)
    env["NO_PROXY"] = env["no_proxy"] = "*"
    return env


# --------------------------------------------------------------- сеть
def _open_client():
    """Клиент httpx: напрямую, с раздельными таймаутами и переходами по редиректам."""
    try:
        import httpx
    except ImportError:
        raise GcvhError(
            "Для скачивания с GetCourse нужен пакет httpx, а он не установлен."
        ) from None

    # connect короткий: если до сайта не достучаться, лучше сказать об этом
    # сразу. read длинный: страница курса и плеер отвечают неторопливо.
    timeout = httpx.Timeout(30.0, connect=10.0, read=60.0, write=30.0)
    # follow_redirects обязателен: страница курса и плеер отвечают 302
    # (httpx, в отличие от привычных библиотек, сам по редиректу не идёт).
    return httpx.Client(
        trust_env=False,
        follow_redirects=True,
        timeout=timeout,
        headers={"User-Agent": UA, "Accept-Language": "ru-RU,ru;q=0.9"},
    )


def _get(client, url: str, referer: str | None, what: str, handle: Any = None) -> str:
    """Прочитать страницу с тремя попытками. Возвращает текст ответа.

    Повторы нужны не из вежливости: площадка регулярно отдаёт пустой ответ на
    первый запрос (особенно плееру), а со второй-третьей попытки всё приходит.
    Признак пустышки — подозрительно короткое тело.
    """
    headers = {"Referer": referer} if referer else {}
    last_text = ""
    last_err: Exception | None = None
    for attempt in (1, 2, 3):
        _check(handle)
        try:
            resp = client.get(url, headers=headers)
        except Exception as err:                     # httpx.*Error, имена не тянем
            last_err = err
            log.info("%s: попытка %d не удалась (%s)", what, attempt,
                     _scrub(type(err).__name__ + ": " + str(err))[:200])
        else:
            if resp.status_code in (401, 403):
                raise GcvhError(
                    "Площадка не пустила: чтобы скачать это видео, нужно войти в "
                    "аккаунт (%s, код %d)." % (what, resp.status_code)
                )
            if resp.status_code == 404:
                raise GcvhError("Не нашёл %s по этой ссылке (код 404). "
                                "Проверьте адрес страницы." % what)
            if resp.status_code >= 400:
                last_err = GcvhError("код %d" % resp.status_code)
                log.info("%s: сервер ответил кодом %d", what, resp.status_code)
            else:
                last_text = resp.text or ""
                if len(last_text) > 200:
                    return last_text
                log.info("%s: ответ подозрительно короткий (%d символов), повторяю",
                         what, len(last_text))
        if attempt < 3:
            time.sleep(2.0)
    if last_text:
        return last_text                              # пусть разбирают дальше
    raise GcvhError(
        "Не смог прочитать %s: площадка не отвечает или её закрыл VPN. "
        "Проверьте интернет и попробуйте ещё раз. (%s)"
        % (what, _scrub(last_err)[:200] if last_err else "пустой ответ")
    )


# --------------------------------------------------------------- разбор вёрстки
def _find_player(page: str, page_url: str) -> str:
    """Ссылка на плеер со страницы курса."""
    found = _PLAYER_RE.search(page)
    if not found:
        raise GcvhError(
            "Не нашёл плеер на странице. Либо это не страница с видео, либо "
            "видео открывается только после входа в аккаунт, либо площадка "
            "поменяла вёрстку."
        )
    src = html.unescape(found.group(1)).strip()
    if src.startswith("//"):                      # ссылка без схемы — берём схему страницы
        src = urlsplit(page_url).scheme + ":" + src
    if not _HTTP_RE.match(src):
        raise GcvhError("Ссылка на плеер выглядит непонятно — площадка поменяла вёрстку.")
    return src


def _find_title(page: str, fallback: str) -> str:
    found = _TITLE_RE.search(page)
    if not found:
        return fallback
    text = html.unescape(re.sub(r"\s+", " ", found.group(1))).strip()
    return text or fallback


def _find_master(player: str) -> str:
    """Ссылка на мастер-плейлист из кода плеера."""
    found = _MASTER_RE.search(player)
    if not found:
        raise GcvhError(
            "Плеер не отдал ссылку на видео. Обычно так бывает, когда запись "
            "доступна только после входа в аккаунт."
        )
    # В JSON плеера слэши экранированы: "https:\/\/...".
    url = found.group(1).replace("\\/", "/").strip()
    if not _HTTP_RE.match(url):
        raise GcvhError("Плеер отдал непонятную ссылку на видео — вёрстка изменилась.")
    return url


def _pick_stream(master: str, max_height: int) -> tuple[int, str]:
    """Выбрать поток из мастер-плейлиста. Возвращает (высота, ссылка)."""
    urls = _MEDIA_RE.findall(master)
    if not urls:
        raise GcvhError(
            "Не нашёл список качеств видео. Площадка поменяла формат плейлиста "
            "или отдала вместо него страницу с ошибкой."
        )
    # Сначала российская раздача: зарубежные зеркала из России не открываются.
    cdn = [u for u in urls if "user-cdn=cdnvideo" in u] or urls

    variants: list[tuple[int, str]] = []
    for url in cdn:
        found = _HEIGHT_RE.search(url)
        variants.append((int(found.group(1)) if found else 0, url))

    known = [v for v in variants if v[0] > 0]
    if not known:
        # Высоту из адреса вытащить не вышло — не повод сдаваться: берём первый
        # поток и честно говорим, что качество неизвестно.
        log.info("высота потоков не читается, беру первый из %d", len(variants))
        return 0, variants[0][1]

    fitting = [v for v in known if v[0] <= max_height]
    if fitting:
        return max(fitting, key=lambda v: v[0])
    # Все потоки выше ограничения (бывает, когда есть только 1080p): качаем
    # самый лёгкий, а не отказываемся от видео вовсе.
    best = min(known, key=lambda v: v[0])
    log.info("все потоки выше ограничения %dp, беру самый лёгкий: %dp", max_height, best[0])
    return best


def _force_ru_origin(url: str) -> str:
    """Прибить поток к российской раздаче (причина — в шапке модуля)."""
    return re.sub(r"://[^/]+/", "://%s/" % RU_ORIGIN, url, count=1)


def _stream_duration(client, url: str, referer: str, handle: Any = None) -> float:
    """Длительность видео как сумма кусков медиа-плейлиста.

    Нужна ради двух вещей: показать человеку честный процент скачивания и
    положить длительность в запись. Если плейлист не прочитался — не беда,
    просто вернём 0.
    """
    try:
        body = _get(client, url, referer, "список кусков видео", handle)
    except GcvhError:
        return 0.0
    parts = [float(x) for x in _EXTINF_RE.findall(body)]
    return round(sum(parts), 2) if parts else 0.0


# --------------------------------------------------------------- скачивание
def _ffmpeg_cmd(stream: str, referer: str, dst: Path, picky: bool) -> list[str]:
    cmd = [
        audio_io.ffmpeg_exe(), "-hide_banner", "-loglevel", "warning", "-nostdin",
        "-f", "hls",
    ]
    if picky:
        # Куски потока называются *.bin, а свежий ffmpeg по умолчанию пускает
        # только знакомые расширения и молча обрывает загрузку.
        cmd += ["-extension_picky", "0"]
    else:
        cmd += ["-allowed_extensions", "ALL"]     # то же самое для старых сборок
    cmd += [
        "-protocol_whitelist", "file,http,https,tcp,tls,crypto",
        # Без User-Agent и Referer раздача отвечает отказом: она проверяет,
        # что запрос пришёл «со страницы курса», а не сам по себе.
        "-user_agent", UA, "-referer", referer,
        # Раздача любит рвать соединение на середине длинного видео: без
        # реконнектов двухчасовая запись почти никогда не докачивалась.
        "-reconnect", "1", "-reconnect_streamed", "1",
        "-reconnect_on_network_error", "1", "-reconnect_delay_max", "5",
        "-i", stream,
        # Перекодировать нечего: поток уже h264+aac, копируем как есть.
        # aac_adtstoasc чинит звук при перекладывании в mp4 (см. шапку).
        "-c", "copy", "-bsf:a", "aac_adtstoasc",
        "-progress", "pipe:1", "-nostats",
        "-y", str(dst),
    ]
    return cmd


def _out_time_s(line: str) -> float | None:
    """Отсчёт времени из строки отчёта ffmpeg.

    out_time_us — микросекунды. out_time_ms, вопреки имени, тоже микросекунды:
    старая ошибка ffmpeg, из-за которой прогресс когда-то улетал в тысячу раз.
    """
    for key in ("out_time_us=", "out_time_ms="):
        if line.startswith(key):
            raw = line[len(key):].strip()
            try:
                return max(0.0, float(raw) / 1_000_000.0)
            except ValueError:
                return None
    return None


def _download(stream: str, referer: str, dst: Path, total_s: float,
              handle: Any = None) -> None:
    """Скачать HLS-поток в dst. Прогресс и отмена — через handle."""
    picky = True
    for attempt in (1, 2):
        tail = _run_ffmpeg(_ffmpeg_cmd(stream, referer, dst, picky), dst, total_s, handle)
        if tail is None:
            return
        # Старый ffmpeg из системы не знает про -extension_picky. Пробуем ещё
        # раз его же словами; на своей сборке этой ветки не бывает.
        if attempt == 1 and picky and "extension_picky" in tail:
            log.info("ffmpeg не знает -extension_picky, повторяю по-старому")
            picky = False
            continue
        raise GcvhError(
            "Поток не отдаётся: видео скачать не удалось. Обычно это значит, что "
            "ссылка на запись протухла (откройте страницу курса заново) или что "
            "раздача недоступна из-за VPN. Ответ ffmpeg: %s" % (tail or "без объяснений")
        )


def _run_ffmpeg(cmd: list[str], dst: Path, total_s: float, handle: Any) -> str | None:
    """Запустить ffmpeg. None — получилось, иначе последние строки его ругани."""
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        # Отчёт о прогрессе и ругань читаем одной трубой: с двумя пришлось бы
        # заводить второй поток, иначе ffmpeg однажды встанет на полном буфере.
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        text=True, encoding="utf-8", errors="replace", bufsize=1,
        # Без окна консоли: иначе на каждый вызов мигает чёрное окно.
        creationflags=platform.system().hidden_process_flags(),
        env=_direct_env(),
    )
    complaints: list[str] = []
    last_note = [0.0]

    def pump() -> None:
        # Вывод читаем отдельным потоком: чтение из трубы блокируется без
        # таймаута, и пока ffmpeg молчал на зависшем соединении, «Отмена» не
        # действовала. Так же сделано в webauth.
        try:
            for raw in proc.stdout or ():
                line = raw.strip()
                if not line:
                    continue
                done = _out_time_s(line)
                if done is not None:
                    now = time.time()
                    if now - last_note[0] > 1.0:      # интерфейс не любит частую капель
                        last_note[0] = now
                        _report(handle, done, total_s)
                elif not _PROGRESS_LINE.match(line):
                    complaints.append(_scrub(line))   # подписи из ссылок наружу не пускаем
                    del complaints[:-6]
        except Exception:
            pass

    reader = threading.Thread(target=pump, name="gcvh-ffmpeg", daemon=True)
    reader.start()
    try:
        while proc.poll() is None:
            if handle is not None and getattr(handle, "cancelled", False):
                _stop(proc)
                _drop(dst)
                raise RuntimeError("отменено")
            time.sleep(0.4)
        reader.join(timeout=5)
        code = proc.returncode if proc.returncode is not None else -1
    finally:
        if proc.poll() is None:
            _stop(proc)

    if code != 0 or not dst.exists() or dst.stat().st_size < _MIN_BYTES:
        _drop(dst)
        return " | ".join(complaints[-3:]) or ("код выхода %s" % code)
    return None


def _report(handle: Any, done_s: float, total_s: float) -> None:
    if total_s > 0:
        frac = min(1.0, done_s / total_s)
        _say(handle, 0.2 + 0.78 * frac,
             "скачиваю видео: %d%% (%s из %s)"
             % (frac * 100, _hms(done_s), _hms(total_s)))
    else:
        # Длительность неизвестна — ползём к 0.9, не обещая скорого конца.
        _say(handle, 0.2 + 0.7 * (done_s / (done_s + 900.0)),
             "скачиваю видео: готово %s" % _hms(done_s))


def _stop(proc: subprocess.Popen) -> None:
    try:
        proc.terminate()
        proc.wait(timeout=10)
    except Exception:
        try:
            proc.kill()
        except Exception:
            log.debug("ffmpeg не удалось остановить", exc_info=True)


def _drop(path: Path) -> None:
    """Убрать недокачанный файл.

    Оборванный mp4 не открывается вовсе: оглавление пишется в самом конце. Хуже
    того, при следующем запуске такой огрызок сошёл бы за готовое видео.
    """
    try:
        if path.exists():
            path.unlink()
    except OSError:
        log.debug("не удалось убрать недокачанный файл %s", path.name, exc_info=True)


# --------------------------------------------------------------- договор источника
def fetch(url: str, dst_dir: Path, handle: Any = None) -> dict[str, Any]:
    """Скачать видео GetCourse по ссылке на страницу курса.

    Возвращает словарь общего договора источников: media, transcript, title,
    url, source_name, duration_s, extra.
    """
    page_url = (url or "").strip()
    if not _HTTP_RE.match(page_url):
        raise GcvhError("Это не похоже на ссылку. Нужен адрес, начинающийся с http.")

    dst_dir = Path(dst_dir)
    dst_dir.mkdir(parents=True, exist_ok=True)
    video = dst_dir / "video.mp4"
    origin = _origin(page_url)
    referer = origin + "/"

    _check(handle)
    _say(handle, 0.02, "открываю страницу курса")
    with _open_client() as client:
        page = _get(client, page_url, None, "страницу курса", handle)
        player_url = _find_player(page, page_url)
        title = _find_title(page, _slug(page_url))
        _tell(handle, "Нашёл видео: %s" % title)

        _check(handle)
        _say(handle, 0.08, "открываю плеер")
        player = _get(client, player_url, referer, "плеер", handle)
        master_url = _find_master(player)

        _check(handle)
        _say(handle, 0.12, "выбираю качество")
        master = _get(client, master_url, referer, "список качеств", handle)
        max_height = _max_height()
        height, stream_url = _pick_stream(master, max_height)
        stream_url = _force_ru_origin(stream_url)
        _tell(handle, "Качество: %s (потолок настройки — %dp)"
              % ("%dp" % height if height else "неизвестно", max_height))

        _check(handle)
        duration_s = _stream_duration(client, stream_url, referer, handle)

    _check(handle)
    if video.exists() and video.stat().st_size > _REUSE_BYTES:
        # Повторный запуск по той же ссылке не качает заново: видео тяжёлое, а
        # ссылка на страницу курса обычно одна и та же.
        _tell(handle, "Видео уже скачано (%.1f МБ), качать заново не буду"
              % (video.stat().st_size / 1_048_576))
    else:
        _say(handle, 0.2, "скачиваю видео")
        # Пишем во временное имя и переименовываем в конце: так недокачанный
        # файл никогда не притворится готовым видео.
        part = dst_dir / "video.download.mp4"
        _drop(part)
        _download(stream_url, referer, part, duration_s, handle)
        part.replace(video)                      # replace затирает старое одним движением
        _tell(handle, "Видео скачано: %.1f МБ" % (video.stat().st_size / 1_048_576))

    _say(handle, 1.0, "видео готово")
    log.info("GetCourse: скачано «%s», %s, %s", title,
             "%dp" % height if height else "качество неизвестно", _hms(duration_s))
    return {
        "media": str(video),
        "transcript": "",                 # готовых субтитров площадка не даёт
        "title": title,
        "url": _clean_url(page_url),
        "source_name": SOURCE_NAME,
        "duration_s": float(duration_s),
        "extra": {
            # Только безличные приметы: подписанные ссылки в meta.json не кладём.
            "height": int(height),
            "max_height": int(max_height),
            "stream_host": RU_ORIGIN,
            "page_host": urlsplit(page_url).netloc,
            "size_bytes": video.stat().st_size,
        },
    }


def _max_height() -> int:
    """Потолок качества из настроек приложения."""
    try:
        value = int(config.get("max_height") or 720)
    except (TypeError, ValueError):
        value = 720
    return value if value > 0 else 720
