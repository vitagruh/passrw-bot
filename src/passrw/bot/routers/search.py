"""Пошаговый поиск: станции → дата → пассажиры → поезд → трекинг."""

from __future__ import annotations

import logging
from datetime import date, timedelta
from html import escape

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from passrw.bot import keyboards
from passrw.bot.flow import perform_search, send_train_card
from passrw.bot.states import SearchFlow
from passrw.config import Settings
from passrw.db import Database, repo
from passrw.domain.rules import clean_text, validate_station
from passrw.services.manager import TrackingManager
from passrw.services.notifications import build_tracking_summary
from passrw.services.rwby import RwbyClient
from passrw.util.time import local_now

logger = logging.getLogger(__name__)
router = Router(name="search")


async def start_flow(bot: Bot, chat_id: int, state: FSMContext, database: Database) -> None:
    await state.clear()
    async with database.session() as session:
        popular = await repo.popular_stations(session, 5)
    await state.set_state(SearchFlow.from_station)
    await state.update_data(candidates=popular)
    await bot.send_message(
        chat_id,
        "1️⃣ <b>Откуда едем?</b>\n"
        "Напишите станцию или выберите из популярных:",
        reply_markup=keyboards.stations_suggestions(popular),
    )


@router.message(Command("track"))
async def cmd_track(message: Message, state: FSMContext, database: Database) -> None:
    await start_flow(message.bot, message.chat.id, state, database)


@router.callback_query(F.data == "menu:track")
async def menu_track(call: CallbackQuery, state: FSMContext, database: Database) -> None:
    await call.answer()
    await start_flow(call.bot, call.message.chat.id, state, database)


@router.message(SearchFlow.from_station)
async def from_station_input(message: Message, state: FSMContext, database: Database) -> None:
    value = clean_text(message.text)
    if not validate_station(value):
        await message.answer("❌ Название станции выглядит некорректно. Попробуйте ещё раз.")
        return
    async with database.session() as session:
        candidates = await repo.suggest_stations(session, value)
    if candidates:
        await state.update_data(candidates=candidates, pending=value)
        await message.answer(
            "🔎 Похожие станции — выберите нужную:",
            reply_markup=keyboards.stations_suggestions(candidates, raw=value),
        )
        return
    await _accept_from(message.bot, message.chat.id, state, database, value)


@router.message(SearchFlow.to_station)
async def to_station_input(message: Message, state: FSMContext, database: Database) -> None:
    value = clean_text(message.text)
    if not validate_station(value):
        await message.answer("❌ Название станции выглядит некорректно. Попробуйте ещё раз.")
        return
    async with database.session() as session:
        candidates = await repo.suggest_stations(session, value)
    if candidates:
        await state.update_data(candidates=candidates, pending=value)
        await message.answer(
            "🔎 Похожие станции — выберите нужную:",
            reply_markup=keyboards.stations_suggestions(candidates, raw=value),
        )
        return
    await _accept_to(message.bot, message.chat.id, state, database, value)


@router.callback_query(F.data.startswith("st:"))
async def station_choice(
    call: CallbackQuery, state: FSMContext, database: Database, settings: Settings
) -> None:
    data = await state.get_data()
    candidates: list[str] = data.get("candidates") or []
    payload = call.data.split(":", 1)[1]
    if payload == "raw":
        value = data.get("pending") or ""
    else:
        try:
            value = candidates[int(payload)]
        except (ValueError, IndexError):
            await call.answer("Станция уже недоступна, повторите ввод", show_alert=True)
            return
    if not validate_station(value):
        await call.answer("Некорректное название", show_alert=True)
        return

    current = await state.get_state()
    await call.answer()
    if current == SearchFlow.from_station.state:
        await _accept_from(call.bot, call.message.chat.id, state, database, value)
    elif current == SearchFlow.to_station.state:
        await _accept_to(call.bot, call.message.chat.id, state, database, value)
    else:
        await call.message.answer("Поиск уже завершён. Начните новый: /track")


async def _accept_from(
    bot: Bot, chat_id: int, state: FSMContext, database: Database, value: str
) -> None:
    if not value:
        await bot.send_message(chat_id, "Не удалось определить станцию. Начните заново: /track")
        return
    async with database.session() as session:
        popular = await repo.popular_stations(session, 5)
    await state.update_data(from_station=value, candidates=popular, pending=None)
    await state.set_state(SearchFlow.to_station)
    await bot.send_message(
        chat_id,
        f"2️⃣ <b>Куда едем из {escape(value)}?</b>\n"
        "Напишите станцию назначения или выберите из популярных:",
        reply_markup=keyboards.stations_suggestions(popular),
    )


async def _accept_to(
    bot: Bot, chat_id: int, state: FSMContext, database: Database, value: str
) -> None:
    data = await state.get_data()
    from_station = data.get("from_station") or ""
    if value.casefold() == from_station.casefold():
        await bot.send_message(chat_id, "❌ Станции отправления и назначения совпадают.")
        return
    today = local_now().date()
    await state.update_data(to_station=value, candidates=[], pending=None)
    await state.set_state(SearchFlow.travel_date)
    await bot.send_message(
        chat_id,
        f"3️⃣ <b>Когда едем?</b>\n{escape(from_station)} → {escape(value)}\n"
        "Выберите дату:",
        reply_markup=keyboards.calendar_kb(today.year, today.month, today),
    )


