"""Конфигурация приложения из окружения и .env."""

from __future__ import annotations

import secrets
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    telegram_token: str = ""
    database_path: Path = Path("data/passrw.db")
    log_dir: Path = Path("logs")
    log_level: str = "INFO"

    local_timezone: str = "Europe/Minsk"

    check_interval: int = Field(60, ge=10)
    min_check_interval: int = Field(30, ge=10)
    http_cache_ttl: int = Field(30, ge=0)
    request_timeout: int = Field(15, ge=5)
    http_concurrency: int = Field(4, ge=1)
    max_passengers: int = Field(9, ge=1)
    max_trackings_per_user: int = Field(5, ge=1)
    notify_cooldown: int = Field(300, ge=0)
    stop_on_success: bool = True
    maintenance_interval: int = Field(21600, ge=300)
    retention_days: int = Field(30, ge=1)
    parse_failure_alert_threshold: int = Field(5, ge=1)
    # Окно продажи билетов на pass.rw.by (суток вперёд).
    sale_window_days: int = Field(60, ge=1, le=365)

    web_enabled: bool = True
    web_host: str = "127.0.0.1"
    web_port: int = 5000
    public_url: str = ""
    # Список доверенных reverse proxy через запятую. Пусто — X-Forwarded-For
    # игнорируется, клиентский IP берётся из реального подключения.
    trusted_proxies: str = ""

    admin_username: str = "admin"
    admin_password: str = ""
    admin_password_hash: str = ""
    secret_key: str = ""
    session_cookie_secure: bool = False
    session_timeout_minutes: int = Field(60, ge=5)
    login_max_attempts: int = Field(5, ge=1)
    login_lockout_seconds: int = Field(900, ge=30)
    rate_limit_requests: int = Field(100, ge=10)
    rate_limit_window: int = Field(60, ge=10)
    ip_whitelist: str = ""
    metrics_token: str = ""

    alert_chat_id: int | None = None

    @field_validator("log_level")
    @classmethod
    def _valid_log_level(cls, value: str) -> str:
        level = value.upper()
        if level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError(f"unknown log level: {value}")
        return level

    @field_validator("local_timezone")
    @classmethod
    def _valid_timezone(cls, value: str) -> str:
        ZoneInfo(value)
        return value

    @field_validator("alert_chat_id", mode="before")
    @classmethod
    def _blank_alert_chat_id(cls, value):
        # В .env.example переменная присутствует с пустым значением.
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @property
    def database_url(self) -> str:
        return f"sqlite+aiosqlite:///{self.database_path.as_posix()}"

    @property
    def timezone(self) -> ZoneInfo:
        return ZoneInfo(self.local_timezone)

    @property
    def ip_allowlist(self) -> list[str]:
        return [item.strip() for item in self.ip_whitelist.split(",") if item.strip()]

    @property
    def trusted_proxy_list(self) -> list[str]:
        return [item.strip() for item in self.trusted_proxies.split(",") if item.strip()]

    @property
    def proxy_headers_enabled(self) -> bool:
        return bool(self.trusted_proxy_list)

    @property
    def admin_auth_configured(self) -> bool:
        return bool(self.admin_password_hash or self.admin_password)


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    if not settings.secret_key:
        settings.secret_key = secrets.token_hex(32)
    return settings
