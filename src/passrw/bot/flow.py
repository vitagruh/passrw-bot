"""Поиск поездов и общие представления, разделяемые роутерами."""

from __future__ import annotations

import logging
from datetime import date
from html import escape

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError

from passrw.db import Database, repo
from passrw.db.models import AvailabilitySample, Tracking
from passrw.domain.railway import Train
from passrw.services.notifications import build_train_card
from passrw.services.parser import LayoutChanged
from passrw.services.rwby import RwbyClient, SiteUnavailable
from passrw.util.time import fmt_local

logger = logging.getLogger(__name__)

SPARK = "▁▂▃▄▅▆▇█"


async def perform_search(
    bot: Bot,
    database: Database,
    client: RwbyClient,
    chat_id: int,
    from_station: str,
    to_station: str,
    travel_date: date,
    passengers: int,
) -> list[Train] | None:
    """Выполняет поиск, пишет историю и отправляет список поездов."""
    async with database.session() as session:
        await repo.add_search_history(
            session,
            chat_id=chat_id,
            from_station=from_station,
            to_station=to_station,
            travel_date=travel_date,
            passengers=passengers,
        )
        await repo.bump_stats(session, chat_id, searches=1)
        await repo.bump_station_usage(session, from_station, to_station)
        await repo.log_user_event(
            session,
            chat_id,
            "SEARCH",
            f"{from_station} → {to_station} · {travel_date} · {passengers} чел.",
        )

    try:
        trains = await client.search(from_station, to_station, travel_date)
    except LayoutChanged:
        await bot.send_message(
            chat_id,
            "⚠️ Сайт ответил, но расписание не удалось разобрать. "
            "Похоже, pass.rw.by изменил вёрстку — я уже сообщил администратору.",
        )
        return None
    except SiteUnavailable as exc:
        logger.warning("pass.rw.by недоступен: %s", exc)
        await bot.send_message(
            chat_id,
            "🌐 Сайт pass.rw.by сейчас недоступен или ограничивает запросы. "
            "Попробуйте через пару минут.",
        )
        return None
    except Exception:  # noqa: BLE001
        logger.exception("Ошибка поиска %s → %s", from_station, to_station)
        await bot.send_message(chat_id, "❌ Не удалось выполнить поиск. Попробуйте ещё раз.")
        return None

    if not trains:
        await bot.send_message(
            chat_id,
            f"🚫 На {travel_date.strftime('%d.%m.%Y')} поездов "
            f"{escape(from_station)} → {escape(to_station)} не найдено.\n"
            "Проверьте дату или попробуйте другой маршрут.",
        )
        return []

    header = (
        f"🚂 <b>{escape(from_station)} → {escape(to_station)}</b>\n"
        f"📅 {travel_date.strftime('%d.%m.%Y')} · 👥 {passengers} чел.\n"
        f"Найдено поездов: <b>{len(trains)}</b>. Выберите нужный:"
    )
    from passrw.bot.keyboards import trains_kb

    try:
        await bot.send_message(chat_id, header, reply_markup=trains_kb(trains))
    except TelegramAPIError as exc:
        logger.warning("Не удалось отправить список поездов %s: %s", chat_id, exc)
    return trains


async def send_train_card(
    bot: Bot,
    chat_id: int,
    *,
    train: Train,
    index: int,
    passengers: int,
    from_station: str,
    to_station: str,
    travel_date: date,
    favorite: bool = False,
) -> None:
    from passrw.bot.keyboards import train_actions_kb
    from passrw.services.rwby import RwbyClient as _Client

    text = build_train_card(train=train, passengers=passengers)
    keyboard = train_actions_kb(
        index,
        buy_url=_Client.buy_url(from_station, to_station, travel_date),
        favorite=favorite,
    )
    await bot.send_message(
        chat_id, text, reply_markup=keyboard, disable_web_page_preview=True
    )


def tracking_line(index: int, tracking: Tracking, tz) -> str:
    seats = tracking.seats_available or 0
    mark = "✅" if seats > 0 else "🔄"
    last_check = fmt_local(tracking.last_checked_at, tz, "%d.%m %H:%M")
    return (
        f"{index}. {mark} <b>{escape(tracking.from_station)} → {escape(tracking.to_station)}</b>\n"
        f"   📅 {tracking.travel_date.strftime('%d.%m.%Y')} · {escape(tracking.depart_time)} "
        f"· №{escape(tracking.train_number or '—')}\n"
        f"   👥 {tracking.passengers} чел. · 🪑 {seats} мест · 🔁 {tracking.requests_count} проверок\n"
        f"   💓 heartbeat: {'вкл' if tracking.heartbeat_enabled else 'выкл'} · "
        f"⏱ проверено: {last_check}"
    )


def trend_text(tracking: Tracking, samples: list[AvailabilitySample], tz) -> str:
    header = (
        f"📈 <b>История наличия</b>\n"
        f"{escape(tracking.from_station)} → {escape(tracking.to_station)} · "
        f"{tracking.travel_date.strftime('%d.%m.%Y')} · поезд {escape(tracking.train_number or '—')}"
        f" ({escape(tracking.depart_time)})\n\n"
    )
    if not samples:
        return header + "Данных пока нет — бот ещё не сделал ни одного замера."

    seats = [sample.seats for sample in samples]
    top = max(seats) or 1
    spark = "".join(SPARK[min(7, int(value / top * 7))] for value in seats)
    lines = [
        header,
        f"<code>{spark}</code>",
        f"Сейчас: <b>{seats[-1]}</b> мест",
    ]
    prices = [sample.min_price for sample in samples if sample.min_price is not None]
    if prices:
        lines.append(f"💰 Последняя цена: <b>{prices[-1]:.2f} BYN</b>")
        if len(prices) >= 2 and prices[-1] != prices[0]:
            delta = prices[-1] - prices[0]
            arrow = "🔻" if delta < 0 else "🔺"
            lines.append(f"{arrow} Изменение: {delta:+.2f} BYN")

    lines.append("\n<b>Последние замеры:</b>")
    for sample in samples[-8:]:
        price = f", {sample.min_price:.2f} BYN" if sample.min_price is not None else ""
        lines.append(
            f"{fmt_local(sample.checked_at, tz, '%d.%m %H:%M')} — {sample.seats} мест{price}"
        )
    return "\n".join(lines)


def stats_text(stats, active: int, favorites: int) -> str:
    searches = stats.total_searches if stats else 0
    created = stats.trackings_created if stats else 0
    notifications = stats.notifications_sent if stats else 0
    conversion = (notifications / searches * 100) if searches else 0.0
    return (
        "📊 <b>Ваша статистика</b>\n\n"
        f"🔍 Поисков: <b>{searches}</b>\n"
        f"🔔 Создано трекингов: <b>{created}</b>\n"
        f"🎯 Уведомлений о местах: <b>{notifications}</b>\n"
        f"📈 Доля поисков с находкой: <b>{conversion:.0f}%</b>\n"
        f"▶️ Активных трекингов: <b>{active}</b>\n"
        f"⭐ Избранных маршрутов: <b>{favorites}</b>"
    )
