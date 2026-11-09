"""ORM-модели. Схема создаётся автоматически при старте приложения."""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from passrw.util.time import utcnow


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    chat_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    username: Mapped[str | None] = mapped_column(String(64))
    first_name: Mapped[str | None] = mapped_column(String(128))
    last_name: Mapped[str | None] = mapped_column(String(128))
    role: Mapped[str] = mapped_column(String(16), default="user")  # user | moderator | admin
    is_blocked: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_active: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    trackings: Mapped[list[Tracking]] = relationship(back_populates="user", cascade="all, delete-orphan")


class Tracking(Base):
    __tablename__ = "trackings"
    __table_args__ = (
        Index("ix_trackings_chat_active", "chat_id", "is_active"),
        Index("ix_trackings_active_date", "is_active", "travel_date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.chat_id", ondelete="CASCADE"), index=True
    )
    from_station: Mapped[str] = mapped_column(String(64))
    to_station: Mapped[str] = mapped_column(String(64))
    travel_date: Mapped[date] = mapped_column(Date)
    passengers: Mapped[int] = mapped_column(Integer, default=1)
    train_number: Mapped[str] = mapped_column(String(16), default="")
    depart_time: Mapped[str] = mapped_column(String(8))

    # Переопределения пользовательских фильтров; NULL — взять из user_settings.
    notify_threshold: Mapped[int | None] = mapped_column(Integer)
    carriage_filter: Mapped[str | None] = mapped_column(String(32))
    max_price: Mapped[float | None] = mapped_column(Float)
    stop_on_success: Mapped[bool] = mapped_column(Boolean, default=True)

    heartbeat_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    heartbeat_interval: Mapped[int] = mapped_column(Integer, default=1800)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    stop_reason: Mapped[str | None] = mapped_column(String(32))
    stopped_at: Mapped[datetime | None] = mapped_column(DateTime)
    requests_count: Mapped[int] = mapped_column(Integer, default=0)
    seats_available: Mapped[int] = mapped_column(Integer, default=0)
    min_price: Mapped[float | None] = mapped_column(Float)
    last_signature: Mapped[str | None] = mapped_column(String(32))
    last_notified_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_error: Mapped[str | None] = mapped_column(String(256))
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    layout_alerted: Mapped[bool] = mapped_column(Boolean, default=False)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    user: Mapped[User] = relationship(back_populates="trackings")
    samples: Mapped[list[AvailabilitySample]] = relationship(
        back_populates="tracking", cascade="all, delete-orphan", passive_deletes=True
    )


class AvailabilitySample(Base):
    __tablename__ = "availability_samples"
    __table_args__ = (Index("ix_samples_tracking_time", "tracking_id", "checked_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tracking_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("trackings.id", ondelete="CASCADE"), index=True
    )
    chat_id: Mapped[int] = mapped_column(BigInteger)
    train_number: Mapped[str | None] = mapped_column(String(16))
    seats: Mapped[int] = mapped_column(Integer, default=0)
    min_price: Mapped[float | None] = mapped_column(Float)
    checked_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    tracking: Mapped[Tracking] = relationship(back_populates="samples")


class SearchHistory(Base):
    __tablename__ = "search_history"
    __table_args__ = (Index("ix_search_history_chat", "chat_id", "searched_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(BigInteger)
    from_station: Mapped[str] = mapped_column(String(64))
    to_station: Mapped[str] = mapped_column(String(64))
    travel_date: Mapped[date] = mapped_column(Date)
    passengers: Mapped[int] = mapped_column(Integer, default=1)
    searched_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class FavoriteRoute(Base):
    __tablename__ = "favorite_routes"
    __table_args__ = (UniqueConstraint("chat_id", "from_station", "to_station", name="uq_favorite"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, index=True)
    from_station: Mapped[str] = mapped_column(String(64))
    to_station: Mapped[str] = mapped_column(String(64))
    passengers: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class UserSettings(Base):
    __tablename__ = "user_settings"

    chat_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.chat_id", ondelete="CASCADE"), primary_key=True, autoincrement=False
    )
    quiet_from: Mapped[str | None] = mapped_column(String(5))
    quiet_to: Mapped[str | None] = mapped_column(String(5))
    notify_threshold: Mapped[int] = mapped_column(Integer, default=1)
    default_passengers: Mapped[int] = mapped_column(Integer, default=1)
    carriage_filter: Mapped[str | None] = mapped_column(String(32))
    max_price: Mapped[float | None] = mapped_column(Float)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class UserStats(Base):
    __tablename__ = "user_stats"

    chat_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.chat_id", ondelete="CASCADE"), primary_key=True, autoincrement=False
    )
    total_searches: Mapped[int] = mapped_column(Integer, default=0)
    trackings_created: Mapped[int] = mapped_column(Integer, default=0)
    notifications_sent: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class PopularStation(Base):
    __tablename__ = "popular_stations"

    name: Mapped[str] = mapped_column(String(64), primary_key=True)
    usage_count: Mapped[int] = mapped_column(Integer, default=1)
    last_used: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class UserLog(Base):
    __tablename__ = "user_logs"
    __table_args__ = (Index("ix_user_logs_created", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, index=True)
    action: Mapped[str] = mapped_column(String(48))
    details: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class BotError(Base):
    __tablename__ = "bot_errors"
    __table_args__ = (Index("ix_bot_errors_created", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    error_type: Mapped[str] = mapped_column(String(64), index=True)
    message: Mapped[str | None] = mapped_column(Text)
    stack_trace: Mapped[str | None] = mapped_column(Text)
    chat_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class AdminLog(Base):
    __tablename__ = "admin_logs"
    __table_args__ = (Index("ix_admin_logs_created", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    admin_username: Mapped[str] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(64))
    details: Mapped[str | None] = mapped_column(Text)
    ip_address: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
