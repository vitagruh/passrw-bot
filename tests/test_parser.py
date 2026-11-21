"""Тесты терпимого парсера выдачи pass.rw.by."""

from __future__ import annotations

import pytest

from passrw.services.parser import (
    EmptyReason,
    detect_empty_reason,
    looks_like_results_page,
    parse_route_html,
)

SAMPLE_HTML = """
<html><body>
<div class="sch-table">
  <div class="sch-table__row-wrap">
    <div class="train-from-time">12:34</div>
    <div class="train-number">712Б</div>
    <div class="train-duration-time">7 ч 20 мин</div>
    <div class="cell-4">
      <div class="sch-table__t-item">
        <div class="sch-table__t-name">Купе</div>
        <a class="sch-table__t-quant">12</a>
        <span class="js-price" data-cost-byn="45.50">45,50 BYN</span>
      </div>
      <div class="sch-table__t-item">
        <div class="sch-table__t-name">Плацкарт</div>
        <a class="sch-table__t-quant">нет мест</a>
        <span class="ticket-cost">от 30,00 BYN</span>
      </div>
    </div>
  </div>
  <div class="sch-table__row-wrap">
    <div class="train-from-time">18:05</div>
    <div class="train-number">003А</div>
    <div class="sch-table__duration">9 ч</div>
    <div class="cell-4">
      <div class="sch-table__t-item">
        <div class="sch-table__t-name">СВ</div>
        <span class="sch-table__t-quant">4</span>
        <span class="js-price" data-cost-byn="120,00">&nbsp;</span>
      </div>
    </div>
  </div>
  <div class="sch-table__row-wrap">
    <div class="train-from-time">20:00</div>
    <div class="cell-4">
      <div class="sch-table__t-item">
        <div class="sch-table__t-name">Сидячий</div>
        <span class="sch-table__t-quant">30</span>
      </div>
    </div>
  </div>
</div>
</body></html>
"""


def test_parses_trains_and_skips_rows_without_number():
    trains = parse_route_html(SAMPLE_HTML)

    assert [train.number for train in trains] == ["712Б", "003А"]
    assert trains[0].depart_time == "12:34"
    assert trains[0].duration == "7 ч 20 мин"
    assert trains[1].duration == "9 ч"


def test_parses_carriages_seats_and_prices():
    train = parse_route_html(SAMPLE_HTML)[0]

    assert len(train.carriages) == 2
    kupe, plaz = train.carriages
    assert (kupe.type, kupe.seats, kupe.price) == ("Купе", 12, 45.5)
    assert (plaz.type, plaz.seats, plaz.price) == ("Плацкарт", 0, 30.0)
    assert train.max_seats == 12
    assert train.min_price == 30.0
    assert train.total_seats == 12


def test_price_from_data_attribute_wins_over_text():
    carriage = parse_route_html(SAMPLE_HTML)[1].carriages[0]

    assert (carriage.type, carriage.seats, carriage.price) == ("СВ", 4, 120.0)


REAL_GROUPED_HTML = """
<html><body>
<div class="sch-table__row-wrap">
  <div class="train-from-time">07:04</div>
  <div class="train-number">007М</div>
  <div class="cell-4">
    <div class="sch-table__t-item">
      <div class="sch-table__t-name">Плацкартный</div>
      <a class="sch-table__t-quant">83</a>
      <span class="js-price" data-cost-byn="29,08"></span>
    </div>
    <div class="sch-table__t-item">
      <div class="sch-table__t-name"></div>
      <a class="sch-table__t-quant">79</a>
      <span class="js-price" data-cost-byn="27,00"></span>
    </div>
    <div class="sch-table__t-item">
      <div class="sch-table__t-name">Купейный</div>
      <a class="sch-table__t-quant">52</a>
      <span class="js-price" data-cost-byn="47,87"></span>
    </div>
    <div class="sch-table__t-item">
      <div class="sch-table__t-name"></div>
      <a class="sch-table__t-quant">38</a>
      <span class="js-price" data-cost-byn="50,00"></span>
    </div>
  </div>
</div>
</body></html>
"""


def test_empty_type_name_inherits_previous_and_groups_seats():
    carriages = parse_route_html(REAL_GROUPED_HTML)[0].carriages

    assert [(c.type, c.seats, c.price) for c in carriages] == [
        ("Плацкартный", 162, 27.0),
        ("Купейный", 90, 47.87),
    ]


def test_empty_layout_is_not_an_error():
    assert parse_route_html("<html><body>Ничего не найдено</body></html>") == []


def test_layout_markers_detected():
    assert looks_like_results_page("<div class='sch-table'></div>")
    assert looks_like_results_page("По вашему запросу нет поездов")
    assert looks_like_results_page("Нет прямого сообщения между станциями")
    assert looks_like_results_page("Станция не найдена")
    assert looks_like_results_page("Выберите дату в пределах 60 суток")
    assert not looks_like_results_page("<html><body><h1>502 Bad Gateway</h1></body></html>")


@pytest.mark.parametrize(
    ("html", "reason"),
    [
        ("<p>Станция отправления не найдена</p>", EmptyReason.STATION_NOT_FOUND),
        ("<p>Нет прямого сообщения между станциями</p>", EmptyReason.NO_DIRECT_CONNECTION),
        ("<p>Выберите дату в пределах 60 суток</p>", EmptyReason.OUT_OF_SALE_WINDOW),
        ("<p>Продажа билетов не открыта</p>", EmptyReason.OUT_OF_SALE_WINDOW),
        ("<p>По вашему запросу ничего не найдено</p>", EmptyReason.NO_TRAINS),
        ("<div class='sch-table'></div>", EmptyReason.NO_TRAINS),
    ],
)
def test_detect_empty_reason_markers(html, reason):
    assert detect_empty_reason(html) is reason


def test_unknown_page_has_no_empty_reason():
    assert detect_empty_reason("<html><body><h1>502 Bad Gateway</h1></body></html>") is None
