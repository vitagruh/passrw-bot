"""Inline-клавиатуры бота."""

from __future__ import annotations

import calendar
from datetime import date, timedelta

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from passrw.domain.railway import Train

MONTHS = (
    "Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
    "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь",
)
WEEKDAYS = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")


def main_menu() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="🔎 Новый поиск", callback_data="menu:track")
    builder.button(text="📋 Мои трекинги", callback_data="menu:mytracks")
    builder.button(text="📊 Статистика", callback_data="menu:stats")
    builder.button(text="📜 История", callback_data="menu:history")
    builder.button(text="⭐ Избранное", callback_data="menu:favorites")
    builder.button(text="⚙️ Настройки", callback_data="menu:settings")
    builder.button(text="❓ Помощь", callback_data="menu:help")
    builder.adjust(1, 2, 2, 2)
    return builder.as_markup()


def back_to_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="🏠 Меню", callback_data="menu:main")]]
    )


def cancel_search() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="✖️ Отмена", callback_data="search:cancel")]]
    )


def stations_suggestions(
    candidates: list[str], raw: str | None = None
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for index, name in enumerate(candidates):
        builder.button(text=name, callback_data=f"st:{index}")
    if raw:
        builder.button(text=f"✅ Использовать «{raw[:40]}»", callback_data="st:raw")
    builder.button(text="✖️ Отмена", callback_data="search:cancel")
    builder.adjust(1)
    return builder.as_markup()


def calendar_kb(
    year: int, month: int, today: date, max_date: date | None = None
) -> InlineKeyboardMarkup:
    max_date = max_date or today + timedelta(days=60)
    builder = InlineKeyboardBuilder()

    prev_month = (year, month - 1) if month > 1 else (year - 1, 12)
    next_month = (year, month + 1) if month < 12 else (year + 1, 1)
    can_prev = date(*prev_month, 1) >= date(today.year, today.month, 1)
    can_next = date(*next_month, 1) <= max_date

    builder.row(
        InlineKeyboardButton(
            text="‹" if can_prev else " ",
            callback_data=f"cal:nav:{prev_month[0]:04d}-{prev_month[1]:02d}" if can_prev else "cal:noop",
        ),
        InlineKeyboardButton(text=f"{MONTHS[month - 1]} {year}", callback_data="cal:noop"),
        InlineKeyboardButton(
            text="›" if can_next else " ",
            callback_data=f"cal:nav:{next_month[0]:04d}-{next_month[1]:02d}" if can_next else "cal:noop",
        ),
    )
    builder.row(*[InlineKeyboardButton(text=name, callback_data="cal:noop") for name in WEEKDAYS])

    for week in calendar.monthcalendar(year, month):
        row = []
        for day in week:
            if day == 0:
                row.append(InlineKeyboardButton(text=" ", callback_data="cal:noop"))
                continue
            current = date(year, month, day)
            if current < today or current > max_date:
                row.append(InlineKeyboardButton(text="·", callback_data="cal:noop"))
            elif current == today:
                row.append(
                    InlineKeyboardButton(text=f"·{day}·", callback_data=f"cal:day:{current.isoformat()}")
                )
            else:
                row.append(
                    InlineKeyboardButton(text=str(day), callback_data=f"cal:day:{current.isoformat()}")
                )
        builder.row(*row)

    quick_row = [InlineKeyboardButton(text="Сегодня", callback_data=f"cal:day:{today.isoformat()}")]
    tomorrow = today + timedelta(days=1)
    if tomorrow <= max_date:
        quick_row.append(
            InlineKeyboardButton(text="Завтра", callback_data=f"cal:day:{tomorrow.isoformat()}")
        )
    builder.row(*quick_row)
    builder.row(InlineKeyboardButton(text="✖️ Отмена", callback_data="search:cancel"))
    return builder.as_markup()


def passengers_kb(max_passengers: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for value in range(1, max_passengers + 1):
        builder.button(text=str(value), callback_data=f"pax:{value}")
    builder.button(text="✖️ Отмена", callback_data="search:cancel")
    builder.adjust(5)
    return builder.as_markup()


def trains_kb(trains: list[Train], search_id: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for index, train in enumerate(trains[:12]):
        seats = train.max_seats
        price = f" · {train.min_price:.0f} BYN" if train.min_price else ""
        builder.button(
            text=f"{train.depart_time} · №{train.number} · {seats} мест{price}",
            callback_data=f"tr:{search_id}:{index}",
        )
    builder.row(InlineKeyboardButton(text="✖️ Закрыть", callback_data="search:cancel"))
    builder.adjust(1)
    return builder.as_markup()


def train_actions_kb(
    search_id: str, index: int, *, buy_url: str, favorite: bool = False
) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="🔔 Отслеживать поезд", callback_data=f"trk:{search_id}:{index}")],
        [
            InlineKeyboardButton(
                text="⭐ В избранном" if favorite else "⭐ В избранное",
                callback_data=f"trfav:{search_id}:{index}",
            ),
            InlineKeyboardButton(text="🔄 Обновить", callback_data=f"refresh:{search_id}"),
        ],
        [InlineKeyboardButton(text="🎫 Открыть на pass.rw.by", url=buy_url)],
        [InlineKeyboardButton(text="⬅️ К списку поездов", callback_data="back:list")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def trackings_kb(trackings) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for index, tracking in enumerate(trackings, 1):
        builder.row(
            InlineKeyboardButton(
                text=f"📈 №{index}", callback_data=f"mt:trend:{tracking.id}"
            ),
            InlineKeyboardButton(
                text=f"🔗 №{index}", callback_data=f"mt:share:{tracking.id}"
            ),
            InlineKeyboardButton(
                text=f"⏹ №{index}", callback_data=f"mt:stop:{tracking.id}"
            ),
        )
    if trackings:
        builder.row(InlineKeyboardButton(text="⛔ Остановить все", callback_data="stop:all"))
    builder.row(InlineKeyboardButton(text="🏠 Меню", callback_data="menu:main"))
    return builder.as_markup()


def stop_kb(trackings) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for tracking in trackings:
        builder.button(
            text=f"⏹ {tracking.from_station} → {tracking.to_station} ({tracking.depart_time})",
            callback_data=f"mt:stop:{tracking.id}",
        )
    builder.button(text="⛔ Остановить все", callback_data="stop:all")
    builder.button(text="🏠 Меню", callback_data="menu:main")
    builder.adjust(1)
    return builder.as_markup()


def history_kb(items) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for item in items:
        builder.button(
            text=f"🔁 {item.from_station} → {item.to_station} · {item.travel_date.strftime('%d.%m')}",
            callback_data=f"hist:{item.id}",
        )
    builder.button(text="🏠 Меню", callback_data="menu:main")
    builder.adjust(1)
    return builder.as_markup()


def favorites_kb(items) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for item in items:
        builder.row(
            InlineKeyboardButton(
                text=f"🔁 {item.from_station} → {item.to_station}",
                callback_data=f"fav:go:{item.id}",
            ),
            InlineKeyboardButton(text="🗑", callback_data=f"fav:del:{item.id}"),
        )
    builder.row(InlineKeyboardButton(text="🏠 Меню", callback_data="menu:main"))
    return builder.as_markup()


def settings_kb() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="🕓 Тихие часы", callback_data="set:quiet")
    builder.button(text="💰 Макс. цена", callback_data="set:price")
    builder.button(text="🎫 Тип вагона", callback_data="set:carriage")
    builder.button(text="🔔 Порог мест", callback_data="set:threshold")
    builder.button(text="👥 Пассажиры по умолчанию", callback_data="set:pax")
    builder.button(text="♻️ Сбросить", callback_data="set:reset")
    builder.button(text="🏠 Меню", callback_data="menu:main")
    builder.adjust(2, 2, 1, 1, 1)
    return builder.as_markup()


def carriage_kb() -> InlineKeyboardMarkup:
    labels = {
        "any": "любой",
        "plaz": "плацкарт",
        "kupe": "купе",
        "sv": "СВ",
        "sid": "сидячий",
        "lux": "люкс",
    }
    builder = InlineKeyboardBuilder()
    for key, label in labels.items():
        builder.button(text=label, callback_data=f"setc:{key}")
    builder.button(text="⬅️ Назад", callback_data="set:open")
    builder.adjust(2)
    return builder.as_markup()


def threshold_kb(max_passengers: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for value in range(1, max_passengers + 1):
        builder.button(text=str(value), callback_data=f"sett:{value}")
    builder.button(text="⬅️ Назад", callback_data="set:open")
    builder.adjust(5)
    return builder.as_markup()


def default_pax_kb(max_passengers: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for value in range(1, max_passengers + 1):
        builder.button(text=str(value), callback_data=f"setp:{value}")
    builder.button(text="⬅️ Назад", callback_data="set:open")
    builder.adjust(5)
    return builder.as_markup()
