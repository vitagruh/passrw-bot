"""Мидлвари: регистрация пользователя, блокировка и антифлуд."""

from __future__ import annotations

import logging
import time
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject

from passrw.db import Database, repo

logger = logging.getLogger(__name__)


class UserMiddleware(BaseMiddleware):
    def __init__(self, database: Database) -> None:
        self.database = database

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        from_user = getattr(event, "from_user", None)
        if from_user is None:
            return await handler(event, data)

        async with self.database.session() as session:
            user = await repo.upsert_user(
                session,
                from_user.id,
                from_user.username,
                from_user.first_name,
                from_user.last_name,
            )
            blocked = user.is_blocked

        if blocked:
            text = "⛔ Доступ к боту ограничен администратором."
            if isinstance(event, CallbackQuery):
                await event.answer(text, show_alert=True)
            elif isinstance(event, Message):
                await event.answer(text)
            return None

        data["db_user"] = user
        return await handler(event, data)


class ThrottleMiddleware(BaseMiddleware):
    def __init__(self, limit: int = 20, window: int = 60) -> None:
        self.limit = limit
        self.window = window
        self._hits: dict[int, deque[float]] = defaultdict(deque)

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = getattr(event, "from_user", None)
        if user is None:
            return await handler(event, data)

        now = time.monotonic()
        hits = self._hits[user.id]
        while hits and now - hits[0] > self.window:
            hits.popleft()
        if len(hits) >= self.limit:
            if isinstance(event, Message):
                await event.answer("⏳ Слишком много сообщений. Подождите минуту.")
            return None
        hits.append(now)
        return await handler(event, data)
