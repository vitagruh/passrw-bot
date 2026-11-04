"""Тесты P2: настройки, тихие часы, подсказки станций, тренды."""

from datetime import datetime

import db
import ticket_bot as tb


def test_settings_defaults_and_roundtrip(clean_db, user):
    settings = tb.get_user_settings(user)
    assert settings['notify_threshold'] == 1
    assert settings['quiet_from'] is None

    assert tb.save_user_settings(
        user, quiet_from='22:00', quiet_to='07:30',
        notify_threshold=3, default_passengers=2,
        carriage_filter='купе', max_price=150.5,
    )
    saved = tb.get_user_settings(user)
    assert saved['quiet_from'] == '22:00'
    assert saved['quiet_to'] == '07:30'
    assert saved['notify_threshold'] == 3
    assert saved['default_passengers'] == 2
    assert saved['carriage_filter'] == 'купе'
    assert saved['max_price'] == 150.5

    tb.save_user_settings(user, max_price=None)
    assert tb.get_user_settings(user)['max_price'] is None


def test_parse_quiet_hours():
    assert tb.parse_quiet_hours('22:00-08:00') == ('22:00', '08:00')
    assert tb.parse_quiet_hours(' 7:00 - 9:30 ') is None  # нужен ведущий ноль
    assert tb.parse_quiet_hours('07:00-09:30') == ('07:00', '09:30')
    assert tb.parse_quiet_hours('off') == (None, None)
    assert tb.parse_quiet_hours('выкл') == (None, None)
    assert tb.parse_quiet_hours('22:00') is None
    assert tb.parse_quiet_hours('25:00-08:00') is None
    assert tb.parse_quiet_hours('10:00-10:00') is None
    assert tb.parse_quiet_hours('') is None


def test_in_quiet_hours_daytime():
    settings = {'quiet_from': '09:00', 'quiet_to': '18:00'}
    assert tb.in_quiet_hours(settings, datetime(2030, 1, 1, 12, 0))
    assert not tb.in_quiet_hours(settings, datetime(2030, 1, 1, 8, 59))
    assert not tb.in_quiet_hours(settings, datetime(2030, 1, 1, 18, 0))


def test_in_quiet_hours_overnight():
    settings = {'quiet_from': '22:00', 'quiet_to': '07:00'}
    assert tb.in_quiet_hours(settings, datetime(2030, 1, 1, 23, 30))
    assert tb.in_quiet_hours(settings, datetime(2030, 1, 1, 6, 59))
    assert not tb.in_quiet_hours(settings, datetime(2030, 1, 1, 12, 0))
    assert not tb.in_quiet_hours({'quiet_from': None, 'quiet_to': None}, datetime(2030, 1, 1, 23, 0))


def test_settings_summary(clean_db, user):
    tb.save_user_settings(user, quiet_from='22:00', quiet_to='08:00', max_price=100)
    text = tb.settings_summary(tb.get_user_settings(user))
    assert '22:00–08:00' in text
    assert '100.00 BYN' in text


def test_station_suggestions(clean_db, user):
    with db.get_db_cursor() as cursor:
        cursor.execute(
            "INSERT INTO popular_stations (station_name, usage_count) VALUES (?, ?)",
            ("Минск-Пассажирский", 10),
        )
    suggestions = tb.get_station_suggestions("Мин")
    assert "Минск-Пассажирский" in suggestions
    # Точное совпадение не подсказываем
    assert tb.get_station_suggestions("Минск-Пассажирский") == []
    assert tb.get_station_suggestions("М") == []


def test_build_trend_text(clean_db, tracking):
    text = tb.build_trend_text(tracking["id"])
    assert "Данных пока нет" in text

    with db.get_db_cursor() as cursor:
        for seats, price in ((0, None), (3, 50.0), (5, 45.5)):
            cursor.execute(
                """INSERT INTO availability_history
                   (tracking_id, chat_id, train_num, train_time, seats_available, min_price)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (tracking["id"], tracking["chat_id"], "701Б", "12:30", seats, price),
            )

    text = tb.build_trend_text(tracking["id"])
    assert "История наличия" in text
    assert "Текущее: <b>5</b>" in text
    assert "45.50 BYN" in text
    assert "▁" in text
    assert "Изменение" in text
