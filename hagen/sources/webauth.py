# -*- coding: utf-8 -*-
"""Сайты со входом по логину и паролю (Facecast и подобные).

Площадка показывает запись только тому, кто ввёл логин и пароль прямо на её
странице, а сам поток живёт по подписанной ссылке `.m3u8`, которую плеер
получает уже после входа. В html этой ссылки нет — она появляется только в
сетевых запросах страницы, поэтому обычным скачиванием такое не берётся.

Работающий путь один: открыть настоящий браузер (Playwright поднимает
Chromium), ввести пароль, дождаться плеера и подслушать, за каким адресом он
пошёл. Дальше поток забирает наш ffmpeg — с теми же cookies, User-Agent и
Referer, что были у браузера; без них площадка отвечает 403.

Три вещи, на которых уже наступали и которые менять нельзя:

* Окно браузера ВИДИМОЕ. В headless-режиме Chromium узнают по отпечатку и
  просто рвут соединение — до формы входа дело даже не доходит.
* Браузер и ffmpeg ходят напрямую, без прокси. Бывает включён
  локальный VPN-клиент, который прописывает http_proxy в окружение: через
  него такие площадки недоступны, а ключи шифрования потока не отдаются.
* На пачку ссылок открывается ОДНО окно. Пароль вводится один раз, дальше
  cookies работают на все ссылки, и человеку не приходится смотреть, как
  окна открываются и закрываются на каждую запись.

Playwright — единственное исключение из правила «никаких лишних программ в
проекте», он разрешён отдельно. Пакет может быть не установлен, и
служба обязана спокойно сказать об этом, а не упасть: импорт playwright живёт
внутри функций, а `available()` отвечает на вопрос «готов ли источник» не
запуская браузер.

Пароль приходит параметром, живёт только в памяти и не попадает ни в журнал,
ни на диск — даже в отладочных сообщениях. Подписанные ссылки и cookies перед
выводом чистятся: журнал можно кому-нибудь переслать.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .. import audio_io, platform
from ..obsidian import _clean_url

log = logging.getLogger("hagen.webauth")

#: Короткое имя источника — его видно в интерфейсе.
SOURCE_NAME = "webauth"


# Один и тот же User-Agent у браузера и у ffmpeg. Площадка привязывает выданную
# ссылку на поток к тому, кто её запросил: с другим UA приходит 403.
UA = "Mozilla/5.0"

# Сколько раз пробуем открыть страницу: первое соединение такие площадки рвут
# постоянно, со второй-третьей попытки открывается та же самая ссылка.
GOTO_TRIES = 5
GOTO_TIMEOUT_MS = 45_000

#: Сколько секунд ждём появления формы входа и запуска плеера.
LOGIN_WAIT_S = 40
STREAM_WAIT_S = 90

#: Доля прогресса, которая приходится на браузер; остальное — скачивание.
CAPTURE_SHARE = 0.35

#: Меньше мегабайта — это не запись, а огрызок: площадка отдала заглушку.
MIN_VIDEO_BYTES = 1_000_000

PLAYWRIGHT_MISSING = (
    "Вход по логину и паролю пока не работает: не установлен Playwright — это он "
    "открывает браузер и вводит пароль за вас.\n"
    "Выполните в консоли две команды:\n"
    "    python -m pip install playwright\n"
    "    python -m playwright install chromium\n"
    "Вторая команда скачает браузер Chromium, примерно 150 МБ — так и задумано, "
    "это нормально."
)

CHROMIUM_MISSING = (
    "Playwright установлен, а браузера к нему нет. Выполните в консоли:\n"
    "    python -m playwright install chromium\n"
    "Команда скачает Chromium, примерно 150 МБ."
)

FFMPEG_MISSING = (
    "Не найден ffmpeg — скачивать поток нечем. Он должен лежать в папке "
    "программы (ffmpeg\\bin\\ffmpeg.exe)."
)

# Строка прогресса ffmpeg выглядит как «out_time_us=12345678»; всё остальное в
# выводе — предупреждения и ошибки, их копим на случай разбора полётов.
_PROGRESS_LINE = re.compile(r"^([a-z0-9_]+)=(.*)$")

_URL_IN_TEXT = re.compile(r"https?://[^\s\"'<>]+", re.I)
_BAD_NAME = re.compile(r'[\\/:*?"<>|]+')


# --------------------------------------------------------------------- мелочи


def _direct_env() -> dict[str, str]:
    """Окружение для дочерних процессов — без прокси.

    Локальный VPN-клиент (например, Hiddify на порту 12334) выставляет
    http_proxy/https_proxy всем процессам подряд. Браузер и ffmpeg это
    подхватывают и уходят через VPN: TLS до российских площадок рвётся, а
    подписанная ссылка на поток перестаёт совпадать по адресу и получает 403.
    Поэтому прокси вычищаем и на всякий случай глушим их через NO_PROXY.
    """
    env = dict(os.environ)
    for key in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY",
                "ALL_PROXY", "all_proxy"):
        env.pop(key, None)
    env["NO_PROXY"] = env["no_proxy"] = "*"
    return env


def _say(handle: Any, text: str) -> None:
    """Строка для человека: в карточку задачи, а оттуда и в общий журнал."""
    if handle is not None and hasattr(handle, "log"):
        handle.log(text)
    else:
        log.info("%s", text)


def _progress(handle: Any, value: float, note: str = "") -> None:
    if handle is not None and hasattr(handle, "progress"):
        try:
            handle.progress(value, note)
        except Exception:
            log.debug("не удалось показать прогресс", exc_info=True)


def _stop_if_cancelled(handle: Any) -> None:
    if handle is not None and getattr(handle, "cancelled", False):
        raise RuntimeError("отменено")


def _is_cancel(err: Exception) -> bool:
    return isinstance(err, RuntimeError) and "отменено" in str(err)


def _bare_url(raw: str) -> str:
    """Адрес без параметров запроса — только сайт и путь.

    Для журнала этого достаточно, а `_clean_url` здесь недостаточно строг: он
    режет известные секретные параметры, но подпись доступа к потоку у каждой
    площадки называется по-своему. В сообщениях об ошибках выкидываем запрос
    целиком и не гадаем.
    """
    try:
        parts = urlsplit(str(raw or ""))
    except ValueError:
        return "<ссылка>"
    if parts.scheme not in ("http", "https"):
        return "<ссылка>"
    return "%s://%s%s" % (parts.scheme, parts.netloc, parts.path)


def _safe_text(text: Any, *secrets: str) -> str:
    """Текст, который не стыдно показать: ссылки обрезаны, секреты вырезаны.

    И ffmpeg, и Playwright охотно печатают в ошибках полный адрес потока, а в
    нём подпись доступа; cookies площадки — такой же пропуск внутрь.
    """
    out = str(text or "")
    for secret in secrets:
        value = str(secret or "")
        if len(value) >= 4:
            out = out.replace(value, "…")
    return _URL_IN_TEXT.sub(lambda m: _bare_url(m.group(0)), out)


def _safe_name(text: str, maxlen: int = 60) -> str:
    """Заголовок страницы -> имя файла, пригодное для Windows."""
    name = _BAD_NAME.sub("_", str(text or ""))
    name = re.sub(r"[\s_]+", " ", name).strip(" ._")
    return name[:maxlen].strip(" ._") or "Видео с сайта"


def _free_path(folder: Path, base: str) -> Path:
    """Свободное имя файла в папке.

    Facecast выдаёт всем залам ОДИН и тот же заголовок страницы, поэтому без
    суффикса записи из одной пачки молча затирали бы друг друга.
    """
    path = folder / ("%s.mp4" % base)
    number = 2
    while path.exists():
        path = folder / ("%s (%d).mp4" % (base, number))
        number += 1
    return path


def _hms(seconds: float) -> str:
    total = max(0, int(seconds))
    return "%d:%02d:%02d" % (total // 3600, (total % 3600) // 60, total % 60)


# ------------------------------------------------------------- готовность


def _chromium_installed() -> bool:
    """Скачан ли браузер для Playwright.

    Смотрим папку кэша, а не запускаем Playwright: запуск поднимает служебный
    процесс на несколько секунд, а `available()` дёргает интерфейс. Если
    браузеры разложены нестандартно — не спорим и считаем, что всё на месте:
    точную причину всё равно покажет попытка запуска.
    """
    where = (os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or "").strip()
    if where == "0":
        return True          # браузер лежит внутри самого пакета
    if not where:
        local = (os.environ.get("LOCALAPPDATA") or "").strip()
        if not local:
            return True      # не Windows или нестандартный профиль — судить не берёмся
        where = str(Path(local) / "ms-playwright")
    try:
        root = Path(where)
        if not root.is_dir():
            return False
        return any(item.name.startswith("chromium") for item in root.iterdir())
    except OSError:
        return True


def available() -> tuple[bool, str]:
    """Готов ли источник к работе и, если нет, что нужно сделать.

    Интерфейс показывает вторую строку человеку — поэтому она написана
    по-русски и с точными командами, а не кодом ошибки.
    """
    try:
        from importlib.util import find_spec
        spec = find_spec("playwright.sync_api")
    except Exception:
        spec = None
    if spec is None:
        return False, PLAYWRIGHT_MISSING
    if not _chromium_installed():
        return False, CHROMIUM_MISSING

    exe = audio_io.ffmpeg_exe()
    if not Path(exe).exists() and shutil.which(exe) is None:
        return False, FFMPEG_MISSING
    return True, "Готово: браузер и ffmpeg на месте."


# ------------------------------------------------------------------ браузер


def _new_browser_page(pw):
    """Видимое окно Chromium, ходящее напрямую.

    headless=False принципиально: facecast и подобные узнают headless-Chromium
    по отпечатку и рвут соединение (ERR_CONNECTION_CLOSED) ещё до формы входа.
    `--no-proxy-server` плюс чистое окружение — чтобы браузер не подхватил
    системный прокси локального VPN, через который площадка недоступна.
    """
    try:
        browser = pw.chromium.launch(headless=False, args=["--no-proxy-server"],
                                     env=_direct_env())
    except Exception as err:
        if "executable doesn" in str(err).lower():
            raise RuntimeError(CHROMIUM_MISSING) from err
        raise RuntimeError("Не удалось открыть браузер: " + _safe_text(err)[:200]) from err
    context = browser.new_context(user_agent=UA, ignore_https_errors=True)
    return browser, context.new_page()


def _fill_login_form(page, login: str, password: str) -> bool:
    """Найти ВИДИМОЕ поле пароля в любом фрейме, заполнить и отправить.

    Плеер обычно сидит в iframe, а форм входа на странице бывает несколько, и
    все, кроме нужной, спрятаны (display:none) до тех пор, пока плеер не
    попросит вход. Поэтому перебираем все фреймы и берём только то поле,
    которое видно на экране.

    Таймауты короткие нарочно: Playwright по умолчанию ждёт появления скрытого
    поля по 30 секунд, и перебор форм растягивался на минуты.

    Здесь нет ни одного сообщения в журнал: рядом с паролем любая отладочная
    строка — лишний риск. Возвращаем просто «получилось или нет».
    """
    for frame in page.frames:
        try:
            fields = frame.query_selector_all("input[type='password'], input[name='password']")
        except Exception:
            continue                      # фрейм мог исчезнуть прямо во время перебора
        for field in fields:
            try:
                if not field.is_visible():
                    continue
            except Exception:
                continue
            try:
                if login:                 # логин или почта нужны не всем площадкам
                    for selector in ("input[type='email']", "input[name*='login' i]",
                                     "input[name*='email' i]", "input[name*='user' i]",
                                     "input[type='text']"):
                        box = frame.query_selector(selector)
                        if box and box.is_visible():
                            box.fill(login, timeout=3000)
                            break
                field.fill(password, timeout=3000)
                try:
                    field.press("Enter", timeout=3000)
                except Exception:
                    pass                  # часть форм на Enter не реагирует — жмём кнопку
                for selector in ("button[type='submit']", "input[type='submit']", ".sbtn",
                                 "button:has-text('Войти')", "button:has-text('Смотреть')",
                                 "button:has-text('Вход')"):
                    button = frame.query_selector(selector)
                    if button and button.is_visible():
                        try:
                            button.click(timeout=3000)
                        except Exception:
                            pass
                        break
                return True
            except Exception:
                continue
    return False


def _capture_on_page(page, url: str, login: str, password: str,
                     handle: Any = None) -> tuple[str, str, str]:
    """На уже открытом окне: зайти на страницу, войти и подслушать адрес потока.

    Возвращает (адрес .m3u8, строка Cookie, заголовок страницы).
    Бросает RuntimeError, если поток так и не появился.
    """
    captured: list[str] = []

    def on_request(request) -> None:
        address = request.url or ""
        if ".m3u8" in address.lower():
            captured.append(address)

    page.on("request", on_request)
    try:
        _say(handle, "Открываю страницу…")
        opened = False
        for attempt in range(GOTO_TRIES):
            _stop_if_cancelled(handle)
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=GOTO_TIMEOUT_MS)
                opened = True
                break
            except Exception as err:
                _say(handle, "  попытка %d не удалась: %s"
                     % (attempt + 1, _safe_text(err)[:90]))
                page.wait_for_timeout(2500)
        if not opened:
            raise RuntimeError("страница не открылась — проверьте ссылку и то, "
                               "открывается ли она в обычном браузере")

        _say(handle, "Ввожу логин и пароль…")
        for _ in range(LOGIN_WAIT_S):
            _stop_if_cancelled(handle)
            if _fill_login_form(page, login, password):
                break
            page.wait_for_timeout(1000)   # форма и плеер догружаются не сразу

        _say(handle, "Жду, пока запустится плеер…")
        for _ in range(STREAM_WAIT_S):
            if captured:
                break
            _stop_if_cancelled(handle)
            page.wait_for_timeout(1000)

        title = ""
        try:
            title = (page.title() or "").strip()
        except Exception:
            pass
        cookies = page.context.cookies()
    finally:
        try:
            page.remove_listener("request", on_request)
        except Exception:
            pass

    if not captured:
        raise RuntimeError("плеер так и не запросил видео — проверьте ссылку, "
                           "логин и пароль")

    # Нужен мастер-плейлист (в нём перечислены качества), а не отдельный вариант.
    # Если по имени его не видно, берём самый короткий адрес: у вариантов к
    # мастеру дописан хвост с качеством, так что они всегда длиннее.
    uniq = list(dict.fromkeys(captured))
    master = [u for u in uniq
              if any(mark in u.lower() for mark in ("index.m3u8", "master", "/source.m3u8"))]
    stream = master[0] if master else min(uniq, key=len)

    cookie_str = "; ".join("%s=%s" % (c.get("name"), c.get("value")) for c in cookies)
    return stream, cookie_str, title


# ---------------------------------------------------------------- скачивание


def _kill(proc: subprocess.Popen) -> None:
    try:
        proc.terminate()
    except Exception:
        pass
    try:
        proc.wait(timeout=5)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def _download_stream(stream: str, cookie_str: str, referer: str, dst: Path,
                     handle: Any = None, base: float = 0.0) -> None:
    """Скачать поток нашим ffmpeg — с cookies, User-Agent и Referer браузера.

    Referer подставляем ИСХОДНЫЙ адрес страницы, со всеми параметрами: часть
    площадок сверяет его целиком, и очищенный вариант (тот, что уходит в
    заметку) здесь не годится.
    """
    args = [
        audio_io.ffmpeg_exe(), "-hide_banner", "-nostdin", "-loglevel", "warning",
        "-user_agent", UA, "-referer", referer,
    ]
    if cookie_str:
        args += ["-headers", "Cookie: %s\r\n" % cookie_str]
    args += [
        # crypto в списке обязателен: сегменты потока зашифрованы, и ключ к ним
        # ffmpeg забирает отдельным запросом — с теми же cookies.
        "-protocol_whitelist", "file,http,https,tcp,tls,crypto",
        # На часовых записях соединение рвётся почти гарантированно; без
        # реконнекта ffmpeg бросает файл на середине и считает это успехом.
        "-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_on_network_error", "1",
        "-reconnect_delay_max", "5",
        "-i", stream,
        # Перекодировать нечего: пишем как есть, иначе час записи стал бы часом
        # работы процессора. aac_adtstoasc — обязательная переупаковка звука
        # из потока в mp4-контейнер.
        "-c", "copy", "-bsf:a", "aac_adtstoasc",
        "-progress", "pipe:1", "-nostats",
        "-y", str(dst),
    ]

    proc = subprocess.Popen(
        args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace",
        env=_direct_env(), creationflags=platform.system().hidden_process_flags(),
    )

    tail: deque[str] = deque(maxlen=12)
    done_seconds = [0.0]

    def pump() -> None:
        # Вывод обязательно вычитывать в отдельном потоке: переполненная труба
        # останавливает ffmpeg намертво, и скачивание встаёт без всякой причины.
        try:
            for line in proc.stdout:
                text = line.strip()
                if not text:
                    continue
                found = _PROGRESS_LINE.match(text)
                if found is None:
                    tail.append(text)
                elif found.group(1) == "out_time_us":
                    try:
                        done_seconds[0] = max(0.0, float(found.group(2)) / 1_000_000.0)
                    except ValueError:
                        pass
        except Exception:
            log.debug("чтение вывода ffmpeg прервано", exc_info=True)

    reader = threading.Thread(target=pump, name="hagen-webauth-ffmpeg", daemon=True)
    reader.start()

    # Сколько всего в записи, заранее неизвестно (плейлист может дописываться),
    # поэтому процент не выдумываем: показываем, сколько уже скачано по времени.
    last_note = 0.0
    while proc.poll() is None:
        if handle is not None and getattr(handle, "cancelled", False):
            _kill(proc)
            try:
                # недокачанный кусок никому не нужен, а весить может гигабайты
                dst.unlink(missing_ok=True)
            except OSError:
                pass
            raise RuntimeError("отменено")
        now = time.time()
        if now - last_note > 10:
            last_note = now
            got = done_seconds[0]
            _progress(handle, base,
                      "скачиваю запись, готово %s" % _hms(got) if got else "скачиваю запись…")
        time.sleep(0.4)
    reader.join(timeout=3)

    size = dst.stat().st_size if dst.exists() else 0
    if proc.returncode != 0 or size < MIN_VIDEO_BYTES:
        if dst.exists() and size < MIN_VIDEO_BYTES:
            try:
                dst.unlink()          # огрызок только путается под ногами
            except OSError:
                pass
        why = _safe_text(" / ".join(list(tail)[-4:]), cookie_str)[:400]
        hint = ""
        if "403" in why or "401" in why:
            hint = (" Похоже, площадка пускает смотреть, но не даёт скачивать, "
                    "либо истёк вход — попробуйте ещё раз.")
        # Windows отдаёт код возврата без знака, и вместо «-138» в сообщении
        # появлялось четырёхмиллиардное число, пугающее на ровном месте.
        code = proc.returncode or 0
        if code > 2 ** 31:
            code -= 2 ** 32
        raise RuntimeError("ffmpeg не смог скачать поток (код %s). %s%s"
                           % (code, why or "Подробностей нет.", hint))


# --------------------------------------------------------------- точка входа


def capture(urls: list[str], login: str, password: str, dst_dir: Path,
            handle: Any = None) -> list[dict[str, Any]]:
    """Скачать записи с сайта, который пускает только по логину и паролю.

    На всю пачку открывается ОДНО окно браузера: сначала для каждой ссылки
    перехватывается адрес потока, и только потом, уже без браузера, ffmpeg
    качает файлы. Держать окно открытым весь час скачивания незачем, а
    полученные cookies живут дольше, чем сам браузер.

    Возвращает по словарю общего договора источников на каждую УДАВШУЮСЯ
    ссылку. Неудавшиеся пропускаются, причина пишется в журнал задачи: терять
    из-за одной битой ссылки полчаса работы по остальным обидно. Если не вышло
    вообще ничего — RuntimeError с понятной причиной.

    handle (если передан) получает ход работы и умеет отменять: между кусками
    работы проверяется handle.cancelled, скачивание при отмене прерывается.
    """
    ready, why = available()
    if not ready:
        raise RuntimeError(why)

    links: list[str] = []
    for raw in (urls or []):
        one = str(raw or "").strip()
        if one.lower().startswith(("http://", "https://")) and one not in links:
            links.append(one)
    if not links:
        raise RuntimeError("Нет ни одной ссылки на страницу с записью — "
                           "нужен адрес, начинающийся с http.")
    if not (password or "").strip():
        raise RuntimeError("Не введён пароль от сайта — без него страница "
                           "не пустит к записи.")

    dst_dir = Path(dst_dir)
    dst_dir.mkdir(parents=True, exist_ok=True)

    # Тяжёлый импорт держим здесь: без него служба должна стартовать и честно
    # говорить, что источник не готов, а не падать при запуске.
    from playwright.sync_api import sync_playwright

    grabbed: list[tuple[str, str, str, str]] = []   # ссылка, поток, cookies, заголовок
    failed: list[tuple[str, str]] = []

    _say(handle, "Открываю браузер, ссылок в работе — %d" % len(links))
    _say(handle, "Окно Chromium откроется на экране — это нормально, закрывать "
                 "его не нужно.")
    with sync_playwright() as pw:
        browser, page = _new_browser_page(pw)
        try:
            for number, link in enumerate(links, start=1):
                _stop_if_cancelled(handle)
                _progress(handle, CAPTURE_SHARE * (number - 1) / len(links),
                          "вход на сайт: %d из %d" % (number, len(links)))
                _say(handle, "Ссылка %d из %d: %s"
                     % (number, len(links), _clean_url(link) or "<ссылка>"))
                try:
                    grabbed.append((link, *_capture_on_page(page, link, login,
                                                            password, handle)))
                    _say(handle, "  поток найден: %s" % (grabbed[-1][3] or "без названия"))
                except Exception as err:
                    if _is_cancel(err):
                        raise
                    reason = _safe_text(err)[:200]
                    failed.append((link, reason))
                    _say(handle, "  не получилось: %s" % reason[:120])
        finally:
            # Окно закрываем сразу, как только всё перехвачено: дальше работает
            # ffmpeg, и браузер только мешается на экране и ест память.
            try:
                browser.close()
            except Exception:
                pass

    results: list[dict[str, Any]] = []
    for number, (link, stream, cookie_str, title) in enumerate(grabbed, start=1):
        _stop_if_cancelled(handle)
        share = CAPTURE_SHARE + (1.0 - CAPTURE_SHARE) * (number - 1) / len(grabbed)
        name = _safe_name(title)
        video = _free_path(dst_dir, name)
        _progress(handle, share, "скачиваю запись %d из %d" % (number, len(grabbed)))
        _say(handle, "Скачиваю запись %d из %d: %s" % (number, len(grabbed), name))
        try:
            _download_stream(stream, cookie_str, link, video, handle, share)
        except Exception as err:
            if _is_cancel(err):
                raise
            reason = _safe_text(err)[:300]
            failed.append((link, reason))
            _say(handle, "  не скачалось: %s" % reason[:150])
            continue

        results.append({
            "media": str(video),
            "transcript": "",          # такие площадки субтитров не отдают
            "title": title or name,
            "url": _clean_url(link),
            "source_name": SOURCE_NAME,
            # Длительность берём у скачанного файла: у потока её заранее не
            # спросить, а тут она достоверна и ничего не стоит.
            "duration_s": audio_io.media_duration(video),
            # Адрес потока и cookies сюда НЕ кладём: в них подпись доступа, а
            # extra уезжает в meta.json на диск.
            "extra": {"stream_host": urlsplit(stream).netloc,
                      "size_mb": round(video.stat().st_size / 1_000_000.0, 1)},
        })
        _say(handle, "  готово: %s" % video.name)

    if failed:
        _say(handle, "Не получилось скачать: %s"
             % "; ".join(_clean_url(one) or "<ссылка>" for one, _ in failed))
    if not results:
        raise RuntimeError("Ни одну запись скачать не удалось. %s"
                           % (failed[0][1] if failed else ""))

    _progress(handle, 1.0, "готово, записей — %d" % len(results))
    return results
