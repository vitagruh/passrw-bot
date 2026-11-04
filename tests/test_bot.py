"""Тесты валидации ввода, парсера и менеджера задач бота."""

from datetime import date, timedelta

import db
import ticket_bot as tb


# ── Валидация ──────────────────────────────────────────────

def test_validate_station_name():
    assert tb.validate_station_name("Минск")
    assert tb.validate_station_name("Санкт-Петербург")
    assert tb.validate_station_name("Киров (Пассажирский)")
    assert not tb.validate_station_name("")
    assert not tb.validate_station_name("М")
    assert not tb.validate_station_name("Минск<script>")
    assert not tb.validate_station_name("A" * 65)


def test_parse_travel_date():
    today = date.today()
    assert tb.parse_travel_date(today.isoformat()) == today.isoformat()
    tomorrow = today + timedelta(days=1)
    assert tb.parse_travel_date(tomorrow.isoformat()) == tomorrow.isoformat()
    assert tb.parse_travel_date((today - timedelta(days=1)).isoformat()) is None
    assert tb.parse_travel_date((today + timedelta(days=366)).isoformat()) is None
    assert tb.parse_travel_date("01.01.2030") is None
    assert tb.parse_travel_date("2030-02-30") is None
    assert tb.parse_travel_date("") is None


def test_parse_passengers():
    assert tb.parse_passengers("1") == 1
    assert tb.parse_passengers("9") == 9
    assert tb.parse_passengers("0") is None
    assert tb.parse_passengers("10") is None
    assert tb.parse_passengers("abc") is None
    assert tb.parse_passengers("") is None


def test_sanitize_input():
    assert tb.sanitize_input('<script>alert("x")</script>') == "scriptalert(x)/script"
    assert tb.sanitize_input("") == ""
    assert len(tb.sanitize_input("a" * 500)) == 100


def test_rate_limit_window(monkeypatch):
    chat_id = 424242
    tb.rate_limit_store.pop(chat_id, None)
    for _ in range(tb.RATE_LIMIT_MAX_REQUESTS):
        assert tb.check_rate_limit(chat_id)
    assert not tb.check_rate_limit(chat_id)
    monkeypatch.setitem(
        tb.rate_limit_store, chat_id,
        {"last_request": 0, "request_count": tb.RATE_LIMIT_MAX_REQUESTS},
    )
    assert tb.check_rate_limit(chat_id)
    tb.rate_limit_store.pop(chat_id, None)


# ── Парсер ─────────────────────────────────────────────────

SAMPLE_HTML = """
<div class="sch-table__row-wrap">
  <span class="train-from-time">12:30</span>
  <span class="train-number">701Б</span>
  <div class="train-duration-time">5 ч 20 мин</div>
  <div class="cell-4">
    <div class="sch-table__t-item">
      <div class="sch-table__t-name">Сидячий</div>
      <a class="sch-table__t-quant"><span>12 мест</span></a>
      <span class="js-price" data-cost-byn="45.50"></span>
    </div>
    <div class="sch-table__t-item">
      <div class="sch-table__t-name">Купе</div>
      <a class="sch-table__t-quant"><span>нет мест</span></a>
      <span class="ticket-cost">88.00</span>
    </div>
  </div>
</div>
<div class="sch-table__row-wrap">
  <span class="train-from-time">18:05</span>
  <span class="train-number">703Б</span>
  <div class="sch-table__duration">6 ч</div>
  <div class="cell-4"></div>
</div>
"""


def test_get_trains_list_parses_rows(monkeypatch):
    monkeypatch.setattr(tb, "fetch_page", lambda *args, **kwargs: SAMPLE_HTML)
    trains = tb.get_trains_list("Минск", "Брест", "2030-01-01", chat_id=1)

    assert len(trains) == 2
    first = trains[0]
    assert first["time"] == "12:30"
    assert first["num"] == "701Б"
    assert first["duration"] == "5 ч 20 мин"
    assert first["parsed_info"][0] == {
        "type": "Сидячий", "seats": "12", "price_byn": "45.50",
    }
    assert first["parsed_info"][1]["seats"] == "0"
    assert first["parsed_info"][1]["price_byn"] == "88.00"
    assert trains[1]["parsed_info"] == []


