"""Единая точка входа: бот, планировщик и веб-админка в одном процессе."""

from __future__ import annotations

import asyncio
import logging

import uvicorn
from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from passrw import __version__
from passrw.bot.setup import create_dispatcher
from passrw.config import Settings, get_settings
from passrw.db import Database, repo
from passrw.logging_setup import setup_logging
from passrw.services.manager import TrackingManager
from passrw.services.rwby import RwbyClient
from passrw.web import create_web_app

logger = logging.getLogger(__name__)


async def maintenance_loop(settings: Settings, database: Database) -> None:
    """Закрывает просроченные трекинги и чистит старые логи по расписанию."""
    while True:
        await asyncio.sleep(settings.maintenance_interval)
        try:
            async with database.session() as session:
                expired = await repo.expire_past_trackings(session)
                deleted = await repo.cleanup_old_data(session, settings.retention_days)
            logger.info("Обслуживание: просрочено трекингов=%s, удалено записей=%s", expired, deleted)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("Ошибка фонового обслуживания")


async def _serve_web(app, settings: Settings) -> None:
    config = uvicorn.Config(
        app,
        host=settings.web_host,
        port=settings.web_port,
        log_level="warning",
        access_log=False,
        lifespan="on",
    )
    server = uvicorn.Server(config)
    # Сигналы обрабатывает aiogram, чтобы остановка была согласованной.
    server.install_signal_handlers = lambda: None  # type: ignore[method-assign]
    try:
        await server.serve()
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001
        logger.exception("Веб-админка остановлена из-за ошибки")


async def run(settings: Settings) -> None:
    if not settings.telegram_token:
        raise SystemExit("TELEGRAM_TOKEN не задан — заполните .env (см. .env.example)")

    setup_logging(settings)
    logger.info("Запуск passrw v%s", __version__)

    database = Database(settings.database_url)
    await database.create_all()

    bot = Bot(
        settings.telegram_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    client = RwbyClient(settings)
    manager = TrackingManager(bot, database, settings, client)

    tasks: list[asyncio.Task] = []
    if settings.web_enabled:
        web_app = create_web_app(settings, database, manager)
        tasks.append(asyncio.create_task(_serve_web(web_app, settings), name="web-admin"))
        logger.info("Админка: http://%s:%s", settings.web_host, settings.web_port)
        if not settings.admin_auth_configured:
            logger.warning("ADMIN_PASSWORD/ADMIN_PASSWORD_HASH не задан — вход в админку закрыт")
    else:
        logger.info("Веб-админка отключена (WEB_ENABLED=false)")

    tasks.append(
        asyncio.create_task(
            maintenance_loop(settings, database), name="maintenance"
        )
    )

    dispatcher = create_dispatcher(settings, database, manager, client)

    try:
        restored = await manager.restore()
        logger.info("Активных трекингов к запуску: %s", restored)
        await dispatcher.start_polling(
            bot, allowed_updates=dispatcher.resolve_used_update_types()
        )
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await manager.shutdown()
        await client.close()
        await bot.session.close()
        await database.dispose()
        logger.info("Остановлено")


def main() -> None:
    settings = get_settings()
    try:
        asyncio.run(run(settings))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
