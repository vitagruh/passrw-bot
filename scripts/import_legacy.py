"""Импорт данных из старой SQLite-базы (ticket_bot.db) в новую схему.

Использование:
    python scripts/import_legacy.py --source data/ticket_bot.db --target data/passrw.db

Импортируются пользователи, активные/остановленные трекинги, настройки,
избранное, история поисков, статистика, популярные станции и история мест.
Существующие записи в целевой базе не перезаписываются.
"""

from __future__ import annotations

import argparse
import asyncio
import sqlite3
import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sqlalchemy import select  # noqa: E402

from passrw.db.models import (  # noqa: E402
    AvailabilitySample,
    FavoriteRoute,
    PopularStation,
    SearchHistory,
    Tracking,
    User,
    UserSettings,
    UserStats,
)
from passrw.db.session import Database  # noqa: E402


def _dt(value) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    text = str(value).strip().replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def _date(value) -> date | None:
    if value is None:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    parsed = _dt(value)
    return parsed.date() if parsed else None


def _get(row: dict, key: str, default=None):
    return row.get(key, default)


def _times(**values):
    """Отбрасывает None, чтобы не перетирать default=utcnow у NOT NULL-колонок."""
    return {key: value for key, value in values.items() if value is not None}


def _read(conn: sqlite3.Connection, table: str) -> list[dict]:
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    if not exists:
        return []
    return [dict(row) for row in conn.execute(f"SELECT * FROM {table}")]


