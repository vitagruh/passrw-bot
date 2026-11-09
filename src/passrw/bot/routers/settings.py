"""Пользовательские настройки: тихие часы, фильтры, пассажиры."""

from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from passrw.bot import keyboards
from passrw.bot.states import SettingsFlow
from passrw.config import Settings
from passrw.db import Database, repo
from passrw.db.models import UserSettings
from passrw.domain.rules import CARRIAGE_FILTERS, parse_quiet_hours

router = Router(name="settings")


def summary_text(row: UserSettings | None) -> str:
    quiet = (
        f"{row.quiet_from}–{row.quiet_to}"
        if row and row.quiet_from and row.quiet_to
        else "выключены"
    )
    carriage = (row.carriage_filter if row else None) or "любой"
    max_price = row.max_price if row else None
    threshold = row.notify_threshold if row else 1
    passengers = row.default_passengers if row else 1
    return (
        "⚙️ <b>Настройки</b>\n\n"
        f"🕓 Тихие часы: <b>{quiet}</b>\n"
        f"🎫 Тип вагона: <b>{carriage}</b>\n"
        f"💰 Макс. цена: <b>{f'{max_price:.2f} BYN' if max_price else 'не задана'}</b>\n"
        f"🔔 Порог мест: <b>{threshold}</b>\n"
        f"👥 Пассажиров по умолчанию: <b>{passengers}</b>\n\n"
        "Выберите, что изменить:"
    )


async def _render(bot: Bot, chat_id: int, database: Database) -> None:
    async with database.session() as session:
        row = await repo.get_user_settings(session, chat_id)
    await bot.send_message(
        chat_id, summary_text(row), reply_markup=keyboards.settings_kb()
    )


@router.message(Command("settings"))
async def cmd_settings(message: Message, state: FSMContext, database: Database) -> None:
    await state.clear()
    await _render(message.bot, message.chat.id, database)


@router.callback_query(F.data == "menu:settings")
async def menu_settings(call: CallbackQuery, state: FSMContext, database: Database) -> None:
    await state.clear()
    await call.answer()
    await _render(call.bot, call.message.chat.id, database)


@router.callback_query(F.data == "set:open")
async def settings_open(call: CallbackQuery, database: Database) -> None:
    await call.answer()
    await _render(call.bot, call.message.chat.id, database)


