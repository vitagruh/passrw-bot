"""Тесты мягкой остановки трекингов через SQLite + файлы-флаги."""

from pathlib import Path

import db
import tracking_sync as ts


def _row(tracking):
    with db.get_db_cursor() as cursor:
        return cursor.execute(
            "SELECT * FROM active_trackings WHERE id = ?", (tracking["id"],)
        ).fetchone()


def test_request_stop_targets_only_one_tracking(clean_db, tracking):
    with db.get_db_cursor() as cursor:
        cursor.execute(
            """INSERT INTO active_trackings
               (chat_id, from_station, to_station, date, passengers, train_time)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (tracking["chat_id"], "Минск", "Гомель", "2030-12-02", 1, "18:00"),
        )

    assert ts.request_tracking_stop(
        tracking["chat_id"], tracking_id=tracking["id"], admin_username="admin"
    )

    first = _row(tracking)
    with db.get_db_cursor() as cursor:
        second = cursor.execute(
            "SELECT * FROM active_trackings WHERE train_time = '18:00'"
        ).fetchone()

    assert first["is_stopped"] == 1
    assert second["is_stopped"] == 0
    assert ts.check_stop_request(tracking["chat_id"], tracking_id=tracking["id"])
    assert not ts.check_stop_request(tracking["chat_id"], tracking_id=second["id"])

    with db.get_db_cursor() as cursor:
        flags = cursor.execute(
            "SELECT * FROM sync_flags WHERE tracking_id = ?", (tracking["id"],)
        ).fetchall()
    assert len(flags) == 1
    assert flags[0]["action"] == "STOP"
    assert flags[0]["admin_username"] == "admin"

    # Файловый флаг существует и содержит tracking_id
    flag_file = Path(ts.SYNC_DIR) / f"stop_{tracking['id']}.flag"
    assert flag_file.exists()


def test_confirm_stop_removes_tracking_and_marks_flag(clean_db, tracking):
    assert ts.request_tracking_stop(tracking["chat_id"], tracking_id=tracking["id"])
    assert ts.confirm_tracking_stopped(
        tracking["chat_id"], tracking_id=tracking["id"], reason="admin_stop"
    )

    assert _row(tracking) is None
    with db.get_db_cursor() as cursor:
        flag = cursor.execute(
            "SELECT * FROM sync_flags WHERE tracking_id = ?", (tracking["id"],)
        ).fetchone()
        history = cursor.execute(
            "SELECT * FROM request_counter_history WHERE tracking_id = ?",
            (tracking["id"],),
        ).fetchone()
    assert flag["processed"] == 1
    assert flag["processed_at"] is not None
    assert history["reason"] == "admin_stop"
    assert not (Path(ts.SYNC_DIR) / f"stop_{tracking['id']}.flag").exists()


def test_confirm_stop_is_idempotent(clean_db, tracking):
    assert ts.confirm_tracking_stopped(tracking["chat_id"], tracking_id=tracking["id"])
    assert ts.confirm_tracking_stopped(tracking["chat_id"], tracking_id=tracking["id"])


def test_request_stop_unknown_tracking_returns_false(clean_db):
    assert not ts.request_tracking_stop(999, tracking_id=999999)


def test_request_stop_requires_identifier(clean_db):
    assert not ts.request_tracking_stop(999)


def test_force_delete_tracking(clean_db, tracking):
    assert ts.force_delete_tracking(tracking["chat_id"], tracking_id=tracking["id"])
    assert _row(tracking) is None


def test_get_tracking_runtime(clean_db, tracking):
    runtime = ts.get_tracking_runtime(tracking["id"])
    assert runtime["id"] == tracking["id"]
    assert runtime["heartbeat_interval"] == 1800


def test_cleanup_old_sync_flags(clean_db, tracking):
    assert ts.request_tracking_stop(tracking["chat_id"], tracking_id=tracking["id"])
    with db.get_db_cursor() as cursor:
        cursor.execute(
            """UPDATE sync_flags
               SET processed = 1, created_at = datetime('now', '-48 hours')
               WHERE tracking_id = ?""",
            (tracking["id"],),
        )
    removed = ts.cleanup_old_sync_flags(max_age_hours=24)
    assert removed >= 1
