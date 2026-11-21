"""Тесты репозитория на реальной временной SQLite."""

from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy import update

from passrw.db import repo
from passrw.db.models import UserLog
from passrw.util.time import utcnow

TOMORROW = date.today() + timedelta(days=1)
YESTERDAY = date.today() - timedelta(days=1)


async def make_tracking(session, chat_id=1, *, travel_date=TOMORROW, train_number="712Б"):
    return await repo.create_tracking(
        session,
        chat_id=chat_id,
        from_station="Минск",
        to_station="Москва",
        travel_date=travel_date,
        passengers=1,
        train_number=train_number,
        depart_time="12:34",
        notify_threshold=1,
        carriage_filter=None,
        max_price=None,
        stop_on_success=True,
    )


async def test_user_lifecycle_and_blocking(database):
    async with database.session() as session:
        user = await repo.upsert_user(session, 10, username="alice", first_name="Alice")
        assert user.chat_id == 10

    async with database.session() as session:
        await repo.upsert_user(session, 10, last_name="Smith")
        user = await repo.get_user(session, 10)
        assert (user.username, user.first_name, user.last_name) == ("alice", "Alice", "Smith")

        await repo.upsert_user(session, 11, username="bob")
        await repo.set_user_blocked(session, 11, True)

    async with database.session() as session:
        assert await repo.count_users(session) == 2
        assert await repo.count_users(session, blocked=True) == 1
        assert await repo.list_chat_ids(session) == [10]
        assert sorted(await repo.list_chat_ids(session, include_blocked=True)) == [10, 11]

        rows, total = await repo.list_users(session, query="ali")
        assert total == 1
        assert rows[0][0].chat_id == 10


async def test_tracking_dedupe_deactivation_and_expiry(database):
    async with database.session() as session:
        await repo.upsert_user(session, 1)
        first = await make_tracking(session)
        duplicate = await make_tracking(session)
        assert duplicate.id == first.id
        assert await repo.count_user_trackings(session, 1) == 1

    async with database.session() as session:
        await repo.deactivate_tracking(session, first.id, "user_stop")
        assert await repo.count_user_trackings(session, 1) == 0
        assert len(await repo.list_user_trackings(session, 1, active_only=False)) == 1

        await make_tracking(session, travel_date=YESTERDAY, train_number="003А")
        assert await repo.expire_past_trackings(session) == 1

    async with database.session() as session:
        active = await repo.list_active_trackings(session)
        assert active == []


async def test_samples_are_stored_only_on_change(database):
    async with database.session() as session:
        await repo.upsert_user(session, 2)
        tracking = await make_tracking(session, chat_id=2)

        assert await repo.add_sample_if_changed(session, tracking, seats=5, min_price=40.0)
        assert not await repo.add_sample_if_changed(session, tracking, seats=5, min_price=40.0)
        assert await repo.add_sample_if_changed(session, tracking, seats=6, min_price=40.0)
        assert await repo.add_sample_if_changed(session, tracking, seats=6, min_price=None)

    async with database.session() as session:
        samples = await repo.list_samples(session, tracking.id)
        assert [(s.seats, s.min_price) for s in samples] == [
            (5, 40.0),
            (6, 40.0),
            (6, None),
        ]
        assert (await repo.samples_per_day(session)) == [(utcnow().date().isoformat(), 3)]


async def test_favorites_upsert_and_ownership(database):
    async with database.session() as session:
        await repo.upsert_user(session, 3)
        await repo.add_favorite(session, 3, "Минск", "Брест", 1)
        await repo.add_favorite(session, 3, "Минск", "Брест", 4)

    async with database.session() as session:
        favorites = await repo.list_favorites(session, 3)
        assert len(favorites) == 1
        assert favorites[0].passengers == 4

        favorite_id = favorites[0].id
        assert not await repo.delete_favorite(session, favorite_id, chat_id=999)
        assert await repo.delete_favorite(session, favorite_id, chat_id=3)


