"""История поисков, избранные маршруты и личная статистика."""

from __future__ import annotations

import secrets
from datetime import timedelta
from html import escape

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from passrw.bot import keyboards
from passrw.bot.flow import perform_search, stats_text
from passrw.bot.states import SearchFlow
from passrw.config import Settings
from passrw.db import Database, repo
from passrw.services.rwby import RwbyClient
from passrw.util.time import local_now

router = Router(name="history")


async def _send_history(
    bot: Bot, chat_id: int, database: Database, *, edit: Message | None = None
) -> None:
    async with database.session() as session:
        items = await repo.recent_searches(session, chat_id, 10)
    if not items:
        text = "📜 История пуста — поиски ещё не выполнялись."
        if edit is not None:
            await edit.edit_text(text, reply_markup=keyboards.main_menu())
        else:
            await bot.send_message(chat_id, text, reply_markup=keyboards.main_menu())
        return
    text = "📜 <b>Последние поиски</b>\nНажмите, чтобы повторить:"
    if edit is not None:
        await edit.edit_text(text, reply_markup=keyboards.history_kb(items))
    else:
        await bot.send_message(chat_id, text, reply_markup=keyboards.history_kb(items))


@router.message(Command("history"))
async def cmd_history(message: Message, database: Database) -> None:
    await _send_history(message.bot, message.chat.id, database)


@router.callback_query(F.data == "menu:history")
async def menu_history(call: CallbackQuery, database: Database) -> None:
    await call.answer()
    await _send_history(call.bot, call.message.chat.id, database, edit=call.message)


@router.callback_query(F.data.regexp(r"^hist:\d+$"))
async def repeat_search(
    call: CallbackQuery,
    state: FSMContext,
    database: Database,
    client: RwbyClient,
    settings: Settings,
) -> None:
    history_id = int(call.data.split(":", 1)[1])
    async with database.session() as session:
        item = await repo.get_search(session, history_id, call.message.chat.id)
    if item is None:
        await call.answer("Запись истории не найдена", show_alert=True)
        return

    today = local_now(settings.timezone).date()
    if item.travel_date < today:
        await call.answer("Дата этого поиска уже прошла", show_alert=True)
        return
    if item.travel_date > today + timedelta(days=settings.sale_window_days):
        await call.answer(
            f"Продажа открыта на {settings.sale_window_days} суток вперёд",
            show_alert=True,
        )
        return

    search_id = secrets.token_hex(4)
    await state.clear()
    await state.set_state(SearchFlow.results)
    await state.update_data(
        from_station=item.from_station,
        to_station=item.to_station,
        travel_date=item.travel_date.isoformat(),
        passengers=item.passengers,
        candidates=[],
        pending=None,
        trains=[],
        search_id=search_id,
    )
    await call.answer("🔎 Повторяю поиск…")
    trains = await perform_search(
        call.bot,
        database,
        client,
        call.message.chat.id,
        item.from_station,
        item.to_station,
        item.travel_date,
        item.passengers,
        search_id,
    )
    await state.update_data(trains=trains or [])


async def _send_favorites(
    bot: Bot, chat_id: int, database: Database, *, edit: Message | None = None
) -> None:
    async with database.session() as session:
        items = await repo.list_favorites(session, chat_id)
    if not items:
        text = "⭐ Избранных маршрутов пока нет.\nДобавить можно из карточки поезда."
        if edit is not None:
            await edit.edit_text(text, reply_markup=keyboards.main_menu())
        else:
            await bot.send_message(chat_id, text, reply_markup=keyboards.main_menu())
        return
    text = "⭐ <b>Избранные маршруты</b>\n🔁 — новый поиск, 🗑 — удалить:"
    if edit is not None:
        await edit.edit_text(text, reply_markup=keyboards.favorites_kb(items))
    else:
        await bot.send_message(chat_id, text, reply_markup=keyboards.favorites_kb(items))


@router.message(Command("favorites"))
async def cmd_favorites(message: Message, database: Database) -> None:
    await _send_favorites(message.bot, message.chat.id, database)


@router.callback_query(F.data == "menu:favorites")
async def menu_favorites(call: CallbackQuery, database: Database) -> None:
    await call.answer()
    await _send_favorites(call.bot, call.message.chat.id, database, edit=call.message)


@router.callback_query(F.data.regexp(r"^fav:go:\d+$"))
async def favorite_open(
    call: CallbackQuery,
    state: FSMContext,
    database: Database,
    settings: Settings,
) -> None:
    favorite_id = int(call.data.rsplit(":", 1)[1])
    async with database.session() as session:
        item = await repo.get_favorite(session, favorite_id, call.message.chat.id)
    if item is None:
        await call.answer("Маршрут не найден", show_alert=True)
        return

    today = local_now(settings.timezone).date()
    await state.clear()
    await state.set_state(SearchFlow.travel_date)
    await state.update_data(
        from_station=item.from_station,
        to_station=item.to_station,
        passengers=item.passengers,
        candidates=[],
        pending=None,
        trains=[],
        search_id=secrets.token_hex(4),
    )
    await call.answer()
    await call.message.answer(
        f"3️⃣ <b>Когда едем?</b>\n{escape(item.from_station)} → {escape(item.to_station)}\n"
        "Выберите дату:",
        reply_markup=keyboards.calendar_kb(
            today.year,
            today.month,
            today,
            max_date=today + timedelta(days=settings.sale_window_days),
        ),
    )


@router.callback_query(F.data.regexp(r"^fav:del:\d+$"))
async def favorite_delete(call: CallbackQuery, database: Database) -> None:
    favorite_id = int(call.data.rsplit(":", 1)[1])
    async with database.session() as session:
        deleted = await repo.delete_favorite(session, favorite_id, call.message.chat.id)
    if not deleted:
        await call.answer("Маршрут не найден", show_alert=True)
        return
    await call.answer("🗑 Удалено")
    await _send_favorites(call.bot, call.message.chat.id, database, edit=call.message)


@router.message(Command("stats"))
@router.callback_query(F.data == "menu:stats")
async def cmd_stats(event: Message | CallbackQuery, database: Database) -> None:
    chat_id = event.chat.id if isinstance(event, Message) else event.message.chat.id
    async with database.session() as session:
        stats = await repo.get_stats(session, chat_id)
        active = await repo.count_user_trackings(session, chat_id)
        favorites = len(await repo.list_favorites(session, chat_id))
    text = stats_text(stats, active, favorites)
    if isinstance(event, CallbackQuery):
        await event.answer()
        await event.message.answer(text, reply_markup=keyboards.back_to_menu())
    else:
        await event.answer(text, reply_markup=keyboards.back_to_menu())
