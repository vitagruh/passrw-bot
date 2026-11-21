"""Единая точка входа: бот, планировщик и веб-админка в одном процессе."""

from __future__ import annotations

import asyncio
import logging

import uvicorn
from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError
from aiogram.types import BotCommand

from passrw import __version__
from passrw.bot.setup import create_dispatcher
from passrw.config import Settings, get_settings
from passrw.db import Database, repo
from passrw.logging_setup import setup_logging
from passrw.services.manager import TrackingManager
from passrw.services.rwby import RwbyClient
from passrw.web import create_web_app
from passrw.web.security import admin_credentials_fingerprint

logger = logging.getLogger(__name__)

BOT_COMMANDS = (
    BotCommand(command="track", description="Новый поиск и трекинг"),
    BotCommand(command="mytracks", description="Мои трекинги"),
    BotCommand(command="status", description="Статус проверок"),
    BotCommand(command="stop", description="Остановить трекинг"),
    BotCommand(command="cancel", description="Отменить текущее действие"),
    BotCommand(command="history", description="История поисков"),
    BotCommand(command="favorites", description="Избранные маршруты"),
    BotCommand(command="stats", description="Личная статистика"),
    BotCommand(command="settings", description="Фильтры и тихие часы"),
    BotCommand(command="privacy", description="Какие данные хранятся"),
    BotCommand(command="help", description="Справка"),
)


async def maintenance_loop(
    settings: Settings, database: Database, manager: TrackingManager
) -> None:
    """Закрывает просроченные трекинги и чистит старые логи по расписанию."""
    while True:
        await asyncio.sleep(settings.maintenance_interval)
        try:
            expired = await manager.expire()
            async with database.session() as session:
                deleted = await repo.cleanup_old_data(session, settings.retention_days)
            logger.info("Обслуживание: просрочено трекингов=%s, удалено записей=%s", expired, deleted)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("Ошибка фонового обслуживания")


def build_uvicorn_config(app, settings: Settings) -> uvicorn.Config:
    """Конфиг uvicorn: X-Forwarded-For принимается только от доверенных прокси."""
    return uvicorn.Config(
        app,
        host=settings.web_host,
        port=settings.web_port,
        log_level="warning",
        access_log=False,
        lifespan="on",
        proxy_headers=settings.proxy_headers_enabled,
        forwarded_allow_ips=settings.trusted_proxies or "127.0.0.1",
    )


async def _serve_web(app, settings: Settings) -> None:
    config = build_uvicorn_config(app, settings)
    server = uvicorn.Server(config)
    # Сигналы обрабатывает aiogram, чтобы остановка была согласованной.
    server.install_signal_handlers = lambda: None  # type: ignore[method-assign]
    try:
        await server.serve()
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001
        logger.exception("Веб-админка остановлена из-за ошибки")


def warn_insecure_defaults(settings: Settings) -> None:
    if not settings.web_enabled:
        return
    if settings.admin_password == "change-me":
        logger.warning(
            "ADMIN_PASSWORD содержит пароль по умолчанию — задайте свой пароль "
            "или ADMIN_PASSWORD_HASH перед публикацией админки"
        )
    if not settings.metrics_token:
        logger.warning("METRICS_TOKEN не задан — /metrics доступен без авторизации")
    if settings.trusted_proxies:
        logger.info("X-Forwarded-For принимается только от: %s", settings.trusted_proxies)
    else:
        logger.info("X-Forwarded-For игнорируется (TRUSTED_PROXIES не задан)")


async def run(settings: Settings) -> None:
    if not settings.telegram_token:
        raise SystemExit("TELEGRAM_TOKEN не задан — заполните .env (см. .env.example)")

    setup_logging(settings)
    logger.info("Запуск passrw v%s", __version__)

    database = Database(settings.database_url)
    await database.create_all()
    if settings.web_enabled:
        # Фиксируем отпечаток учётных данных: смена пароля отзовёт старые сессии.
        async with database.session() as session:
            await repo.ensure_admin_session_epoch(
                session, admin_credentials_fingerprint(settings)
            )

    bot = Bot(
        settings.telegram_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    client = RwbyClient(settings)
    manager = TrackingManager(bot, database, settings, client)

    try:
        await bot.set_my_commands(BOT_COMMANDS)
    except TelegramAPIError as exc:
        logger.warning("Не удалось зарегистрировать команды бота: %s", exc)

    tasks: list[asyncio.Task] = []
    if settings.web_enabled:
        web_app = create_web_app(settings, database, manager)
        tasks.append(asyncio.create_task(_serve_web(web_app, settings), name="web-admin"))
        logger.info("Админка: http://%s:%s", settings.web_host, settings.web_port)
        warn_insecure_defaults(settings)
        if not settings.admin_auth_configured:
            logger.warning("ADMIN_PASSWORD/ADMIN_PASSWORD_HASH не задан — вход в админку закрыт")
    else:
        logger.info("Веб-админка отключена (WEB_ENABLED=false)")

    tasks.append(
        asyncio.create_task(
            maintenance_loop(settings, database, manager), name="maintenance"
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
