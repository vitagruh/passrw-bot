"""Тесты доменных правил и подбора вагонов."""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from passrw.domain.railway import Carriage, Filters, Train, evaluate_train, pick_train
from passrw.domain.rules import (
    adaptive_interval,
    in_quiet_hours,
    parse_quiet_hours,
    parse_travel_date,
    seconds_until_quiet_end,
    validate_station,
)

TODAY = date(2026, 11, 9)


# ── станции и даты ─────────────────────────────────────────────


@pytest.mark.parametrize(
    "value",
    ["Минск", "Санкт-Петербург", "Минск-Пассажирский", "Брест (Центральный)", "Москва 2"],
)
def test_validate_station_accepts_normal_names(value):
    assert validate_station(value)


@pytest.mark.parametrize("value", ["", "A", "Минск!", "<script>", "   "])
def test_validate_station_rejects_bad_names(value):
    assert not validate_station(value)


def test_parse_travel_date_limits():
    assert parse_travel_date("2026-11-09", TODAY) == TODAY
    assert parse_travel_date("2026-11-10", TODAY) == TODAY + timedelta(days=1)
    assert parse_travel_date("2026-11-08", TODAY) is None
    assert parse_travel_date("2027-11-10", TODAY) is None
    assert parse_travel_date("09.11.2026", TODAY) is None


# ── тихие часы ─────────────────────────────────────────────────


def test_parse_quiet_hours_variants():
    assert parse_quiet_hours("22:00-08:00") == ("22:00", "08:00")
    assert parse_quiet_hours("off") == (None, None)
    assert parse_quiet_hours("22:00-22:00") == (None, None)
    assert parse_quiet_hours("25:00-08:00") is None
    assert parse_quiet_hours("") is None


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        (datetime(2026, 11, 9, 23, 30), True),
        (datetime(2026, 11, 9, 7, 59), True),
        (datetime(2026, 11, 9, 8, 0), False),
        (datetime(2026, 11, 9, 12, 0), False),
        (datetime(2026, 11, 9, 22, 0), True),
    ],
)
def test_in_quiet_hours_overnight(now, expected):
    assert in_quiet_hours("22:00", "08:00", now) is expected


def test_in_quiet_hours_same_day_window():
    assert in_quiet_hours("13:00", "15:00", datetime(2026, 11, 9, 14, 0))
    assert not in_quiet_hours("13:00", "15:00", datetime(2026, 11, 9, 15, 0))


def test_quiet_end_rolls_to_next_day():
    seconds = seconds_until_quiet_end("08:00", datetime(2026, 11, 9, 23, 0))
    assert seconds == 9 * 3600


def test_adaptive_interval_accelerates_near_date():
    for days, factor in ((10, 1), (3, 2), (1, 3)):
        travel_date = TODAY + timedelta(days=days)
        value = adaptive_interval(600, 60, travel_date, TODAY)
        base = max(60, 600 // factor)
        assert base <= value <= base + base // 10

    near = adaptive_interval(600, 600, TODAY, TODAY)
    assert near >= 600


# ── подбор вагонов ─────────────────────────────────────────────


def make_train() -> Train:
    return Train(
        number="712Б",
        depart_time="12:34",
        carriages=(
            Carriage("Плацкарт", 20, 30.0),
            Carriage("Купе", 8, 60.0),
            Carriage("СВ", 0, 120.0),
        ),
    )


def test_filters_threshold_carriage_and_price():
    filters = Filters(threshold=9, carriage="куп", max_price=100.0)

    assert filters.matches(Carriage("Купе", 10, 60.0))
    assert not filters.matches(Carriage("Купе", 8, 60.0))
    assert not filters.matches(Carriage("Плацкарт", 20, 30.0))
    assert not filters.matches(Carriage("Купе", 10, 150.0))
    assert not filters.matches(Carriage("Купе", 10, None))


def test_zero_threshold_still_requires_a_seat():
    filters = Filters(threshold=0)

    assert not filters.matches(Carriage("Купе", 0, 10.0))
    assert filters.matches(Carriage("Купе", 1, 10.0))


def test_evaluate_train_signature_is_stable_and_content_based():
    train = make_train()
    filters = Filters(threshold=8)

    first = evaluate_train(train, filters)
    assert [c.type for c in first.suitable] == ["Плацкарт", "Купе"]
    assert first.has_matches
    assert first.signature

    assert evaluate_train(train, filters).signature == first.signature
    assert evaluate_train(train, Filters(threshold=20)).signature != first.signature
    assert evaluate_train(train, Filters(threshold=8, carriage="св")).signature == ""


def test_evaluate_train_without_matches():
    evaluation = evaluate_train(make_train(), Filters(threshold=100))

    assert not evaluation.has_matches
    assert evaluation.signature == ""
    assert evaluation.max_seats == 20
    assert evaluation.min_price == 30.0


def test_pick_train_exact_then_fallbacks():
    trains = [
        Train("001А", "10:00"),
        Train("002Б", "12:00"),
        Train("003В", "14:00"),
    ]

    assert pick_train(trains, "12:00", "002Б") is trains[1]
    assert pick_train(trains, "09:00", "003В") is trains[2]
    assert pick_train(trains, "14:00", "999Я") is trains[2]
    assert pick_train(trains, "16:00", "999Я") is None
    assert pick_train([], "10:00", None) is None
