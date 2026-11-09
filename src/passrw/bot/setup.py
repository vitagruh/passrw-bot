"""Сборка Dispatcher: middleware, роутеры и обработчик ошибок."""

from __future__ import annotations

import logging
import traceback

from aiogram import Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import ErrorEvent

from passrw.bot.middlewares import ThrottleMiddleware, UserMiddleware
from passrw.bot.routers import common, history, search, trackings
from passrw.bot.routers import settings as settings_router
from passrw.config import Settings
from passrw.db import Database, repo
from passrw.services.manager import TrackingManager
from passrw.services.rwby import RwbyClient

logger = logging.getLogger(__name__)


def create_dispatcher(
    settings: Settings,
    database: Database,
    manager: TrackingManager,
    client: RwbyClient,
) -> Dispatcher:
    dp = Dispatcher(storage=MemoryStorage())
    dp["settings"] = settings
    dp["database"] = database
    dp["manager"] = manager
    dp["client"] = client

    for observer in (dp.message, dp.callback_query):
        observer.middleware(UserMiddleware(database))
        observer.middleware(ThrottleMiddleware())

    # common подключается последним: его fallback ловит всё, что не обработано выше.
    dp.include_routers(
        search.router,
        trackings.router,
        settings_router.router,
        history.router,
        common.router,
    )

    @dp.errors()
    async def on_error(event: ErrorEvent) -> bool:
        exc = event.exception
        update = event.update
        chat_id = None
        if update.message:
            chat_id = update.message.chat.id
        elif update.callback_query and update.callback_query.message:
            chat_id = update.callback_query.message.chat.id
        logger.error(
            "Необработанная ошибка (chat_id=%s): %s", chat_id, exc, exc_info=exc
        )
        try:
            async with database.session() as session:
                await repo.log_bot_error(
                    session,
                    type(exc).__name__,
                    str(exc),
                    "".join(traceback.format_exception(exc)),
                    chat_id=chat_id,
                )
        except Exception:  # noqa: BLE001 — логирование не должно падать
            logger.exception("Не удалось сохранить ошибку бота в БД")
        return True

    return dp