def test_get_trains_list_returns_empty_on_http_error(monkeypatch):
    monkeypatch.setattr(tb, "fetch_page", lambda *args, **kwargs: None)
    assert tb.get_trains_list("Минск", "Брест", "2030-01-01") == []


# ── История наличия мест ───────────────────────────────────

def _job(tracking_id=1, chat_id=1001):
    return tb.TrackingJob(
        tracking_id=tracking_id,
        chat_id=chat_id,
        from_station="Минск",
        to_station="Брест",
        date="2030-01-01",
        train_time="12:30",
        passengers=1,
    )


def test_availability_history_deduplicates_unchanged_values(clean_db):
    job = _job()
    train = {"num": "701Б"}

    tb._record_availability(job, train, 3, 50.0)
    tb._record_availability(job, train, 3, 50.0)

    with db.get_db_cursor() as cursor:
        rows = cursor.execute("SELECT * FROM availability_history").fetchall()
    assert len(rows) == 1

    tb._record_availability(job, train, 4, 50.0)
    tb._record_availability(job, train, 4, 55.0)
    with db.get_db_cursor() as cursor:
        rows = cursor.execute(
            "SELECT seats_available, min_price FROM availability_history ORDER BY id"
        ).fetchall()
    assert [(r["seats_available"], r["min_price"]) for r in rows] == [
        (3, 50.0), (4, 50.0), (4, 55.0),
    ]


# ── Менеджер задач ─────────────────────────────────────────

def test_start_tracking_job_reuses_and_separates(monkeypatch, clean_db, user):
    monkeypatch.setattr(tb, "tracking_worker", lambda job: None)

    with db.get_db_cursor() as cursor:
        ids = []
        for train_time in ("12:30", "18:00"):
            cursor.execute(
                """INSERT INTO active_trackings
                   (chat_id, from_station, to_station, date, passengers, train_time)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (user, "Минск", "Брест", "2030-01-01", 1, train_time),
            )
            ids.append(cursor.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])

    try:
        job1 = tb.start_tracking_job(ids[0], user, "Минск", "Брест", "2030-01-01", 1, "12:30")
        job2 = tb.start_tracking_job(ids[1], user, "Минск", "Брест", "2030-01-01", 2, "18:00")
        assert job1 is not job2
        assert len(tb.get_user_jobs(user)) == 2

        again = tb.start_tracking_job(ids[0], user, "Минск", "Брест", "2030-01-01", 3, "12:30")
        assert again is job1
        assert again.passengers == 3
    finally:
        with tb.jobs_lock:
            tb.active_jobs.clear()


def test_stop_job_deletes_only_target_row(monkeypatch, clean_db, user):
    monkeypatch.setattr(tb, "tracking_worker", lambda job: None)

    with db.get_db_cursor() as cursor:
        cursor.execute(
            """INSERT INTO active_trackings
               (chat_id, from_station, to_station, date, passengers, train_time)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (user, "Минск", "Брест", "2030-01-01", 1, "12:30"),
        )
        target = cursor.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
        cursor.execute(
            """INSERT INTO active_trackings
               (chat_id, from_station, to_station, date, passengers, train_time)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (user, "Минск", "Гомель", "2030-01-02", 1, "18:00"),
        )
        other = cursor.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]

    try:
        tb.start_tracking_job(target, user, "Минск", "Брест", "2030-01-01", 1, "12:30")
        tb.start_tracking_job(other, user, "Минск", "Гомель", "2030-01-02", 1, "18:00")
        job = tb.stop_job(target, chat_id=user)

        assert job.stop_event.is_set()
        with db.get_db_cursor() as cursor:
            remaining = [
                row["id"] for row in cursor.execute("SELECT id FROM active_trackings")
            ]
        assert remaining == [other]
        running = [j for j in tb.get_user_jobs(user) if not j.stop_event.is_set()]
        assert len(running) == 1
    finally:
        with tb.jobs_lock:
            tb.active_jobs.clear()
