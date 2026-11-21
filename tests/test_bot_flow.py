"""Сценарии поиска в боте: причины пустой выдачи и search_id в кнопках."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from passrw.bot.flow import perform_search
from passrw.db import repo
from passrw.domain.railway import Carriage, Train
from passrw.services.manager import TrackingManager
from passrw.services.parser import EmptyReason, LayoutChanged, RouteResult
from passrw.util.time import local_now

TRAVEL_DATE = date(2026, 12, 1)


class RecordingBot:
    def __init__(self) -> None:
        self.messages: list[tuple[int, str, dict]] = []

    async def send_message(self, chat_id: int, text: str, **kwargs) -> None:
        self.messages.append((chat_id, text, kwargs))


class StubClient:
    def __init__(self, *, result: RouteResult | None = None, error: Exception | None = None):
        self.result = result
        self.error = error

    async def search(self, from_station: str, to_station: str, travel_date: date):
        if self.error is not None:
            raise self.error
        assert self.result is not None
        return self.result


async def prepare_user(database, chat_id: int = 50) -> None:
    async with database.session() as session:
        await repo.upsert_user(session, chat_id)


@pytest.mark.parametrize(
    ("reason", "fragment"),
    [
        (EmptyReason.STATION_NOT_FOUND, "Станция не найдена"),
        (EmptyReason.NO_DIRECT_CONNECTION, "нет прямого сообщения"),
        (EmptyReason.OUT_OF_SALE_WINDOW, "ещё не открыта"),
        (EmptyReason.NO_TRAINS, "поездов"),
    ],
)
async def test_perform_search_explains_empty_result(database, reason, fragment):
    await prepare_user(database)
    bot = RecordingBot()
    client = StubClient(result=RouteResult(trains=[], reason=reason))

    result = await perform_search(
        bot, database, client, 50, "Минск", "Москва", TRAVEL_DATE, 1, "cafebabe"
    )

    assert result == []
    assert fragment in bot.messages[-1][1]


async def test_perform_search_reports_layout_change(database):
    await prepare_user(database)
    bot = RecordingBot()
    client = StubClient(error=LayoutChanged("no table"))

    result = await perform_search(
        bot, database, client, 50, "Минск", "Москва", TRAVEL_DATE, 1, "cafebabe"
    )

    assert result is None
    assert "вёрстку" in bot.messages[-1][1]


async def test_train_list_buttons_carry_search_id(database):
    await prepare_user(database)
    train = Train("712Б", "12:34", carriages=(Carriage("Купе", 3, 50.0),))
    bot = RecordingBot()
    client = StubClient(result=RouteResult(trains=[train]))

    result = await perform_search(
        bot, database, client, 50, "Минск", "Москва", TRAVEL_DATE, 2, "deadbeef"
    )

    assert result == [train]
    keyboard = bot.messages[-1][2]["reply_markup"]
    callbacks = [
        button.callback_data
        for row in keyboard.inline_keyboard
        for button in row
        if button.callback_data
    ]
    assert "tr:deadbeef:0" in callbacks
    assert all(len(value.encode("utf-8")) <= 64 for value in callbacks)


async def test_manager_expire_closes_past_tracking(database, settings):
    expired_date = local_now(settings.timezone).date() - timedelta(days=1)
    async with database.session() as session:
        await repo.upsert_user(session, 51)
        expired = await repo.create_tracking(
            session,
            chat_id=51,
            from_station="Минск",
            to_station="Брест",
            travel_date=expired_date,
            passengers=1,
            train_number="001А",
            depart_time="10:00",
            notify_threshold=1,
            carriage_filter=None,
            max_price=None,
            stop_on_success=True,
        )
        future = await repo.create_tracking(
            session,
            chat_id=51,
            from_station="Минск",
            to_station="Гомель",
            travel_date=expired_date + timedelta(days=10),
            passengers=1,
            train_number="002Б",
            depart_time="12:00",
            notify_threshold=1,
            carriage_filter=None,
            max_price=None,
            stop_on_success=True,
        )

    bot = RecordingBot()
    manager = TrackingManager(bot, database, settings, client=None)

    assert await manager.expire() == 1

    async with database.session() as session:
        assert (await repo.get_tracking(session, expired.id)).is_active is False
        assert (await repo.get_tracking(session, future.id)).is_active is True
    assert any("дата поездки прошла" in text for _, text, _ in bot.messages)
