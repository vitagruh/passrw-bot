"""Разбор HTML-выдачи pass.rw.by.

Парсер намеренно терпим к косметическим изменениям вёрстки: у каждого
элемента есть запасной селектор, а отсутствие таблицы распознаётся отдельно,
чтобы не путать «нет поездов» со «сайт изменился».
"""

from __future__ import annotations

import re

from bs4 import BeautifulSoup

from passrw.domain.railway import Carriage, Train

_DIGITS = re.compile(r"\d+")
_PRICE = re.compile(r"(\d+(?:[.,]\d+)?)")

_EMPTY_MARKERS = (
    "sch-table",
    "ничего не найдено",
    "нет поездов",
    "не найдено",
    "по вашему запросу",
    "нет прямого сообщения",
    "станция не найдена",
    "в пределах 60 суток",
)


class LayoutChanged(Exception):
    """Страница получена, но таблица результатов не распознана."""


def parse_route_html(html: str) -> list[Train]:
    soup = BeautifulSoup(html, "html.parser")
    rows = soup.find_all("div", class_="sch-table__row-wrap")
    if not rows:
        rows = soup.find_all("div", class_="sch-table__row")

    trains: list[Train] = []
    for row in rows:
        time_node = row.find(class_="train-from-time")
        number_node = row.find(class_="train-number")
        depart_time = _text(time_node)
        number = _text(number_node)
        if not depart_time or not number:
            continue

        duration_node = row.find(class_="train-duration-time") or row.find(
            class_="sch-table__duration"
        )
        trains.append(
            Train(
                number=number,
                depart_time=depart_time,
                duration=_text(duration_node),
                carriages=tuple(_parse_carriages(row)),
            )
        )
    return trains


def looks_like_results_page(html: str) -> bool:
    lowered = (html or "").casefold()
    return any(marker in lowered for marker in _EMPTY_MARKERS)


def _parse_carriages(row) -> list[Carriage]:
    status_cell = row.find(class_="cell-4") or row
    items = status_cell.find_all("div", class_="sch-table__t-item")
    # Сайт оставляет имя типа пустым у следующих групп мест той же категории.
    grouped: dict[str, tuple[int, float | None]] = {}
    current_type = ""
    for item in items:
        carriage_type = _text(item.find(class_="sch-table__t-name"))
        if carriage_type:
            current_type = carriage_type
        carriage_type = carriage_type or current_type or "Неизвестный"

        quant_node = item.find("a", class_="sch-table__t-quant") or item.find(
            class_="sch-table__t-quant"
        )
        seats_text = _text(quant_node)
        digits = _DIGITS.search(seats_text)
        seats = int(digits.group()) if digits else 0

        price_node = item.find(class_="js-price")
        price = None
        if price_node is not None:
            price = _parse_price(price_node.get("data-cost-byn"))
        if price is None:
            price = _parse_price(_text(item.find(class_="ticket-cost")))

        # Группы одной категории (разные классы) суммируем до уровня типа вагона.
        total, cheapest = grouped.get(carriage_type, (0, None))
        grouped[carriage_type] = (total + seats, _min_price(cheapest, price))
    return [
        Carriage(type=carriage_type, seats=seats, price=price)
        for carriage_type, (seats, price) in grouped.items()
    ]


def _min_price(first: float | None, second: float | None) -> float | None:
    if first is None:
        return second
    if second is None:
        return first
    return min(first, second)


def _text(node) -> str:
    return node.get_text(" ", strip=True) if node is not None else ""


def _parse_price(value) -> float | None:
    if value is None:
        return None
    match = _PRICE.search(str(value).replace("\xa0", " "))
    if not match:
        return None
    try:
        price = float(match.group(1).replace(",", "."))
    except ValueError:
        return None
    return price if price > 0 else None
