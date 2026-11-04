"""Общие фикстуры для тестов passrw-bot.

Переменные окружения задаются ДО импорта db/ticket_bot/admin_panel:
эти модули читают настройки и инициализируют схему на этапе импорта.
"""

import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

TEST_DIR = tempfile.mkdtemp(prefix="passrw_tests_")

os.environ["DATABASE_PATH"] = os.path.join(TEST_DIR, "test.db")
os.environ["SYNC_DIR"] = os.path.join(TEST_DIR, "sync")
os.environ["TELEGRAM_TOKEN"] = "123456:TEST_TOKEN"
os.environ["ADMIN_USERNAME"] = "admin"
os.environ["ADMIN_PASSWORD"] = "testpass123"
os.environ["ADMIN_PASSWORD_HASH"] = ""
os.environ["SECRET_KEY"] = "test-secret-key"
os.environ["IP_WHITELIST"] = ""
os.environ["CHECK_INTERVAL"] = "10"
os.environ["MAINTENANCE_INTERVAL"] = "300"
os.environ["LOG_LEVEL"] = "WARNING"

import pytest  # noqa: E402

import db  # noqa: E402

CLEAN_ORDER = (
    "availability_history",
    "request_counter_history",
    "sync_flags",
    "active_trackings",
    "search_history",
    "favorite_routes",
    "user_stats",
    "user_settings",
    "user_logs",
    "bot_errors",
    "admin_logs",
    "webhook_events",
    "users",
)


@pytest.fixture(scope="session", autouse=True)
def _schema():
    db.init_schema()
    yield
    shutil.rmtree(TEST_DIR, ignore_errors=True)


@pytest.fixture()
def clean_db():
    """Чистые таблицы перед каждым тестом (feature_flags не трогаем)."""
    db.init_schema()
    with db.get_db_cursor() as cursor:
        for table in CLEAN_ORDER:
            cursor.execute(f"DELETE FROM {table}")
    yield db


@pytest.fixture()
def user(clean_db):
    with clean_db.get_db_cursor() as cursor:
        cursor.execute(
            "INSERT INTO users (chat_id, username, first_name, role) VALUES (?, ?, ?, ?)",
            (1001, "tester", "Test", "user"),
        )
    return 1001


@pytest.fixture()
def tracking(clean_db, user):
    with clean_db.get_db_cursor() as cursor:
        cursor.execute(
            """INSERT INTO active_trackings
               (chat_id, from_station, to_station, date, passengers, train_time,
                train_num, heartbeat_enabled, heartbeat_interval)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (user, "Минск", "Брест", "2030-12-01", 2, "12:30", "701Б", 0, 1800),
        )
        tracking_id = cursor.execute("SELECT id FROM active_trackings").fetchone()["id"]
    return {"id": tracking_id, "chat_id": user}