@router.callback_query(F.data.startswith("cal:"))
async def calendar_choice(
    call: CallbackQuery, state: FSMContext, settings: Settings
) -> None:
    if await state.get_state() != SearchFlow.travel_date.state:
        await call.answer("Выбор даты сейчас недоступен", show_alert=True)
        return
    parts = call.data.split(":")
    if parts[1] == "noop":
        await call.answer()
        return

    today = local_now().date()
    if parts[1] == "nav":
        try:
            year, month = (int(part) for part in parts[2].split("-"))
        except (ValueError, IndexError):
            await call.answer()
            return
        first_allowed = date(today.year, today.month, 1)
        requested = date(year, month, 1)
        if requested < first_allowed or requested > today + timedelta(days=365):
            await call.answer("Эта дата вне допустимого диапазона", show_alert=True)
            return
        await call.message.edit_reply_markup(
            reply_markup=keyboards.calendar_kb(year, month, today)
        )
        await call.answer()
        return

    if parts[1] == "day":
        try:
            selected = date.fromisoformat(parts[2])
        except (ValueError, IndexError):
            await call.answer("Некорректная дата", show_alert=True)
            return
        if selected < today or selected > today + timedelta(days=365):
            await call.answer("Дата вне допустимого диапазона", show_alert=True)
            return
        await state.update_data(travel_date=selected.isoformat())
        await state.set_state(SearchFlow.passengers)
        await call.answer()
        try:
            await call.message.edit_reply_markup(reply_markup=None)
        except Exception:  # noqa: BLE001
            pass
        await call.message.answer(
            f"4️⃣ <b>Сколько пассажиров?</b>\n📅 {selected.strftime('%d.%m.%Y')}",
            reply_markup=keyboards.passengers_kb(settings.max_passengers),
        )


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("✖️ Действие отменено.", reply_markup=keyboards.back_to_menu())


@router.callback_query(F.data.startswith("pax:"))
async def passengers_choice(
    call: CallbackQuery,
    state: FSMContext,
    database: Database,
    client: RwbyClient,
    settings: Settings,
) -> None:
    if await state.get_state() != SearchFlow.passengers.state:
        await call.answer("Выбор пассажиров недоступен", show_alert=True)
        return
    try:
        passengers = int(call.data.split(":", 1)[1])
    except ValueError:
        await call.answer("Некорректное число", show_alert=True)
        return
    if not 1 <= passengers <= settings.max_passengers:
        await call.answer(
            f"От 1 до {settings.max_passengers} пассажиров", show_alert=True
        )
        return

    data = await state.get_data()
    try:
        travel_date = date.fromisoformat(data["travel_date"])
    except (KeyError, ValueError):
        await call.answer("Поиск устарел, начните заново", show_alert=True)
        return

    await state.update_data(passengers=passengers)
    await state.set_state(SearchFlow.results)
    await call.answer()
    try:
        await call.message.edit_text("🔎 Ищу поезда…")
    except Exception:  # noqa: BLE001
        pass

    trains = await perform_search(
        call.bot,
        database,
        client,
        call.message.chat.id,
        data.get("from_station", ""),
        data.get("to_station", ""),
        travel_date,
        passengers,
    )
    await state.update_data(trains=trains or [])


@router.callback_query(F.data.regexp(r"^tr:\d+$"))
async def train_choice(
    call: CallbackQuery, state: FSMContext, database: Database
) -> None:
    data = await state.get_data()
    trains = data.get("trains") or []
    try:
        index = int(call.data.split(":", 1)[1])
        train = trains[index]
        travel_date = date.fromisoformat(data["travel_date"])
    except (ValueError, IndexError, KeyError):
        await call.answer("Список поездов устарел. Повторите поиск.", show_alert=True)
        return

    async with database.session() as session:
        favorites = await repo.list_favorites(session, call.message.chat.id)
    favorite = any(
        item.from_station == data.get("from_station") and item.to_station == data.get("to_station")
        for item in favorites
    )
    await call.answer()
    await send_train_card(
        call.bot,
        call.message.chat.id,
        train=train,
        index=index,
        passengers=data.get("passengers", 1),
        from_station=data.get("from_station", ""),
        to_station=data.get("to_station", ""),
        travel_date=travel_date,
        favorite=favorite,
    )


