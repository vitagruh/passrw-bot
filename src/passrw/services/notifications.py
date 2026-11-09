"""Формирование текстов уведомлений."""

from __future__ import annotations

from datetime import date
from html import escape

from passrw.domain.railway import Carriage, Evaluation, Train


def _carriage_icon(carriage_type: str) -> str:
    return "🪑" if "сидяч" in carriage_type.casefold() else "🛏"


def _price_text(price: float | None) -> str:
    return f"{price:.2f} BYN" if price is not None else "цена н/д"


def build_alert(
    *,
    from_station: str,
    to_station: str,
    travel_date: date,
    passengers: int,
    evaluation: Evaluation,
) -> str:
    train: Train = evaluation.train
    lines = [
        f"🎉 <b>Есть места:</b> {escape(from_station)} → {escape(to_station)}",
        f"📅 {travel_date.strftime('%d.%m.%Y')} · поезд <b>{escape(train.number)}</b>"
        f" · {escape(train.depart_time)}",
        f"👥 нужно мест: <b>{passengers}</b>",
        "",
    ]
    for carriage in evaluation.suitable[:8]:
        lines.append(_carriage_line(carriage, passengers))
    if len(evaluation.suitable) > 8:
        lines.append(f"…и ещё {len(evaluation.suitable) - 8} ваг.")
    lines.append("")
    lines.append("🎫 Купить билет на pass.rw.by")
    return "\n".join(lines)


def _carriage_line(carriage: Carriage, passengers: int) -> str:
    mark = "✅" if carriage.seats >= passengers else "⚠️"
    return (
        f"{mark} {_carriage_icon(carriage.type)} <b>{escape(carriage.type)}</b>: "
        f"{carriage.seats} мест · {_price_text(carriage.price)}"
    )


def build_tracking_summary(
    *,
    from_station: str,
    to_station: str,
    travel_date: date,
    passengers: int,
    train_number: str,
    depart_time: str,
    threshold: int,
    carriage_filter: str | None,
    max_price: float | None,
) -> str:
    filters = [f"порог {threshold} мест"]
    if carriage_filter:
        filters.append(f"вагон: {carriage_filter}")
    if max_price:
        filters.append(f"до {max_price:.2f} BYN")
    return (
        f"✅ <b>Трекинг создан</b>\n\n"
        f"📍 {escape(from_station)} → {escape(to_station)}\n"
        f"📅 {travel_date.strftime('%d.%m.%Y')} · {escape(depart_time)} · "
        f"поезд <b>{escape(train_number)}</b>\n"
        f"👥 {passengers} чел. · фильтры: {escape(', '.join(filters))}\n\n"
        "Пришлю сообщение, когда появятся подходящие места."
    )


def build_train_card(*, train: Train, passengers: int, duration: str | None = None) -> str:
    lines = [
        f"🚂 <b>Поезд {escape(train.number)}</b> · {escape(train.depart_time)}",
    ]
    if duration or train.duration:
        lines.append(f"⏳ В пути: {escape(duration or train.duration)}")
    lines.append(f"👥 Нужно мест: {passengers}")
    lines.append("")
    if not train.carriages:
        lines.append("Данные о вагонах пока недоступны.")
        return "\n".join(lines)

    lines.append("<b>Вагоны:</b>")
    for carriage in train.carriages:
        mark = "✅" if carriage.seats >= passengers else "❌"
        lines.append(
            f"{mark} {_carriage_icon(carriage.type)} {escape(carriage.type)}: "
            f"{carriage.seats} мест · {_price_text(carriage.price)}"
        )
    return "\n".join(lines)


def build_heartbeat(tracking_id: int, seats: int) -> str:
    return f"💓 Трекинг #{tracking_id} работает. Сейчас мест: {seats}."
