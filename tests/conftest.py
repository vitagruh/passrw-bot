"""Общие фикстуры: изолированная БД и настройки на каждый тест."""

from __future__ import annotations

import os
import tempfile

# Модуль passrw.db создаёт Engine при импорте: уводим его из рабочего каталога.
_IMPORT_TMP = tempfile.mkdtemp(prefix="passrw-import-")
os.environ.setdefault("DATABASE_PATH", os.path.join(_IMPORT_TMP, "import.db"))
os.environ.setdefault("SECRET_KEY", "test-secret")

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402

from passrw.config import Settings  # noqa: E402
from passrw.db import Database  # noqa: E402


def make_settings(tmp_path, **overrides) -> Settings:
    values = {
        "telegram_token": "123456:TESTTOKEN",
        "database_path": tmp_path / "test.db",
        "log_dir": tmp_path / "logs",
        "secret_key": "test-secret",
        "admin_username": "admin",
        "admin_password": "testpass123",
        "web_enabled": False,
        "check_interval": 60,
        "min_check_interval": 30,
        "maintenance_interval": 21600,
        "login_max_attempts": 5,
        "login_lockout_seconds": 900,
        "rate_limit_requests": 1000,
        "rate_limit_window": 60,
        "session_timeout_minutes": 60,
    }
    values.update(overrides)
    return Settings(**values)


@pytest.fixture
def settings(tmp_path) -> Settings:
    return make_settings(tmp_path)


@pytest_asyncio.fixture
async def database(settings: Settings):
    db = Database(settings.database_url)
    await db.create_all()
    try:
        yield db
    finally:
        await db.dispose()
