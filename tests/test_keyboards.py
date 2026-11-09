"""Inline-клавиатуры: календарь выбора даты."""

from __future__ import annotations

import calendar
from datetime import date

from passrw.bot import keyboards


def _day_buttons(markup):
    return [
        button
        for row in markup.inline_keyboard
        for button in row
        if button.callback_data.startswith("cal:day:")
    ]


def test_calendar_grid_matches_month():
    today = date(2026, 11, 9)
    markup = keyboards.calendar_kb(2026, 11, today)

    header = markup.inline_keyboard[0]
    assert [b.text for b in header] == [" ", "Ноябрь 2026", "›"]
    assert header[0].callback_data == "cal:noop"
    assert header[2].callback_data == "cal:nav:2026-12"
    assert [b.text for b in markup.inline_keyboard[1]] == list(keyboards.WEEKDAYS)

    weeks = calendar.monthcalendar(2026, 11)
    grid = markup.inline_keyboard[2 : 2 + len(weeks)]
    assert len(grid) == len(weeks)
    for week, row in zip(weeks, grid, strict=True):
        assert len(row) == 7
        for day, button in zip(week, row, strict=True):
            if day == 0:
                continue
            if day < today.day:
                assert (button.text, button.callback_data) == ("·", "cal:noop")
            elif day == today.day:
                assert button.text == f"·{day}·"
                assert button.callback_data == f"cal:day:2026-11-{day:02d}"
            else:
                assert button.text == str(day)
                assert button.callback_data == f"cal:day:2026-11-{day:02d}"

    quick = markup.inline_keyboard[2 + len(weeks)]
    assert [b.callback_data for b in quick] == ["cal:day:2026-11-09", "cal:day:2026-11-10"]


def test_calendar_respects_max_date():
    today = date(2026, 11, 9)
    markup = keyboards.calendar_kb(2026, 12, today, max_date=date(2026, 12, 20))

    header = markup.inline_keyboard[0]
    assert header[0].callback_data == "cal:nav:2026-11"
    assert header[2].callback_data == "cal:noop"

    dates = {date.fromisoformat(b.callback_data.rsplit(":", 1)[1]) for b in _day_buttons(markup)}
    assert date(2026, 12, 20) in dates
    assert date(2026, 12, 21) not in dates
