"""Стартовые экраны, справка и обработка нераспознанного ввода."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from passrw.bot import keyboards
from passrw.bot.texts import HELP, PRIVACY, WELCOME, web_admin_link
from passrw.config import Settings
from passrw.db.models import User

router = Router(name="common")


async def _main_menu(message: Message) -> None:
    await message.answer(WELCOME, reply_markup=keyboards.main_menu())


@router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await _main_menu(message)


@router.callback_query(F.data == "menu:main")
async def menu_main(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await call.answer()
    try:
        await call.message.edit_text(WELCOME, reply_markup=keyboards.main_menu())
    except Exception:  # noqa: BLE001 — сообщение могло быть отправлено другим ботом
        await call.message.answer(WELCOME, reply_markup=keyboards.main_menu())


@router.message(Command("help"))
@router.callback_query(F.data == "menu:help")
async def cmd_help(event: Message | CallbackQuery) -> None:
    if isinstance(event, CallbackQuery):
        await event.answer()
        await event.message.answer(HELP, reply_markup=keyboards.back_to_menu())
    else:
        await event.answer(HELP, reply_markup=keyboards.back_to_menu())


@router.message(Command("privacy"))
async def cmd_privacy(message: Message) -> None:
    await message.answer(PRIVACY, reply_markup=keyboards.back_to_menu())


@router.message(Command("admin"))
async def cmd_admin(
    message: Message, settings: Settings, db_user: User | None = None
) -> None:
    if db_user is None or db_user.role != "admin":
        await message.answer(
            "🔒 Команда доступна только администратору.\n"
            "Роль выдаётся в веб-админке на странице пользователей."
        )
        return
    if not settings.web_enabled:
        text = (
            "🖥 Веб-админка отключена. Задайте <code>WEB_ENABLED=true</code> "
            "и пароль администратора."
        )
    else:
        text = web_admin_link(settings.public_url)
    await message.answer(
        text,
        reply_markup=keyboards.back_to_menu(),
        disable_web_page_preview=True,
    )


@router.message(F.text)
async def fallback(message: Message) -> None:
    await message.answer(
        "🤔 Не понял сообщение. Начать поиск — /track, справка — /help.",
        reply_markup=keyboards.main_menu(),
    )
