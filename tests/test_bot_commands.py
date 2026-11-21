"""Слэш-команды работают в любом состоянии поиска."""

from __future__ import annotations

from datetime import date, datetime

from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import AnswerCallbackQuery, SendMessage
from aiogram.types import CallbackQuery, Chat, Message, Update

from passrw.bot.routers import search
from passrw.bot.setup import create_dispatcher
from passrw.bot.states import SearchFlow
from passrw.db import repo
from passrw.domain.railway import Train
from passrw.services.manager import TrackingManager


class FakeSession(BaseSession):
    def __init__(self) -> None:
        super().__init__()
        self.requests: list = []

    async def make_request(self, bot: Bot, method, timeout=None):  # noqa: ASYNC109
        self.requests.append(method)
        if isinstance(method, SendMessage):
            return Message(
                message_id=len(self.requests),
                date=datetime.now(),
                chat=Chat(id=method.chat_id, type="private"),
                text=method.text,
                reply_markup=method.reply_markup,
            )
        if isinstance(method, AnswerCallbackQuery):
            return True
        return True

    async def close(self) -> None:
        return None

    async def stream_content(self, *args, **kwargs):
        yield b""


def make_bot() -> Bot:
    return Bot("123456:TESTTOKEN", session=FakeSession())


def make_update(bot: Bot, text: str, chat_id: int = 42) -> Update:
    return Update.model_validate(
        {
            "update_id": 1,
            "message": {
                "message_id": 1,
                "date": int(datetime.now().timestamp()),
                "chat": {"id": chat_id, "type": "private"},
                "from": {"id": chat_id, "is_bot": False, "first_name": "Tester"},
                "text": text,
            },
        },
        context={"bot": bot},
    )


def sent_texts(bot: Bot) -> list[str]:
    return [
        request.text
        for request in bot.session.requests
        if isinstance(request, SendMessage)
    ]


async def test_commands_work_inside_search_state(settings, database):
    bot = make_bot()
    manager = TrackingManager(bot, database, settings, client=None)
    dispatcher = create_dispatcher(settings, database, manager, None)
    key = StorageKey(bot_id=bot.id, chat_id=42, user_id=42)
    await dispatcher.storage.set_state(key, SearchFlow.from_station.state)

    await dispatcher.feed_update(bot, make_update(bot, "/help"))
    assert any("Команды" in text for text in sent_texts(bot))
    # Справка не должна сбрасывать начатый поиск.
    assert await dispatcher.storage.get_state(key) == SearchFlow.from_station.state

    await dispatcher.feed_update(bot, make_update(bot, "/cancel"))
    assert await dispatcher.storage.get_state(key) is None
    assert any("отменено" in text for text in sent_texts(bot))

    await dispatcher.feed_update(bot, make_update(bot, "/admin"))
    assert "только администратору" in sent_texts(bot)[-1]

    async with database.session() as session:
        await repo.set_user_role(session, 42, "admin")

    await dispatcher.feed_update(bot, make_update(bot, "/admin"))
    assert "Веб-админка отключена" in sent_texts(bot)[-1]


async def test_stale_search_callback_never_creates_tracking(settings, database):
    bot = make_bot()
    storage = MemoryStorage()
    state = FSMContext(storage, StorageKey(bot_id=bot.id, chat_id=42, user_id=42))
    await state.set_state(SearchFlow.results)
    await state.update_data(
        search_id="cafebabe",
        travel_date="2026-12-01",
        from_station="Минск",
        to_station="Москва",
        passengers=2,
        trains=[Train("712Б", "12:34")],
    )

    class ManagerStub:
        def __init__(self) -> None:
            self.added: list[int] = []

        def add(self, tracking_id: int) -> None:
            self.added.append(tracking_id)

    manager = ManagerStub()
    callback = CallbackQuery.model_validate(
        {
            "id": "cb-1",
            "from": {"id": 42, "is_bot": False, "first_name": "Tester"},
            "chat_instance": "chat-instance",
            "data": "trk:deadbeef:0",
            "message": {
                "message_id": 5,
                "date": int(datetime.now().timestamp()),
                "chat": {"id": 42, "type": "private"},
                "text": "Карточка поезда",
            },
        },
        context={"bot": bot},
    )

    await search.create_tracking(callback, state, database, settings, manager)

    answers = [
        request
        for request in bot.session.requests
        if isinstance(request, AnswerCallbackQuery)
    ]
    assert answers and "устарел" in answers[-1].text
    async with database.session() as session:
        assert await repo.list_user_trackings(session, 42) == []
    assert manager.added == []
    assert (await state.get_data())["search_id"] == "cafebabe"


