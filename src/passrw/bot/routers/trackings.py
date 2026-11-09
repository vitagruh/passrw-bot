"""Управление трекингами: список, статус, история, шаринг, остановка."""

from __future__ import annotations

from html import escape
from urllib.parse import urlencode

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from passrw.bot import keyboards
from passrw.bot.flow import tracking_line, trend_text
from passrw.bot.texts import no_trackings, tracks_header
from passrw.config import Settings
from passrw.db import Database, repo
from passrw.db.models import Tracking
from passrw.services.manager import TrackingManager

router = Router(name="trackings")


async def _owned(database: Database, tracking_id: int, chat_id: int) -> Tracking | None:
    async with database.session() as session:
        tracking = await repo.get_tracking(session, tracking_id)
    if tracking is None or tracking.chat_id != chat_id:
        return None
    return tracking


async def _send_list(
    message: Message,
    database: Database,
    settings: Settings,
    *,
    detailed: bool = False,
) -> None:
    async with database.session() as session:
        trackings = await repo.list_user_trackings(session, message.chat.id)
    if not trackings:
        await message.answer(no_trackings(), reply_markup=keyboards.main_menu())
        return
    header = tracks_header(len(trackings))
    if detailed:
        header += "Проверки идут автоматически, ближе к дате — чаще.\n"
    tz = settings.timezone
    body = "\n\n".join(
        tracking_line(index, tracking, tz) for index, tracking in enumerate(trackings, 1)
    )
    await message.answer(
        header + "\n" + body,
        reply_markup=keyboards.trackings_kb(trackings),
        disable_web_page_preview=True,
    )


@router.message(Command("mytracks"))
async def cmd_mytracks(message: Message, database: Database, settings: Settings) -> None:
    await _send_list(message, database, settings)


@router.message(Command("status"))
async def cmd_status(message: Message, database: Database, settings: Settings) -> None:
    await _send_list(message, database, settings, detailed=True)


@router.callback_query(F.data == "menu:mytracks")
async def menu_mytracks(call: CallbackQuery, database: Database, settings: Settings) -> None:
    await call.answer()
    await _send_list(call.message, database, settings)


@router.message(Command("stop"))
async def cmd_stop(message: Message, database: Database) -> None:
    async with database.session() as session:
        trackings = await repo.list_user_trackings(session, message.chat.id)
    if not trackings:
        await message.answer("ℹ️ Нет активных трекингов.", reply_markup=keyboards.main_menu())
        return
    await message.answer(
        f"⏹ <b>Что остановить?</b> ({len(trackings)} активных)",
        reply_markup=keyboards.stop_kb(trackings),
    )


@router.callback_query(F.data.regexp(r"^mt:trend:\d+$"))
async def tracking_trend(
    call: CallbackQuery, database: Database, settings: Settings
) -> None:
    tracking_id = int(call.data.rsplit(":", 1)[1])
    tracking = await _owned(database, tracking_id, call.message.chat.id)
    if tracking is None:
        await call.answer("Трекинг не найден", show_alert=True)
        return
    async with database.session() as session:
        samples = await repo.list_samples(session, tracking_id, 40)
    await call.answer()
    await call.message.answer(
        trend_text(tracking, samples, settings.timezone),
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="🔄 Обновить", callback_data=f"mt:trend:{tracking_id}")]
            ]
        ),
    )


@router.callback_query(F.data.regexp(r"^mt:share:\d+$"))
async def tracking_share(call: CallbackQuery, database: Database) -> None:
    tracking_id = int(call.data.rsplit(":", 1)[1])
    tracking = await _owned(database, tracking_id, call.message.chat.id)
    if tracking is None:
        await call.answer("Трекинг не найден", show_alert=True)
        return

    route_url = "https://pass.rw.by/ru/route/?" + urlencode(
        {
            "from": tracking.from_station,
            "to": tracking.to_station,
            "date": tracking.travel_date.isoformat(),
        }
    )
    share_url = "https://t.me/share/url?" + urlencode(
        {
            "url": route_url,
            "text": (
                f"Слежу за билетами {tracking.from_station} → {tracking.to_station} "
                f"на {tracking.travel_date.strftime('%d.%m.%Y')}"
            ),
        }
    )
    await call.answer()
    await call.message.answer(
        f"🔗 <b>{escape(tracking.from_station)} → {escape(tracking.to_station)}</b>\n"
        f"📅 {tracking.travel_date.strftime('%d.%m.%Y')} · {escape(tracking.depart_time)} · "
        f"№{escape(tracking.train_number or '—')}",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="📤 Поделиться", url=share_url)],
                [InlineKeyboardButton(text="🎫 Открыть на pass.rw.by", url=route_url)],
            ]
        ),
        disable_web_page_preview=True,
    )


@router.callback_query(F.data.regexp(r"^mt:stop:\d+$"))
async def tracking_stop(
    call: CallbackQuery,
    database: Database,
    manager: TrackingManager,
) -> None:
    tracking_id = int(call.data.rsplit(":", 1)[1])
    tracking = await _owned(database, tracking_id, call.message.chat.id)
    if tracking is None:
        await call.answer("Трекинг не найден", show_alert=True)
        return
    await manager.stop(
        tracking_id,
        reason="user_stop",
        notify_user=True,
        message=(
            f"⏹ Трекинг {tracking.from_station} → {tracking.to_station} "
            f"({tracking.travel_date.strftime('%d.%m.%Y')}) остановлен."
        ),
    )
    await call.answer("⏹ Остановлен")


@router.callback_query(F.data == "stop:all")
async def stop_all(call: CallbackQuery, database: Database, manager: TrackingManager) -> None:
    count = await manager.stop_all_for_chat(call.message.chat.id)
    await call.answer(f"Остановлено: {count}")


@router.callback_query(F.data == "menu:status")
async def menu_status(call: CallbackQuery, database: Database, settings: Settings) -> None:
    await call.answer()
    await _send_list(call.message, database, settings, detailed=True)
