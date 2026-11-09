"""Доменные модели поездов и правила подбора подходящих вагонов."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class Carriage:
    type: str
    seats: int = 0
    price: float | None = None

    @property
    def has_seats(self) -> bool:
        return self.seats > 0


@dataclass(frozen=True, slots=True)
class Train:
    number: str
    depart_time: str
    duration: str = ""
    carriages: tuple[Carriage, ...] = ()

    @property
    def max_seats(self) -> int:
        return max((c.seats for c in self.carriages), default=0)

    @property
    def min_price(self) -> float | None:
        prices = [c.price for c in self.carriages if c.price and c.price > 0]
        return min(prices) if prices else None

    @property
    def total_seats(self) -> int:
        return sum(c.seats for c in self.carriages)


@dataclass(frozen=True, slots=True)
class Filters:
    threshold: int = 1
    carriage: str | None = None
    max_price: float | None = None

    def matches(self, carriage: Carriage) -> bool:
        if carriage.seats < max(1, self.threshold):
            return False
        if self.carriage and self.carriage.casefold() not in carriage.type.casefold():
            return False
        if self.max_price is not None:
            if carriage.price is None or carriage.price > self.max_price:
                return False
        return True


@dataclass(slots=True)
class Evaluation:
    train: Train
    max_seats: int
    min_price: float | None
    suitable: list[Carriage] = field(default_factory=list)
    signature: str = ""

    @property
    def has_matches(self) -> bool:
        return bool(self.suitable)


def evaluate_train(train: Train, filters: Filters) -> Evaluation:
    suitable = [c for c in train.carriages if filters.matches(c)]
    raw = "|".join(
        sorted(f"{c.type}:{c.seats}:{c.price if c.price is not None else '-'}" for c in suitable)
    )
    signature = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:20] if raw else ""
    return Evaluation(
        train=train,
        max_seats=train.max_seats,
        min_price=train.min_price,
        suitable=suitable,
        signature=signature,
    )


def pick_train(trains: list[Train], depart_time: str, number: str | None) -> Train | None:
    """Ищет поезд по номеру и времени; терпит перенумерацию и сдвиг времени."""
    if not trains:
        return None
    exact = next(
        (t for t in trains if t.depart_time == depart_time and (not number or t.number == number)),
        None,
    )
    if exact:
        return exact
    if number:
        by_number = next((t for t in trains if t.number == number), None)
        if by_number:
            return by_number
    return next((t for t in trains if t.depart_time == depart_time), None)
