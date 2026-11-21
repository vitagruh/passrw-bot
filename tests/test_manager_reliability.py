"""Устойчивость менеджера: супервизор, джиттер старта, тихие часы."""

from __future__ import annotations

import asyncio
from datetime import date, timedelta

from passrw.db import repo
from passrw.services import manager as manager_module
from passrw.services.manager import TrackingManager
from passrw.services.parser import RouteResult
from passrw.util.time import local_now


class StubBot:
    def __init__(self) -> None:
        self.messages: list[tuple[int, str]] = []

    async def send_message(self, chat_id: int, text: str, **kwargs) -> None:
        self.messages.append((chat_id, text))


class EmptyClient:
    async def search(self, from_station: str, to_station: str, travel_date: date):
        return RouteResult(trains=[])


async def _add_tracking(database, chat_id: int, travel_date: date):
    async with database.session() as session:
        await repo.upsert_user(session, chat_id)
        return await repo.create_tracking(
            session,
            chat_id=chat_id,
            from_station="Минск",
            to_station="Брест",
            travel_date=travel_date,
            passengers=1,
            train_number="001А",
            depart_time="10:00",
            notify_threshold=1,
            carriage_filter=None,
            max_price=None,
            stop_on_success=True,
        )


async def test_unexpected_error_restarts_tracking_task(database, settings, monkeypatch):
    tracking = await _add_tracking(database, 78, date.today() + timedelta(days=10))
    tracker = TrackingManager(StubBot(), database, settings, EmptyClient())
    monkeypatch.setattr(tracker, "_next_delay", lambda snapshot: 0)
    monkeypatch.setattr(manager_module, "ERROR_BACKOFF_START", 0.01)
    monkeypatch.setattr(manager_module, "ERROR_BACKOFF_MAX", 0.05)

    original_update = repo.update_tracking
    calls = 0
    recovered = asyncio.Event()

    async def flaky_update(session, tracking_id, **fields):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("boom")
        result = await original_update(session, tracking_id, **fields)
        recovered.set()
        return result

    monkeypatch.setattr(repo, "update_tracking", flaky_update)
    tracker.add(tracking.id)

    await asyncio.wait_for(recovered.wait(), 2)

    assert tracker.is_running(tracking.id)
    assert calls >= 2
    await tracker.shutdown()


async def test_restore_staggers_first_checks(database, settings, monkeypatch):
    for index in range(3):
        await _add_tracking(database, 79, date.today() + timedelta(days=5 + index))

    tracker = TrackingManager(StubBot(), database, settings, EmptyClient())
    delays: list[float] = []

    def fake_add(tracking_id: int, start_delay: float = 0.0) -> None:
        delays.append(start_delay)

    monkeypatch.setattr(tracker, "add", fake_add)
    monkeypatch.setattr(manager_module.random, "uniform", lambda a, b: 0.5)

    restored = await tracker.restore()

    assert restored == 3
    assert delays == [0.5, 2.5, 4.5]
    assert max(delays) <= 30


async def test_expire_respects_quiet_hours(database, settings):
    now = local_now(settings.timezone)
    quiet_from = (now - timedelta(minutes=30)).strftime("%H:%M")
    quiet_to = (now + timedelta(minutes=30)).strftime("%H:%M")

    async with database.session() as session:
        await repo.upsert_user(session, 80)
        await repo.ensure_user_settings(session, 80)
        await repo.save_user_settings(
            session, 80, quiet_from=quiet_from, quiet_to=quiet_to
        )

    tracking = await _add_tracking(database, 80, now.date() - timedelta(days=1))
    bot = StubBot()
    tracker = TrackingManager(bot, database, settings, EmptyClient())

    assert await tracker.expire() == 1
    assert bot.messages == []

    async with database.session() as session:
        saved = await repo.get_tracking(session, tracking.id)
        assert saved is not None
        assert saved.is_active is False
