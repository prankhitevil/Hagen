# -*- coding: utf-8 -*-
"""Скачать файл по адресу и сверить его с контрольной суммой.

Этим путём приходит всё, что программа берёт из интернета по описи выпуска
(`lock.json`): библиотеки, ffmpeg, архив обновления. Сумма SHA-256 записана в
описи заранее — файл, который по дороге подменили или недокачали, не ляжет на
место: он качается во временный файл и переименовывается, только когда сумма
сошлась.

Модуль берёт только стандартную библиотеку Python: им пользуется установщик,
когда окружения с библиотеками программы ещё нет. Прокси — системный, как у
любой программы Windows; сертификаты — из хранилища Windows (так Python на
Windows делает сам, а в программе это закреплено ещё и через truststore).
"""
from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

USER_AGENT = "Hagen (+https://github.com/prankhitevil/Hagen)"
CHUNK = 1 << 20

#: Сколько раз пробовать заново. И при обрыве связи, и при несовпадении суммы:
#: файл бывает, приходит битым без всякой ошибки связи (так случилось на
#: сборке 21.09 — со второго раза пришёл целым). Подменённый же файл не сойдётся
#: ни разу, и тогда — отказ.
RETRIES = 3


class DownloadError(RuntimeError):
    """Скачать не вышло; текст — для человека."""


class Cancelled(DownloadError):
    """Скачивание отменили."""


Progress = Callable[[int, int], Any]   # (скачано байт, всего байт или 0)


def sha256_file(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(CHUNK), b""):
            h.update(block)
    return h.hexdigest()


def _request(url: str, headers: dict[str, str] | None = None) -> urllib.request.Request:
    head = {"User-Agent": USER_AGENT}
    head.update(headers or {})
    return urllib.request.Request(url, headers=head)


def get_json(url: str, headers: dict[str, str] | None = None, timeout: float = 20.0) -> Any:
    """GET и разобрать JSON. Код ответа ≠ 200 — urllib.error.HTTPError как есть."""
    with urllib.request.urlopen(_request(url, headers), timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch(url: str, dest: str | Path, sha256: str | None = None, size: int | None = None,
          progress: Progress | None = None, cancelled: Callable[[], bool] | None = None,
          headers: dict[str, str] | None = None, timeout: float = 60.0) -> Path:
    """Скачать url в dest. Файл с той же суммой уже лежит — не качаем заново.

    sha256 — ожидаемая сумма (без неё файл принимается как есть: так качаются
    только архивы, чью сумму сообщает сам GitHub, а он её дал не всем).
    """
    dest = Path(dest)
    want = (sha256 or "").lower().removeprefix("sha256:")
    if want and dest.exists() and sha256_file(dest) == want:
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    last: Exception | None = None
    for attempt in range(RETRIES):
        if attempt:
            time.sleep(2.0 * attempt)
        try:
            _fetch_once(url, part, want, size, progress, cancelled, headers, timeout)
            os.replace(part, dest)
            return dest
        except Cancelled:
            part.unlink(missing_ok=True)
            raise
        except (DownloadError, urllib.error.URLError, OSError, TimeoutError) as err:
            last = err
            part.unlink(missing_ok=True)
    if isinstance(last, DownloadError):
        raise last
    raise DownloadError("Не удалось скачать %s: %s" % (_short(url), last))


def _fetch_once(url: str, part: Path, want: str, size: int | None,
                progress: Progress | None, cancelled: Callable[[], bool] | None,
                headers: dict[str, str] | None, timeout: float) -> None:
    h = hashlib.sha256()
    done = 0
    with urllib.request.urlopen(_request(url, headers), timeout=timeout) as resp, \
            open(part, "wb") as out:
        total = int(resp.headers.get("Content-Length") or size or 0)
        while True:
            if cancelled is not None and cancelled():
                raise Cancelled("Скачивание отменено.")
            block = resp.read(CHUNK)
            if not block:
                break
            out.write(block)
            h.update(block)
            done += len(block)
            if progress is not None:
                try:
                    progress(done, total)
                except Exception:
                    pass
    expected = int(size or 0) or total
    if expected and done != expected:
        raise OSError("пришло %d байт из %d" % (done, expected))
    if want and h.hexdigest() != want:
        raise DownloadError("Файл %s пришёл не тот: контрольная сумма не сошлась. "
                            "Его могли подменить по дороге — ставить такой нельзя."
                            % _short(url))


def _short(url: str) -> str:
    """Адрес для сообщения: без параметров, хвост имени."""
    base = str(url).split("?", 1)[0].split("#", 1)[0]
    return base if len(base) <= 90 else "…" + base[-88:]