@router.callback_query(F.data.regexp(r"^trk:\d+$"))
async def create_tracking(
    call: CallbackQuery,
    state: FSMContext,
    database: Database,
    settings: Settings,
    manager: TrackingManager,
) -> None:
    data = await state.get_data()
    trains = data.get("trains") or []
    try:
        index = int(call.data.split(":", 1)[1])
        train = trains[index]
        travel_date = date.fromisoformat(data["travel_date"])
        from_station = data["from_station"]
        to_station = data["to_station"]
        passengers = int(data["passengers"])
    except (ValueError, IndexError, KeyError):
        await call.answer("Поиск устарел. Начните заново: /track", show_alert=True)
        return

    chat_id = call.message.chat.id
    async with database.session() as session:
        trackings = await repo.list_user_trackings(session, chat_id)
        if len(trackings) >= settings.max_trackings_per_user:
            await call.answer(
                f"Достигнут лимит {settings.max_trackings_per_user} трекингов. "
                "Остановите один из них.",
                show_alert=True,
            )
            return
        existing = next(
            (
                item
                for item in trackings
                if item.from_station == from_station
                and item.to_station == to_station
                and item.travel_date == travel_date
                and item.depart_time == train.depart_time
                and item.train_number == train.number
            ),
            None,
        )
        user_settings = await repo.get_user_settings(session, chat_id)
        if existing is None:
            tracking = await repo.create_tracking(
                session,
                chat_id=chat_id,
                from_station=from_station,
                to_station=to_station,
                travel_date=travel_date,
                passengers=passengers,
                train_number=train.number,
                depart_time=train.depart_time,
                notify_threshold=None,
                carriage_filter=None,
                max_price=None,
                stop_on_success=settings.stop_on_success,
            )
            tracking_id = tracking.id
            await repo.bump_stats(session, chat_id, trackings=1)
            await repo.log_user_event(
                session,
                chat_id,
                "TRACK_CREATED",
                f"id={tracking_id} {from_station} → {to_station} {travel_date} {train.number}",
            )
        else:
            tracking_id = existing.id

    manager.add(tracking_id)
    await state.clear()
    await call.answer("✅ Трекинг активен")

    threshold = max(passengers, (user_settings.notify_threshold if user_settings else 1) or 1)
    carriage = user_settings.carriage_filter if user_settings else None
    max_price = user_settings.max_price if user_settings else None
    text = build_tracking_summary(
        from_station=from_station,
        to_station=to_station,
        travel_date=travel_date,
        passengers=passengers,
        train_number=train.number,
        depart_time=train.depart_time,
        threshold=threshold,
        carriage_filter=carriage,
        max_price=max_price,
    )
    if existing is not None:
        text = "ℹ️ Такой трекинг уже есть — продолжаю следить.\n\n" + text
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📋 Мои трекинги", callback_data="menu:mytracks")],
            [InlineKeyboardButton(text="🏠 Меню", callback_data="menu:main")],
        ]
    )
    await call.message.answer(text, reply_markup=keyboard, disable_web_page_preview=True)


@router.callback_query(F.data.regexp(r"^trfav:\d+$"))
async def add_favorite(call: CallbackQuery, state: FSMContext, database: Database) -> None:
    data = await state.get_data()
    trains = data.get("trains") or []
    try:
        index = int(call.data.split(":", 1)[1])
        trains[index]
        from_station = data["from_station"]
        to_station = data["to_station"]
        passengers = int(data["passengers"])
    except (ValueError, IndexError, KeyError):
        await call.answer("Поиск устарел", show_alert=True)
        return
    async with database.session() as session:
        await repo.add_favorite(session, call.message.chat.id, from_station, to_station, passengers)
    await call.answer("⭐ Маршрут сохранён")


@router.callback_query(F.data.regexp(r"^refresh:\d+$"))
async def refresh_results(
    call: CallbackQuery,
    state: FSMContext,
    database: Database,
    client: RwbyClient,
) -> None:
    data = await state.get_data()
    try:
        travel_date = date.fromisoformat(data["travel_date"])
    except (KeyError, ValueError):
        await call.answer("Поиск устарел", show_alert=True)
        return
    await call.answer("🔄 Обновляю…")
    trains = await perform_search(
        call.bot,
        database,
        client,
        call.message.chat.id,
        data.get("from_station", ""),
        data.get("to_station", ""),
        travel_date,
        int(data.get("passengers", 1)),
    )
    if trains is not None:
        await state.update_data(trains=trains)


@router.callback_query(F.data == "back:list")
async def back_to_list(call: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    trains = data.get("trains") or []
    if not trains:
        await call.answer("Список пуст — начните новый поиск", show_alert=True)
        return
    await call.answer()
    await call.message.answer(
        f"🚂 <b>{escape(data.get('from_station', ''))} → "
        f"{escape(data.get('to_station', ''))}</b>\nВыберите поезд:",
        reply_markup=keyboards.trains_kb(trains),
    )


@router.callback_query(F.data == "search:cancel")
async def cancel_search(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await call.answer("Отменено")
    try:
        await call.message.edit_text(
            "✖️ Поиск отменён. Начать заново — /track.", reply_markup=keyboards.back_to_menu()
        )
    except Exception:  # noqa: BLE001
        await call.message.answer(
            "✖️ Поиск отменён. Начать заново — /track.", reply_markup=keyboards.back_to_menu()
        )
