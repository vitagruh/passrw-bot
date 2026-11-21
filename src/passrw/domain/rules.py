"""Правила ввода, тихих часов и расписания проверок."""

from __future__ import annotations

import random
import re
from datetime import date, datetime, timedelta

STATION_RE = re.compile(r"^[A-Za-zА-Яа-яЁё0-9\s\-.'()]+$")
QUIET_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)\s*-\s*([01]\d|2[0-3]):([0-5]\d)$")

DEFAULT_STATIONS = (
    "Минск",
    "Москва",
    "Санкт-Петербург",
    "Гомель",
    "Брест",
    "Витебск",
    "Гродно",
    "Могилёв",
    "Калининград",
    "Смоленск",
)

CARRIAGE_FILTERS: dict[str, str | None] = {
    "any": None,
    "plaz": "плацкарт",
    "kupe": "купе",
    "sv": "св",
    "sid": "сидяч",
    "lux": "люкс",
}

CARRIAGE_LABELS: dict[str, str] = {
    "any": "любой",
    "plaz": "плацкарт",
    "kupe": "купе",
    "sv": "СВ",
    "sid": "сидячий",
    "lux": "люкс",
}


def clean_text(value: str | None, limit: int = 100) -> str:
    if not value:
        return ""
    # Управляющие символы не нужны ни станциям, ни фильтрам.
    return "".join(ch for ch in value if ch.isprintable()).strip()[:limit]


def validate_station(value: str) -> bool:
    text = value.strip()
    return 2 <= len(text) <= 64 and bool(STATION_RE.match(text))


def parse_quiet_hours(value: str) -> tuple[str | None, str | None] | None:
    """'22:00-08:00' -> пара; 'off' -> (None, None); неверный формат -> None."""
    cleaned = (value or "").strip().lower()
    if cleaned in {"off", "выкл", "нет", "-", "0"}:
        return None, None
    match = QUIET_RE.match(cleaned)
    if not match:
        return None
    start = f"{match.group(1)}:{match.group(2)}"
    end = f"{match.group(3)}:{match.group(4)}"
    return (None, None) if start == end else (start, end)


def in_quiet_hours(
    quiet_from: str | None, quiet_to: str | None, now: datetime
) -> bool:
    if not quiet_from or not quiet_to:
        return False
    try:
        start_h, start_m = (int(part) for part in quiet_from.split(":"))
        end_h, end_m = (int(part) for part in quiet_to.split(":"))
    except (ValueError, TypeError):
        return False
    current = now.hour * 60 + now.minute
    start = start_h * 60 + start_m
    end = end_h * 60 + end_m
    if start == end:
        return False
    if start < end:
        return start <= current < end
    return current >= start or current < end


def seconds_until_quiet_end(quiet_to: str, now: datetime) -> int:
    """Сколько секунд осталось до конца тихих часов (минимум 60)."""
    try:
        hour, minute = (int(part) for part in quiet_to.split(":"))
    except (ValueError, TypeError):
        return 60
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return max(60, int((target - now).total_seconds()))


def adaptive_interval(base: int, minimum: int, travel_date: date, today: date | None = None) -> int:
    """Ближе к поездке опрашиваем чаще; добавляем джиттер против синхронных всплесков."""
    days_left = (travel_date - (today or date.today())).days
    if days_left <= 1:
        interval = max(minimum, base // 3)
    elif days_left <= 3:
        interval = max(minimum, base // 2)
    else:
        interval = base
    jitter = random.randint(0, max(1, interval // 10))
    return interval + jitter
