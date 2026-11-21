"""Слэш-команды, которые должны работать в любом состоянии диалога.

Роутер подключается первым: без фильтра по состоянию команды не перехватываются
шагами поиска (например, `/help` во время ввода станции).
"""

from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from passrw.bot import keyboards
from passrw.bot.routers import common, history, search, trackings
from passrw.bot.routers import settings as settings_router
from passrw.db import Database

router = Router(name="commands")

router.message.register(common.cmd_start, Command("start"))
router.message.register(common.cmd_help, Command("help"))
router.message.register(common.cmd_privacy, Command("privacy"))
router.message.register(common.cmd_admin, Command("admin"))
router.message.register(history.cmd_history, Command("history"))
router.message.register(history.cmd_favorites, Command("favorites"))
router.message.register(history.cmd_stats, Command("stats"))
router.message.register(trackings.cmd_mytracks, Command("mytracks"))
router.message.register(trackings.cmd_status, Command("status"))
router.message.register(trackings.cmd_stop, Command("stop"))
router.message.register(settings_router.cmd_settings, Command("settings"))


@router.message(Command("track"))
async def cmd_track(message: Message, state: FSMContext, database: Database) -> None:
    await search.start_flow(message.bot, message.chat.id, state, database)


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    if await state.get_state() is None:
        await message.answer("Нечего отменять.", reply_markup=keyboards.main_menu())
        return
    await state.clear()
    await message.answer("✖️ Действие отменено.", reply_markup=keyboards.main_menu())