async def import_data(source: Path, target: Path) -> dict[str, int]:
    conn = sqlite3.connect(str(source))
    conn.row_factory = sqlite3.Row
    stats = {name: 0 for name in ("users", "trackings", "settings", "favorites", "history", "samples", "stations")}

    try:
        users = _read(conn, "users")
        trackings = _read(conn, "active_trackings")
        settings_rows = _read(conn, "user_settings")
        favorites = _read(conn, "favorite_routes")
        history = _read(conn, "search_history")
        samples = _read(conn, "availability_history")
        stations = _read(conn, "popular_stations")
        user_stats = _read(conn, "user_stats")
    finally:
        conn.close()

    database = Database(f"sqlite+aiosqlite:///{target.as_posix()}")
    await database.create_all()
    tracking_map: dict[int, int] = {}

    try:
        async with database.session() as session:
            known_users: set[int] = set()

            async def ensure_user(chat_id: int) -> None:
                if chat_id in known_users or await session.get(User, chat_id) is not None:
                    known_users.add(chat_id)
                    return
                session.add(User(chat_id=chat_id))
                known_users.add(chat_id)
                stats["users"] += 1

            for row in users:
                chat_id = int(row["chat_id"])
                existing = await session.get(User, chat_id)
                if existing is not None:
                    known_users.add(chat_id)
                    continue
                session.add(
                    User(
                        chat_id=chat_id,
                        username=_get(row, "username"),
                        first_name=_get(row, "first_name"),
                        last_name=_get(row, "last_name"),
                        role=_get(row, "role") or "user",
                        **_times(
                            created_at=_dt(_get(row, "created_at")),
                            last_active=_dt(_get(row, "last_active")),
                        ),
                    )
                )
                known_users.add(chat_id)
                stats["users"] += 1
            await session.flush()

            for row in trackings:
                chat_id = int(row["chat_id"])
                travel_date = _date(row.get("date"))
                if travel_date is None:
                    continue
                await ensure_user(chat_id)
                depart_time = (row.get("train_time") or "").strip()
                train_number = (row.get("train_num") or "").strip()
                duplicate = (
                    await session.execute(
                        select(Tracking).where(
                            Tracking.chat_id == chat_id,
                            Tracking.from_station == row["from_station"],
                            Tracking.to_station == row["to_station"],
                            Tracking.travel_date == travel_date,
                            Tracking.depart_time == depart_time,
                            Tracking.train_number == train_number,
                        )
                    )
                ).scalar_one_or_none()
                if duplicate is not None:
                    tracking_map[int(row["id"])] = duplicate.id
                    continue
                active = not bool(row.get("is_stopped"))
                tracking = Tracking(
                    chat_id=chat_id,
                    from_station=row["from_station"],
                    to_station=row["to_station"],
                    travel_date=travel_date,
                    passengers=int(row.get("passengers") or 1),
                    train_number=train_number,
                    depart_time=depart_time,
                    heartbeat_enabled=bool(row.get("heartbeat_enabled")),
                    heartbeat_interval=int(row.get("heartbeat_interval") or 1800),
                    is_active=active,
                    stop_reason=None if active else "legacy_stopped",
                    stopped_at=None if active else _dt(row.get("updated_at")),
                    requests_count=int(row.get("requests_count") or 0),
                    seats_available=int(row.get("seats_available") or 0),
                    **_times(
                        created_at=_dt(row.get("created_at")),
                        updated_at=_dt(row.get("updated_at")),
                    ),
                )
                session.add(tracking)
                await session.flush()
                tracking_map[int(row["id"])] = tracking.id
                stats["trackings"] += 1

            for row in settings_rows:
                chat_id = int(row["chat_id"])
                await ensure_user(chat_id)
                if await session.get(UserSettings, chat_id) is not None:
                    continue
                session.add(
                    UserSettings(
                        chat_id=chat_id,
                        quiet_from=_get(row, "quiet_from"),
                        quiet_to=_get(row, "quiet_to"),
                        notify_threshold=int(row.get("notify_threshold") or 1),
                        default_passengers=int(row.get("default_passengers") or 1),
                        carriage_filter=_get(row, "carriage_filter"),
                        max_price=row.get("max_price"),
                        **_times(updated_at=_dt(row.get("updated_at"))),
                    )
                )
                stats["settings"] += 1

            for row in favorites:
                chat_id = int(row["chat_id"])
                await ensure_user(chat_id)
                exists = (
                    await session.execute(
                        select(FavoriteRoute).where(
                            FavoriteRoute.chat_id == chat_id,
                            FavoriteRoute.from_station == row["from_station"],
                            FavoriteRoute.to_station == row["to_station"],
                        )
                    )
                ).scalar_one_or_none()
                if exists is not None:
                    continue
                session.add(
                    FavoriteRoute(
                        chat_id=chat_id,
                        from_station=row["from_station"],
                        to_station=row["to_station"],
                        passengers=int(row.get("passengers") or 1),
                        **_times(created_at=_dt(row.get("created_at"))),
                    )
                )
                stats["favorites"] += 1

            for row in history:
                chat_id = int(row["chat_id"])
                travel_date = _date(row.get("date"))
                if travel_date is None:
                    continue
                await ensure_user(chat_id)
                session.add(
                    SearchHistory(
                        chat_id=chat_id,
                        from_station=row["from_station"],
                        to_station=row["to_station"],
                        travel_date=travel_date,
                        passengers=int(row.get("passengers") or 1),
                        **_times(searched_at=_dt(row.get("searched_at"))),
                    )
                )
                stats["history"] += 1

            for row in samples:
                new_id = tracking_map.get(int(row.get("tracking_id") or -1))
                if new_id is None:
                    continue
                session.add(
                    AvailabilitySample(
                        tracking_id=new_id,
                        chat_id=int(row["chat_id"]),
                        train_number=(row.get("train_num") or None),
                        seats=int(row.get("seats_available") or 0),
                        min_price=row.get("min_price"),
                        **_times(checked_at=_dt(row.get("checked_at"))),
                    )
                )
                stats["samples"] += 1

            for row in stations:
                name = row.get("station_name") or row.get("name")
                if not name:
                    continue
                station = await session.get(PopularStation, name)
                if station is None:
                    session.add(
                        PopularStation(
                            name=name,
                            usage_count=int(row.get("usage_count") or 1),
                            **_times(last_used=_dt(row.get("last_used"))),
                        )
                    )
                    stats["stations"] += 1
                else:
                    station.usage_count += int(row.get("usage_count") or 0)

            for row in user_stats:
                chat_id = int(row["chat_id"])
                await ensure_user(chat_id)
                if await session.get(UserStats, chat_id) is not None:
                    continue
                session.add(
                    UserStats(
                        chat_id=chat_id,
                        total_searches=int(row.get("total_searches") or 0),
                        notifications_sent=int(row.get("successful_bookings") or 0),
                    )
                )
    finally:
        await database.dispose()

    return stats


def main() -> None:
    parser = argparse.ArgumentParser(description="Импорт старой базы passrw-bot")
    parser.add_argument("--source", type=Path, required=True, help="старый ticket_bot.db")
    parser.add_argument("--target", type=Path, default=Path("data/passrw.db"), help="новая база")
    args = parser.parse_args()

    source = args.source.resolve()
    target = args.target.resolve()
    if not source.exists():
        raise SystemExit(f"Файл не найден: {source}")
    if source == target:
        raise SystemExit("Source и target не могут совпадать")
    target.parent.mkdir(parents=True, exist_ok=True)

    stats = asyncio.run(import_data(source, target))
    print("Импорт завершён:")
    for name, value in stats.items():
        print(f"  {name}: {value}")


if __name__ == "__main__":
    main()
