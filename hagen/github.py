# -*- coding: utf-8 -*-
"""Релизы программы на GitHub — внешний сервис с узкой дверью.

Программа спрашивает у GitHub одно: какие выпуски лежат в репозитории (адрес —
release.json, ключ «updates») и какие файлы к ним приложены. Спрашивает
только по кнопке «Проверить обновления», без токена: открытый репозиторий
отвечает всем. Закрытый отвечает «не найдено» — тогда обновление ставится из
файла, скачанного вручную.

Ответ GitHub приводится к своему виду, дальше программа не знает, как устроен
его API.
"""
from __future__ import annotations

import urllib.error
from typing import Any

from . import download

API = "https://api.github.com"
HEADERS = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}


class NotAvailable(RuntimeError):
    """GitHub не ответил списком выпусков; текст — для человека."""


def releases(repo: str, timeout: float = 20.0) -> list[dict[str, Any]]:
    """Выпуски репозитория, свежие первыми: номер, заметки, приложенные файлы."""
    try:
        data = download.get_json("%s/repos/%s/releases?per_page=20" % (API, repo),
                                 headers=HEADERS, timeout=timeout)
    except urllib.error.HTTPError as err:
        if err.code == 404:
            raise NotAvailable("Репозиторий %s не отвечает: он закрытый или его нет. Скачайте "
                               "архив выпуска со страницы релиза и выберите его кнопкой "
                               "«Установить из файла…»." % repo) from None
        if err.code in (403, 429):
            raise NotAvailable("GitHub просит подождать: слишком много проверок подряд. "
                               "Попробуйте через час.") from None
        raise NotAvailable("GitHub ответил ошибкой %s." % err.code) from None
    except (urllib.error.URLError, OSError, ValueError) as err:
        raise NotAvailable("Нет связи с GitHub: %s" % getattr(err, "reason", err)) from None
    out = []
    for item in data if isinstance(data, list) else []:
        assets = []
        for a in item.get("assets") or []:
            digest = str(a.get("digest") or "")
            assets.append({
                "name": str(a.get("name") or ""),
                "url": str(a.get("browser_download_url") or ""),
                "size": int(a.get("size") or 0),
                "sha256": digest.split(":", 1)[1] if digest.startswith("sha256:") else "",
            })
        out.append({
            "tag": str(item.get("tag_name") or ""),
            "title": str(item.get("name") or ""),
            "notes": str(item.get("body") or ""),
            "draft": bool(item.get("draft")),
            "prerelease": bool(item.get("prerelease")),
            "published": str(item.get("published_at") or ""),
            "page": str(item.get("html_url") or ""),
            "assets": assets,
        })
    return out