async def test_dashboard_counts_and_cleanup(database):
    async with database.session() as session:
        await repo.upsert_user(session, 4, username="carol")
        tracking = await make_tracking(session, chat_id=4)
        await repo.add_sample_if_changed(session, tracking, seats=3, min_price=25.0)
        await repo.log_bot_error(session, "parser", "boom")
        await repo.log_user_event(session, 4, "NOTIFY", "tracking=1")
        await repo.bump_stats(session, 4, notifications=2)
        old = utcnow() - timedelta(days=90)
        await session.execute(update(UserLog).values(created_at=old))

    async with database.session() as session:
        counts = await repo.dashboard_counts(session)
        assert counts["users"] == 1
        assert counts["active_trackings"] == 1
        assert counts["notifications"] == 2
        assert counts["errors_24h"] == 1

        assert await repo.cleanup_old_data(session, retention_days=30) == 1
        assert await repo.list_user_logs(session) == []


async def test_settings_and_station_suggestions(database):
    async with database.session() as session:
        await repo.upsert_user(session, 5)
        row = await repo.save_user_settings(session, 5, notify_threshold=3, max_price=50.0)
        assert row.notify_threshold == 3
        assert row.max_price == 50.0

        await repo.bump_station_usage(session, "Минск", "Минск", "Брест")

    async with database.session() as session:
        assert await repo.suggest_stations(session, "мин") == ["Минск"]
        assert await repo.suggest_stations(session, "м") == []
        top = await repo.top_stations(session, 2)
        assert [station.name for station in top] == ["Минск", "Брест"]


async def test_search_history_isolated_by_chat(database):
    async with database.session() as session:
        await repo.upsert_user(session, 6)
        await repo.upsert_user(session, 7)
        await repo.add_search_history(
            session,
            chat_id=6,
            from_station="Минск",
            to_station="Гомель",
            travel_date=TOMORROW,
            passengers=2,
        )

    async with database.session() as session:
        history = await repo.recent_searches(session, 6)
        assert len(history) == 1
        assert await repo.get_search(session, history[0].id, chat_id=7) is None
        assert await repo.get_search(session, history[0].id, chat_id=6) is not None


async def test_toggle_favorite_adds_and_removes(database):
    async with database.session() as session:
        await repo.upsert_user(session, 20)
        assert await repo.toggle_favorite(session, 20, "Минск", "Брест", 2) is True

    async with database.session() as session:
        favorites = await repo.list_favorites(session, 20)
        assert [(f.from_station, f.passengers) for f in favorites] == [("Минск", 2)]
        assert await repo.toggle_favorite(session, 20, "Минск", "Брест", 2) is False

    async with database.session() as session:
        assert await repo.list_favorites(session, 20) == []


async def test_admin_session_epoch_tracks_fingerprint_and_logout(database):
    async with database.session() as session:
        assert await repo.ensure_admin_session_epoch(session, "fingerprint-a") == 1
        assert await repo.ensure_admin_session_epoch(session, "fingerprint-a") == 1
        assert await repo.ensure_admin_session_epoch(session, "fingerprint-b") == 2
        assert await repo.bump_admin_session_epoch(session) == 3


async def test_dashboard_reports_findings_and_check_errors(database):
    async with database.session() as session:
        await repo.upsert_user(session, 30)
        tracking = await make_tracking(session, chat_id=30)
        await repo.add_sample_if_changed(session, tracking, seats=4, min_price=10.0)
        await repo.update_tracking(session, tracking.id, last_checked_at=utcnow())

    async with database.session() as session:
        counts = await repo.dashboard_counts(session)
        assert counts["success_24h"] == 1
        assert counts["last_success_at"] is not None
        assert counts["errors_24h"] == 0

        await repo.update_tracking(
            session,
            tracking.id,
            last_error="site: timeout",
            last_checked_at=utcnow(),
        )

    async with database.session() as session:
        counts = await repo.dashboard_counts(session)
        assert counts["errors_24h"] == 1
        assert counts["last_success_at"] is None