@router.callback_query(F.data == "set:quiet")
async def settings_quiet(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(SettingsFlow.quiet)
    await call.answer()
    await call.message.answer(
        "🕓 <b>Тихие часы</b>\n"
        "Ночью уведомления не отправляются, проверки продолжаются.\n\n"
        "Формат: <code>22:00-08:00</code>. Отключить: <code>off</code>."
    )


@router.message(SettingsFlow.quiet)
async def settings_quiet_input(
    message: Message, state: FSMContext, database: Database
) -> None:
    value = (message.text or "").strip().lower()
    parsed = parse_quiet_hours(value)
    if parsed is None:
        await message.answer(
            "❌ Не понял интервал. Пример: <code>22:00-08:00</code> или <code>off</code>."
        )
        return
    quiet_from, quiet_to = parsed
    async with database.session() as session:
        await repo.save_user_settings(
            session, message.chat.id, quiet_from=quiet_from, quiet_to=quiet_to
        )
    await state.clear()
    await message.answer(
        f"✅ Тихие часы: <b>{quiet_from}–{quiet_to}</b>"
        if quiet_from
        else "✅ Тихие часы выключены."
    )
    await _render(message.bot, message.chat.id, database)


@router.callback_query(F.data == "set:price")
async def settings_price(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(SettingsFlow.max_price)
    await call.answer()
    await call.message.answer(
        "💰 <b>Максимальная цена</b>\n"
        "Уведомлять только о вагонах не дороже указанной суммы.\n\n"
        "Отправьте число в BYN (например <code>150</code>) или <code>off</code>."
    )


@router.message(SettingsFlow.max_price)
async def settings_price_input(
    message: Message, state: FSMContext, database: Database
) -> None:
    value = (message.text or "").strip().lower().replace(",", ".")
    if value in {"off", "нет", "выкл", "-", "0"}:
        max_price = None
    else:
        try:
            max_price = float(value)
        except ValueError:
            await message.answer("❌ Нужно число, например <code>150</code>, или <code>off</code>.")
            return
        if not 0 < max_price <= 100000:
            await message.answer("❌ Слишком большая или нулевая цена.")
            return
    async with database.session() as session:
        await repo.save_user_settings(session, message.chat.id, max_price=max_price)
    await state.clear()
    await message.answer(
        f"✅ Максимальная цена: <b>{max_price:.2f} BYN</b>"
        if max_price
        else "✅ Ограничение по цене снято."
    )
    await _render(message.bot, message.chat.id, database)


@router.callback_query(F.data == "set:carriage")
async def settings_carriage(call: CallbackQuery) -> None:
    await call.answer()
    await call.message.answer(
        "🎫 <b>Тип вагона</b>\nВыберите фильтр:",
        reply_markup=keyboards.carriage_kb(),
    )


@router.callback_query(F.data.startswith("setc:"))
async def settings_carriage_set(
    call: CallbackQuery, database: Database
) -> None:
    key = call.data.split(":", 1)[1]
    if key not in CARRIAGE_FILTERS:
        await call.answer("Неизвестный тип", show_alert=True)
        return
    async with database.session() as session:
        await repo.save_user_settings(
            session, call.message.chat.id, carriage_filter=CARRIAGE_FILTERS[key]
        )
    await call.answer("Сохранено")
    await _render(call.bot, call.message.chat.id, database)


@router.callback_query(F.data == "set:threshold")
async def settings_threshold(call: CallbackQuery, settings: Settings) -> None:
    await call.answer()
    await call.message.answer(
        "🔔 <b>Порог мест</b>\nУведомлять, когда доступно не меньше N мест:",
        reply_markup=keyboards.threshold_kb(settings.max_passengers),
    )


@router.callback_query(F.data.startswith("sett:"))
async def settings_threshold_set(
    call: CallbackQuery, database: Database, settings: Settings
) -> None:
    try:
        value = int(call.data.split(":", 1)[1])
    except ValueError:
        await call.answer("Некорректное значение", show_alert=True)
        return
    if not 1 <= value <= settings.max_passengers:
        await call.answer("Значение вне диапазона", show_alert=True)
        return
    async with database.session() as session:
        await repo.save_user_settings(session, call.message.chat.id, notify_threshold=value)
    await call.answer("Сохранено")
    await _render(call.bot, call.message.chat.id, database)


@router.callback_query(F.data == "set:pax")
async def settings_pax(call: CallbackQuery, settings: Settings) -> None:
    await call.answer()
    await call.message.answer(
        "👥 <b>Пассажиры по умолчанию</b>\nПодставлять это число в новый поиск:",
        reply_markup=keyboards.default_pax_kb(settings.max_passengers),
    )


@router.callback_query(F.data.startswith("setp:"))
async def settings_pax_set(
    call: CallbackQuery, database: Database, settings: Settings
) -> None:
    try:
        value = int(call.data.split(":", 1)[1])
    except ValueError:
        await call.answer("Некорректное значение", show_alert=True)
        return
    if not 1 <= value <= settings.max_passengers:
        await call.answer("Значение вне диапазона", show_alert=True)
        return
    async with database.session() as session:
        await repo.save_user_settings(session, call.message.chat.id, default_passengers=value)
    await call.answer("Сохранено")
    await _render(call.bot, call.message.chat.id, database)


@router.callback_query(F.data == "set:reset")
async def settings_reset(call: CallbackQuery, database: Database) -> None:
    async with database.session() as session:
        await repo.save_user_settings(
            session,
            call.message.chat.id,
            quiet_from=None,
            quiet_to=None,
            notify_threshold=1,
            default_passengers=1,
            carriage_filter=None,
            max_price=None,
        )
    await call.answer("♻️ Настройки сброшены")
    await _render(call.bot, call.message.chat.id, database)
