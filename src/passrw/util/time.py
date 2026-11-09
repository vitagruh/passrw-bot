"""Работа со временем: в БД храним наивный UTC, наружу показываем локальное время."""

from __future__ import annotations

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

MSK = ZoneInfo("Europe/Minsk")


def utcnow() -> datetime:
    """Наивный UTC — единый формат для SQLite."""
    return datetime.now(UTC).replace(tzinfo=None)


def local_now(tz: ZoneInfo = MSK) -> datetime:
    return datetime.now(tz)


def as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def fmt_local(value: datetime | str | None, tz: ZoneInfo = MSK, fmt: str = "%d.%m.%Y %H:%M") -> str:
    if value is None:
        return "—"
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return value
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(tz).strftime(fmt)


def days_until(travel_date: date, today: date | None = None) -> int:
    return (travel_date - (today or date.today())).days
