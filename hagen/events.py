# -*- coding: utf-8 -*-
"""Рассылка событий открытым вкладкам — общая для службы и её маршрутов.

Раньше `EventHub` и единственный `hub` жили в `server.py`. Пока маршруты были
там же, это не мешало; после разрезания на роутеры роутеру пришлось бы
импортировать `server`, а `server` импортирует роутеры — замкнутый круг.
Поэтому рассылка вынесена отдельным модулем: её
импортируют и служба, и роутеры, а она — никого.

Код класса перенесён без изменений. Имена сохранены: `server.hub` и
`server.EventHub` продолжают работать, потому что `server` забирает их отсюда.
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from fastapi import WebSocket


class EventHub:
    """Рассылка событий открытым вкладкам. Вкладок может не быть — это нормально."""

    def __init__(self) -> None:
        self._clients: set[WebSocket] = set()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._lock = asyncio.Lock()
        self._last_level: dict[str, float] = {}

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    async def add(self, ws: WebSocket) -> None:
        async with self._lock:
            self._clients.add(ws)

    async def remove(self, ws: WebSocket) -> None:
        async with self._lock:
            self._clients.discard(ws)

    async def _broadcast(self, payload: dict[str, Any]) -> None:
        async with self._lock:
            clients = list(self._clients)
        if not clients:
            return
        text = json.dumps(payload, ensure_ascii=False, default=str)
        dead = []
        for ws in clients:
            try:
                await ws.send_text(text)
            except Exception:
                dead.append(ws)
        if dead:
            async with self._lock:
                for ws in dead:
                    self._clients.discard(ws)

    def publish(self, payload: dict[str, Any]) -> None:
        """Можно звать из любого потока."""
        # уровни индикаторов приходят десятки раз в секунду — прореживаем
        if payload.get("type") == "level":
            key = "%s|%s" % (payload.get("rec_id"), payload.get("track"))
            now = time.time()
            if now - self._last_level.get(key, 0.0) < 0.12:
                return
            self._last_level[key] = now
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            asyncio.run_coroutine_threadsafe(self._broadcast(payload), loop)
        except RuntimeError:
            pass


#: Единственная рассылка на весь процесс.
hub = EventHub()
