"""Тесты общего слоя БД и утилит времени."""

import db


def test_schema_is_idempotent(clean_db):
    clean_db.init_schema()
    clean_db.init_schema()
    with clean_db.get_db_cursor() as cursor:
        tables = {
            row["name"]
            for row in cursor.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    expected = {
        "users",
        "active_trackings",
        "search_history",
        "popular_stations",
        "favorite_routes",
        "user_stats",
        "feature_flags",
        "webhook_events",
        "user_logs",
        "bot_errors",
        "admin_logs",
        "sync_flags",
        "request_counter_history",
        "availability_history",
        "user_settings",
    }
    assert expected <= tables


def test_foreign_keys_enabled(clean_db):
    with clean_db.get_db_cursor() as cursor:
        assert cursor.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_fmt_local_utc_to_minsk(clean_db):
    # Europe/Minsk = UTC+3, поэтому 10:00 UTC -> 13:00 локального
    assert clean_db.fmt_local("2030-01-15 10:00:00") == "15.01.2030 13:00"


def test_fmt_local_accepts_formats_and_handles_bad_values(clean_db):
    assert clean_db.fmt_local("2030-01-15 10:00") == "15.01.2030 13:00"
    assert clean_db.fmt_local("2030-01-15", fmt="%d.%m.%Y") == "15.01.2030"
    assert clean_db.fmt_local(None) == "—"
    assert clean_db.fmt_local("not-a-date") == "—"


def test_cleanup_old_data(clean_db, user):
    with clean_db.get_db_cursor() as cursor:
        cursor.execute(
            "INSERT INTO user_logs (chat_id, action, created_at) VALUES (?, ?, datetime('now', '-40 days'))",
            (user, "old"),
        )
        cursor.execute(
            "INSERT INTO user_logs (chat_id, action, created_at) VALUES (?, ?, datetime('now'))",
            (user, "fresh"),
        )
    deleted = clean_db.cleanup_old_data(30)
    assert deleted >= 1
    with clean_db.get_db_cursor() as cursor:
        actions = [row["action"] for row in cursor.execute("SELECT action FROM user_logs")]
    assert actions == ["fresh"]


def test_iter_chunks():
    assert list(db.iter_chunks([1, 2, 3, 4, 5], 2)) == [[1, 2], [3, 4], [5]]
    assert list(db.iter_chunks([], 2)) == []


def test_utc_now_is_timezone_aware():
    now = db.utc_now()
    assert now.tzinfo is not None
    assert abs((db.local_now().astimezone(now.tzinfo) - now).total_seconds()) < 5
