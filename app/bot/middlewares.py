"""Middleware aiogram — сквозная логика до хендлеров."""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import Message, TelegramObject, User

log = logging.getLogger(__name__)


class AccessMiddleware(BaseMiddleware):
    """Whitelist по user_id.

    Бот пишет в личную базу Notion, поэтому по умолчанию он персональный.
    Пустой allowlist = режим «открыт для всех» (для демо/командного инбокса).
    """

    def __init__(self, allowlist: set[int]):
        self._allowlist = allowlist

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if not self._allowlist:
            return await handler(event, data)

        user: User | None = data.get("event_from_user")
        if user is None or user.id in self._allowlist:
            return await handler(event, data)

        log.warning("Отклонён доступ: user_id=%s username=%s", user.id, user.username)
        if isinstance(event, Message):
            await event.answer("Этот бот приватный.")
        return None


class ThrottleMiddleware(BaseMiddleware):
    """Простой троттлинг: не чаще одного сообщения в N секунд от пользователя.

    Защищает и Notion (rate limit), и очередь от случайного флуда.
    Хранит состояние в памяти — этого достаточно для одного инстанса.
    """

    def __init__(self, rate: float = 0.7):
        self._rate = rate
        self._last: dict[int, float] = {}

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user: User | None = data.get("event_from_user")
        if user is not None:
            now = time.monotonic()
            last = self._last.get(user.id, 0.0)
            if now - last < self._rate:
                return None
            self._last[user.id] = now
        return await handler(event, data)