def make_callback(bot: Bot, data: str, *, message_id: int) -> CallbackQuery:
    return CallbackQuery.model_validate(
        {
            "id": f"cb-{message_id}",
            "from": {"id": 42, "is_bot": False, "first_name": "Tester"},
            "chat_instance": "chat-instance",
            "data": data,
            "message": {
                "message_id": message_id,
                "date": int(datetime.now().timestamp()),
                "chat": {"id": 42, "type": "private"},
                "text": "Карточка поезда",
            },
        },
        context={"bot": bot},
    )


async def test_tracking_can_be_created_from_several_messages_of_one_search(
    settings, database
):
    async with database.session() as session:
        await repo.upsert_user(session, 42)

    bot = make_bot()
    storage = MemoryStorage()
    state = FSMContext(storage, StorageKey(bot_id=bot.id, chat_id=42, user_id=42))
    await state.set_state(SearchFlow.results)
    await state.update_data(
        search_id="cafebabe",
        travel_date="2026-12-01",
        from_station="Минск",
        to_station="Москва",
        passengers=2,
        trains=[Train("712Б", "12:34"), Train("003А", "18:05")],
    )

    class ManagerStub:
        def __init__(self) -> None:
            self.added: list[int] = []

        def add(self, tracking_id: int) -> None:
            self.added.append(tracking_id)

    manager = ManagerStub()
    await search.create_tracking(
        make_callback(bot, "trk:cafebabe:0", message_id=5),
        state,
        database,
        settings,
        manager,
    )
    await search.create_tracking(
        make_callback(bot, "trk:cafebabe:1", message_id=6),
        state,
        database,
        settings,
        manager,
    )

    assert len(manager.added) == 2
    async with database.session() as session:
        trackings = await repo.list_user_trackings(session, 42)
    assert sorted(item.train_number for item in trackings) == ["003А", "712Б"]
    assert (await state.get_data())["search_id"] == "cafebabe"


async def test_duplicate_is_checked_before_tracking_limit(settings, database):
    settings.max_trackings_per_user = 1
    async with database.session() as session:
        await repo.upsert_user(session, 42)
        existing = await repo.create_tracking(
            session,
            chat_id=42,
            from_station="Минск",
            to_station="Москва",
            travel_date=date(2026, 12, 1),
            passengers=2,
            train_number="712Б",
            depart_time="12:34",
            notify_threshold=None,
            carriage_filter=None,
            max_price=None,
            stop_on_success=True,
        )
        existing_id = existing.id

    bot = make_bot()
    storage = MemoryStorage()
    state = FSMContext(storage, StorageKey(bot_id=bot.id, chat_id=42, user_id=42))
    await state.set_state(SearchFlow.results)
    await state.update_data(
        search_id="cafebabe",
        travel_date="2026-12-01",
        from_station="Минск",
        to_station="Москва",
        passengers=2,
        trains=[Train("712Б", "12:34")],
    )

    class ManagerStub:
        def __init__(self) -> None:
            self.added: list[int] = []

        def add(self, tracking_id: int) -> None:
            self.added.append(tracking_id)

    manager = ManagerStub()
    callback = CallbackQuery.model_validate(
        {
            "id": "cb-2",
            "from": {"id": 42, "is_bot": False, "first_name": "Tester"},
            "chat_instance": "chat-instance",
            "data": "trk:cafebabe:0",
            "message": {
                "message_id": 6,
                "date": int(datetime.now().timestamp()),
                "chat": {"id": 42, "type": "private"},
                "text": "Карточка поезда",
            },
        },
        context={"bot": bot},
    )

    await search.create_tracking(callback, state, database, settings, manager)

    answers = [
        request
        for request in bot.session.requests
        if isinstance(request, AnswerCallbackQuery)
    ]
    assert answers and "Уже отслеживается" in answers[-1].text
    assert manager.added == [existing_id]
    async with database.session() as session:
        assert await repo.count_user_trackings(session, 42) == 1
    # Контекст поиска сохранён: другие карточки этого поиска продолжают работать.
    assert (await state.get_data())["search_id"] == "cafebabe"
