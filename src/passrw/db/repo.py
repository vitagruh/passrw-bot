"""Репозитории: единственное место, где выполняются SQL-запросы."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from passrw.db.models import (
    AdminLog,
    AppMeta,
    AvailabilitySample,
    BotError,
    FavoriteRoute,
    PopularStation,
    SearchHistory,
    Tracking,
    User,
    UserLog,
    UserSettings,
    UserStats,
)
from passrw.domain.rules import DEFAULT_STATIONS
from passrw.util.time import utcnow

# ── Пользователи ───────────────────────────────────────────────


async def upsert_user(
    session: AsyncSession,
    chat_id: int,
    username: str | None = None,
    first_name: str | None = None,
    last_name: str | None = None,
) -> User:
    user = await session.get(User, chat_id)
    if user is None:
        user = User(chat_id=chat_id, username=username, first_name=first_name, last_name=last_name)
        session.add(user)
        return user
    user.username = username or user.username
    user.first_name = first_name or user.first_name
    user.last_name = last_name or user.last_name
    user.last_active = utcnow()
    return user


async def get_user(session: AsyncSession, chat_id: int) -> User | None:
    return await session.get(User, chat_id)


async def set_user_role(session: AsyncSession, chat_id: int, role: str) -> None:
    await session.execute(update(User).where(User.chat_id == chat_id).values(role=role))


async def set_user_blocked(session: AsyncSession, chat_id: int, blocked: bool) -> None:
    await session.execute(update(User).where(User.chat_id == chat_id).values(is_blocked=blocked))


async def list_users(
    session: AsyncSession, offset: int = 0, limit: int = 50, query: str = ""
) -> tuple[list[Any], int]:
    active_count = (
        select(func.count(Tracking.id))
        .where(Tracking.chat_id == User.chat_id, Tracking.is_active.is_(True))
        .correlate(User)
        .scalar_subquery()
    )
    stmt = select(User, active_count.label("active_trackings"))
    total_stmt = select(func.count(User.chat_id))
    if query:
        pattern = f"%{query.strip()}%"
        condition = or_(
            User.username.ilike(pattern),
            User.first_name.ilike(pattern),
            User.last_name.ilike(pattern),
        )
        stmt = stmt.where(condition)
        total_stmt = total_stmt.where(condition)
    total = (await session.execute(total_stmt)).scalar_one()
    rows = (
        await session.execute(
            stmt.order_by(User.created_at.desc()).offset(offset).limit(limit)
        )
    ).all()
    return list(rows), total


async def list_chat_ids(session: AsyncSession, include_blocked: bool = False) -> list[int]:
    stmt = select(User.chat_id)
    if not include_blocked:
        stmt = stmt.where(User.is_blocked.is_(False))
    return list((await session.execute(stmt)).scalars())


async def count_users(session: AsyncSession, blocked: bool | None = None) -> int:
    stmt = select(func.count(User.chat_id))
    if blocked is not None:
        stmt = stmt.where(User.is_blocked.is_(blocked))
    return (await session.execute(stmt)).scalar_one()


# ── Трекинги ───────────────────────────────────────────────────


async def create_tracking(
    session: AsyncSession,
    *,
    chat_id: int,
    from_station: str,
    to_station: str,
    travel_date: date,
    passengers: int,
    train_number: str,
    depart_time: str,
    notify_threshold: int,
    carriage_filter: str | None,
    max_price: float | None,
    stop_on_success: bool,
    heartbeat_enabled: bool = False,
    heartbeat_interval: int = 1800,
) -> Tracking:
    duplicate = (
        await session.execute(
            select(Tracking).where(
                Tracking.chat_id == chat_id,
                Tracking.is_active.is_(True),
                Tracking.from_station == from_station,
                Tracking.to_station == to_station,
                Tracking.travel_date == travel_date,
                Tracking.depart_time == depart_time,
                Tracking.train_number == train_number,
            )
        )
    ).scalar_one_or_none()
    if duplicate:
        return duplicate

    tracking = Tracking(
        chat_id=chat_id,
        from_station=from_station,
        to_station=to_station,
        travel_date=travel_date,
        passengers=passengers,
        train_number=train_number,
        depart_time=depart_time,
        notify_threshold=notify_threshold,
        carriage_filter=carriage_filter,
        max_price=max_price,
        stop_on_success=stop_on_success,
        heartbeat_enabled=heartbeat_enabled,
        heartbeat_interval=heartbeat_interval,
    )
    session.add(tracking)
    await session.flush()
    return tracking


async def get_tracking(session: AsyncSession, tracking_id: int) -> Tracking | None:
    return await session.get(Tracking, tracking_id)


async def get_tracking_with_user(session: AsyncSession, tracking_id: int) -> Tracking | None:
    return (
        await session.execute(
            select(Tracking)
            .options(selectinload(Tracking.user))
            .where(Tracking.id == tracking_id)
        )
    ).scalar_one_or_none()


async def list_user_trackings(
    session: AsyncSession, chat_id: int, active_only: bool = True
) -> list[Tracking]:
    stmt = select(Tracking).where(Tracking.chat_id == chat_id)
    if active_only:
        stmt = stmt.where(Tracking.is_active.is_(True))
    stmt = stmt.order_by(Tracking.created_at.desc())
    return list((await session.execute(stmt)).scalars())


async def count_user_trackings(session: AsyncSession, chat_id: int) -> int:
    return (
        await session.execute(
            select(func.count(Tracking.id)).where(
                Tracking.chat_id == chat_id, Tracking.is_active.is_(True)
            )
        )
    ).scalar_one()


async def count_active_trackings(session: AsyncSession) -> int:
    return (
        await session.execute(
            select(func.count(Tracking.id)).where(Tracking.is_active.is_(True))
        )
    ).scalar_one()


async def last_check_time(session: AsyncSession) -> datetime | None:
    return (
        await session.execute(select(func.max(Tracking.last_checked_at)))
    ).scalar_one()


async def list_active_trackings(session: AsyncSession) -> list[Tracking]:
    return list(
        (
            await session.execute(
                select(Tracking).where(Tracking.is_active.is_(True)).order_by(Tracking.id)
            )
        ).scalars()
    )


async def list_trackings(
    session: AsyncSession,
    *,
    active: bool | None = True,
    query: str = "",
    offset: int = 0,
    limit: int = 50,
) -> tuple[list[Tracking], int]:
    stmt = select(Tracking).options(selectinload(Tracking.user))
    count_stmt = select(func.count(Tracking.id))
    conditions = []
    if active is not None:
        conditions.append(Tracking.is_active.is_(active))
    if query:
        pattern = f"%{query.strip()}%"
        conditions.append(
            or_(
                Tracking.from_station.ilike(pattern),
                Tracking.to_station.ilike(pattern),
                Tracking.train_number.ilike(pattern),
            )
        )
    for condition in conditions:
        stmt = stmt.where(condition)
        count_stmt = count_stmt.where(condition)
    total = (await session.execute(count_stmt)).scalar_one()
    rows = (
        await session.execute(
            stmt.order_by(Tracking.is_active.desc(), Tracking.created_at.desc())
            .offset(offset)
            .limit(limit)
        )
    ).scalars()
    return list(rows), total


async def update_tracking(session: AsyncSession, tracking_id: int, **fields: Any) -> None:
    if not fields:
        return
    fields["updated_at"] = utcnow()
    await session.execute(update(Tracking).where(Tracking.id == tracking_id).values(**fields))


async def delete_tracking(session: AsyncSession, tracking_id: int) -> bool:
    result = await session.execute(delete(Tracking).where(Tracking.id == tracking_id))
    return bool(result.rowcount)


async def deactivate_tracking(session: AsyncSession, tracking_id: int, reason: str) -> None:
    await session.execute(
        update(Tracking)
        .where(Tracking.id == tracking_id)
        .values(
            is_active=False,
            stop_reason=reason[:32],
            stopped_at=utcnow(),
            updated_at=utcnow(),
        )
    )


async def add_sample_if_changed(
    session: AsyncSession,
    tracking: Tracking,
    *,
    seats: int,
    min_price: float | None,
) -> bool:
    last = (
        await session.execute(
            select(AvailabilitySample)
            .where(AvailabilitySample.tracking_id == tracking.id)
            .order_by(AvailabilitySample.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()

    if last is not None:
        same_seats = last.seats == seats
        if last.min_price is None or min_price is None:
            same_price = last.min_price is None and min_price is None
        else:
            same_price = abs(last.min_price - min_price) < 0.01
        if same_seats and same_price:
            return False

    session.add(
        AvailabilitySample(
            tracking_id=tracking.id,
            chat_id=tracking.chat_id,
            train_number=tracking.train_number,
            seats=seats,
            min_price=min_price,
        )
    )
    return True


async def list_samples(session: AsyncSession, tracking_id: int, limit: int = 50) -> list[AvailabilitySample]:
    rows = (
        await session.execute(
            select(AvailabilitySample)
            .where(AvailabilitySample.tracking_id == tracking_id)
            .order_by(AvailabilitySample.id.desc())
            .limit(limit)
        )
    ).scalars()
    return list(reversed(list(rows)))


async def samples_per_day(session: AsyncSession, days: int = 7) -> list[tuple[str, int]]:
    since = utcnow() - timedelta(days=days)
    day = func.date(AvailabilitySample.checked_at)
    rows = (
        await session.execute(
            select(day.label("day"), func.count(AvailabilitySample.id))
            .where(AvailabilitySample.checked_at >= since)
            .group_by(day)
            .order_by(day)
        )
    ).all()
    return [(str(item[0]), int(item[1])) for item in rows]


async def expire_past_trackings(session: AsyncSession, today: date | None = None) -> int:
    today = today or date.today()
    result = await session.execute(
        update(Tracking)
        .where(Tracking.is_active.is_(True), Tracking.travel_date < today)
        .values(
            is_active=False,
            stop_reason="expired",
            stopped_at=utcnow(),
            updated_at=utcnow(),
        )
    )
    return result.rowcount or 0


# ── История поиска и избранное ─────────────────────────────────


async def add_search_history(
    session: AsyncSession,
    *,
    chat_id: int,
    from_station: str,
    to_station: str,
    travel_date: date,
    passengers: int,
) -> None:
    session.add(
        SearchHistory(
            chat_id=chat_id,
            from_station=from_station,
            to_station=to_station,
            travel_date=travel_date,
            passengers=passengers,
        )
    )


async def recent_searches(session: AsyncSession, chat_id: int, limit: int = 5) -> list[SearchHistory]:
    return list(
        (
            await session.execute(
                select(SearchHistory)
                .where(SearchHistory.chat_id == chat_id)
                .order_by(SearchHistory.id.desc())
                .limit(limit)
            )
        ).scalars()
    )


async def get_search(session: AsyncSession, history_id: int, chat_id: int) -> SearchHistory | None:
    return (
        await session.execute(
            select(SearchHistory).where(
                SearchHistory.id == history_id, SearchHistory.chat_id == chat_id
            )
        )
    ).scalar_one_or_none()


async def add_favorite(
    session: AsyncSession, chat_id: int, from_station: str, to_station: str, passengers: int
) -> None:
    exists = (
        await session.execute(
            select(FavoriteRoute).where(
                FavoriteRoute.chat_id == chat_id,
                FavoriteRoute.from_station == from_station,
                FavoriteRoute.to_station == to_station,
            )
        )
    ).scalar_one_or_none()
    if exists:
        exists.passengers = passengers
        return
    session.add(
        FavoriteRoute(
            chat_id=chat_id,
            from_station=from_station,
            to_station=to_station,
            passengers=passengers,
        )
    )


async def toggle_favorite(
    session: AsyncSession, chat_id: int, from_station: str, to_station: str, passengers: int
) -> bool:
    """Добавляет маршрут в избранное или удаляет его. True — добавлен."""
    exists = (
        await session.execute(
            select(FavoriteRoute).where(
                FavoriteRoute.chat_id == chat_id,
                FavoriteRoute.from_station == from_station,
                FavoriteRoute.to_station == to_station,
            )
        )
    ).scalar_one_or_none()
    if exists is not None:
        await session.delete(exists)
        return False
    session.add(
        FavoriteRoute(
            chat_id=chat_id,
            from_station=from_station,
            to_station=to_station,
            passengers=passengers,
        )
    )
    return True


async def list_favorites(session: AsyncSession, chat_id: int) -> list[FavoriteRoute]:
    return list(
        (
            await session.execute(
                select(FavoriteRoute)
                .where(FavoriteRoute.chat_id == chat_id)
                .order_by(FavoriteRoute.created_at.desc())
            )
        ).scalars()
    )


async def get_favorite(session: AsyncSession, favorite_id: int, chat_id: int) -> FavoriteRoute | None:
    return (
        await session.execute(
            select(FavoriteRoute).where(
                FavoriteRoute.id == favorite_id, FavoriteRoute.chat_id == chat_id
            )
        )
    ).scalar_one_or_none()


async def delete_favorite(session: AsyncSession, favorite_id: int, chat_id: int) -> bool:
    result = await session.execute(
        delete(FavoriteRoute).where(
            FavoriteRoute.id == favorite_id, FavoriteRoute.chat_id == chat_id
        )
    )
    return bool(result.rowcount)


# ── Настройки и статистика ─────────────────────────────────────


async def get_user_settings(session: AsyncSession, chat_id: int) -> UserSettings | None:
    return await session.get(UserSettings, chat_id)


async def ensure_user_settings(session: AsyncSession, chat_id: int) -> UserSettings:
    row = await session.get(UserSettings, chat_id)
    if row is None:
        row = UserSettings(chat_id=chat_id)
        session.add(row)
        await session.flush()
    return row


async def save_user_settings(session: AsyncSession, chat_id: int, **fields: Any) -> UserSettings:
    row = await ensure_user_settings(session, chat_id)
    for key, value in fields.items():
        if hasattr(row, key):
            setattr(row, key, value)
    row.updated_at = utcnow()
    return row


async def bump_stats(
    session: AsyncSession,
    chat_id: int,
    *,
    searches: int = 0,
    trackings: int = 0,
    notifications: int = 0,
) -> None:
    row = await session.get(UserStats, chat_id)
    if row is None:
        row = UserStats(chat_id=chat_id)
        session.add(row)
    row.total_searches = (row.total_searches or 0) + searches
    row.trackings_created = (row.trackings_created or 0) + trackings
    row.notifications_sent = (row.notifications_sent or 0) + notifications
    row.updated_at = utcnow()


async def get_stats(session: AsyncSession, chat_id: int) -> UserStats | None:
    return await session.get(UserStats, chat_id)


async def bump_station_usage(session: AsyncSession, *names: str) -> None:
    for name in names:
        station = await session.get(PopularStation, name)
        if station:
            station.usage_count += 1
            station.last_used = utcnow()
        else:
            session.add(PopularStation(name=name))


async def suggest_stations(
    session: AsyncSession, query: str, limit: int = 5
) -> list[str]:
    text = (query or "").strip().casefold()
    if len(text) < 2:
        return []
    names = list(
        (
            await session.execute(
                select(PopularStation.name).order_by(PopularStation.usage_count.desc()).limit(300)
            )
        ).scalars()
    )
    result: list[str] = []
    for name in [*names, *DEFAULT_STATIONS]:
        if text in name.casefold() and name not in result:
            result.append(name)
            if len(result) >= limit:
                break
    if any(name.casefold() == text for name in result):
        return []
    return result


async def top_stations(session: AsyncSession, limit: int = 5) -> list[PopularStation]:
    return list(
        (
            await session.execute(
                select(PopularStation)
                .order_by(PopularStation.usage_count.desc(), PopularStation.name)
                .limit(limit)
            )
        ).scalars()
    )


async def popular_stations(session: AsyncSession, limit: int = 5) -> list[str]:
    names = list(
        (
            await session.execute(
                select(PopularStation.name).order_by(PopularStation.usage_count.desc()).limit(limit)
            )
        ).scalars()
    )
    for name in DEFAULT_STATIONS:
        if len(names) >= limit:
            break
        if name not in names:
            names.append(name)
    return names[:limit]


# ── Логи и статистика для админки ──────────────────────────────


async def log_user_event(
    session: AsyncSession, chat_id: int, action: str, details: str | None = None
) -> None:
    session.add(UserLog(chat_id=chat_id, action=action[:48], details=(details or "")[:2000]))


async def log_bot_error(
    session: AsyncSession,
    error_type: str,
    message: str,
    stack_trace: str | None = None,
    chat_id: int | None = None,
) -> None:
    session.add(
        BotError(
            error_type=error_type[:64],
            message=message[:2000],
            stack_trace=(stack_trace or "")[:8000],
            chat_id=chat_id,
        )
    )


async def log_admin_action(
    session: AsyncSession,
    username: str,
    action: str,
    details: str | None = None,
    ip_address: str | None = None,
) -> None:
    session.add(
        AdminLog(
            admin_username=username[:64],
            action=action[:64],
            details=(details or "")[:2000],
            ip_address=ip_address,
        )
    )


# ── Служебные метаданные ───────────────────────────────────────

_EPOCH_KEY = "admin_session_epoch"
_FINGERPRINT_KEY = "admin_credentials_fingerprint"


async def get_meta(session: AsyncSession, key: str) -> str | None:
    row = await session.get(AppMeta, key)
    return row.value if row is not None else None


async def set_meta(session: AsyncSession, key: str, value: str) -> None:
    row = await session.get(AppMeta, key)
    if row is None:
        session.add(AppMeta(key=key, value=value))
    else:
        row.value = value


async def ensure_admin_session_epoch(session: AsyncSession, fingerprint: str) -> int:
    """Возвращает эпоху админских сессий; при смене пароля увеличивает её."""
    epoch_raw = await get_meta(session, _EPOCH_KEY)
    epoch = int(epoch_raw) if epoch_raw and epoch_raw.isdigit() else 1
    stored = await get_meta(session, _FINGERPRINT_KEY)
    if stored is None:
        await set_meta(session, _FINGERPRINT_KEY, fingerprint)
        await set_meta(session, _EPOCH_KEY, str(epoch))
        return epoch
    if stored != fingerprint:
        epoch += 1
        await set_meta(session, _FINGERPRINT_KEY, fingerprint)
        await set_meta(session, _EPOCH_KEY, str(epoch))
    return epoch


async def bump_admin_session_epoch(session: AsyncSession) -> int:
    epoch_raw = await get_meta(session, _EPOCH_KEY)
    epoch = (int(epoch_raw) if epoch_raw and epoch_raw.isdigit() else 1) + 1
    await set_meta(session, _EPOCH_KEY, str(epoch))
    return epoch


async def list_user_logs(session: AsyncSession, limit: int = 100, offset: int = 0) -> list[UserLog]:
    return list(
        (
            await session.execute(
                select(UserLog).order_by(UserLog.id.desc()).offset(offset).limit(limit)
            )
        ).scalars()
    )


async def count_user_logs(session: AsyncSession) -> int:
    return (await session.execute(select(func.count(UserLog.id)))).scalar_one()


async def count_bot_errors(session: AsyncSession) -> int:
    return (await session.execute(select(func.count(BotError.id)))).scalar_one()


async def count_admin_logs(session: AsyncSession) -> int:
    return (await session.execute(select(func.count(AdminLog.id)))).scalar_one()


async def list_bot_errors(
    session: AsyncSession, limit: int = 100, offset: int = 0
) -> list[BotError]:
    return list(
        (
            await session.execute(
                select(BotError).order_by(BotError.id.desc()).offset(offset).limit(limit)
            )
        ).scalars()
    )


async def list_admin_logs(session: AsyncSession, limit: int = 100, offset: int = 0):
    return list(
        (
            await session.execute(
                select(AdminLog).order_by(AdminLog.id.desc()).offset(offset).limit(limit)
            )
        ).scalars()
    )


async def dashboard_counts(session: AsyncSession) -> dict[str, Any]:
    since = utcnow() - timedelta(hours=24)
    users = (await session.execute(select(func.count(User.chat_id)))).scalar_one()
    active = (
        await session.execute(select(func.count(Tracking.id)).where(Tracking.is_active.is_(True)))
    ).scalar_one()
    stopped = (
        await session.execute(select(func.count(Tracking.id)).where(Tracking.is_active.is_(False)))
    ).scalar_one()
    notifications = (await session.execute(select(func.coalesce(func.sum(UserStats.notifications_sent), 0)))).scalar_one()
    # Ошибки: необработанные исключения бота + трекинги, чья последняя проверка упала.
    bot_errors = (
        await session.execute(select(func.count(BotError.id)).where(BotError.created_at >= since))
    ).scalar_one()
    check_errors = (
        await session.execute(
            select(func.count(Tracking.id)).where(
                Tracking.last_error.is_not(None), Tracking.last_checked_at >= since
            )
        )
    ).scalar_one()
    # Находки: замеры с доступными местами за сутки, независимо от STOP_ON_SUCCESS.
    findings = (
        await session.execute(
            select(func.count(AvailabilitySample.id)).where(
                AvailabilitySample.checked_at >= since, AvailabilitySample.seats > 0
            )
        )
    ).scalar_one()
    last_success = (
        await session.execute(
            select(func.max(Tracking.last_checked_at)).where(Tracking.last_error.is_(None))
        )
    ).scalar_one()
    return {
        "users": int(users),
        "active_trackings": int(active),
        "stopped_trackings": int(stopped),
        "notifications": int(notifications),
        "errors_24h": int(bot_errors) + int(check_errors),
        "success_24h": int(findings),
        "last_success_at": last_success,
    }


async def cleanup_old_data(session: AsyncSession, retention_days: int) -> int:
    cutoff = utcnow() - timedelta(days=max(1, retention_days))
    deleted = 0
    for model, column in (
        (UserLog, UserLog.created_at),
        (BotError, BotError.created_at),
        (SearchHistory, SearchHistory.searched_at),
        (AvailabilitySample, AvailabilitySample.checked_at),
    ):
        result = await session.execute(delete(model).where(column < cutoff))
        deleted += result.rowcount or 0

    result = await session.execute(delete(AdminLog).where(AdminLog.created_at < cutoff))
    deleted += result.rowcount or 0

    result = await session.execute(
        delete(Tracking).where(Tracking.is_active.is_(False), Tracking.stopped_at < cutoff)
    )
    deleted += result.rowcount or 0
    return deleted
