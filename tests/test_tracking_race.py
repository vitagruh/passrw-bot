"""Конкурентное создание трекинга: лок не даёт создать дубль."""

from __future__ import annotations

import asyncio
from datetime import date

from passrw.bot.routers import search as search_router
from passrw.db import repo


async def test_concurrent_tracking_creation_creates_one_row(
    database, settings, monkeypatch
):
    async with database.session() as session:
        await repo.upsert_user(session, 77)

    original_list = repo.list_user_trackings
    original_create = repo.create_tracking

    async def slow_list(session, chat_id):
        await asyncio.sleep(0.02)
        return await original_list(session, chat_id)

    async def slow_create(session, **kwargs):
        await asyncio.sleep(0.02)
        return await original_create(session, **kwargs)

    monkeypatch.setattr(repo, "list_user_trackings", slow_list)
    monkeypatch.setattr(repo, "create_tracking", slow_create)

    async def create():
        return await search_router._get_or_create_tracking(
            database,
            settings,
            chat_id=77,
            from_station="Минск",
            to_station="Брест",
            travel_date=date(2026, 12, 1),
            passengers=1,
            train_number="001А",
            depart_time="10:00",
        )

    results = await asyncio.gather(create(), create())

    async with database.session() as session:
        trackings = await repo.list_user_trackings(session, 77)

    assert len(trackings) == 1
    assert sorted(created for _, created, _ in results) == [False, True]
