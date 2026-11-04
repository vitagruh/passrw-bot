"""
Общий слой доступа к SQLite для бота и админ-панели.

Единая схема, миграции и настройки подключения (WAL, busy_timeout, FK),
чтобы бот и админка больше не расходились по структуре таблиц.
"""

import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterable, Optional
from zoneinfo import ZoneInfo

DATABASE_PATH = os.getenv("DATABASE_PATH", "data/ticket_bot.db")
LOCAL_TZ = ZoneInfo(os.getenv("LOCAL_TIMEZONE", "Europe/Minsk"))

# Общая блокировка записи внутри процесса бота.
DB_LOCK = threading.RLock()


def ensure_db_dir(path: Optional[str] = None) -> None:
    """Создаёт каталог для файла БД до первого подключения."""
    path = path or DATABASE_PATH
    directory = os.path.dirname(os.path.abspath(path))
    if directory:
        os.makedirs(directory, exist_ok=True)


def get_connection(path: Optional[str] = None) -> sqlite3.Connection:
    """Подключение с едиными pragma для всех процессов."""
    path = path or DATABASE_PATH
    ensure_db_dir(path)
    conn = sqlite3.connect(
        path,
        timeout=60.0,
        check_same_thread=False,
        isolation_level=None,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 60000")
    conn.execute("PRAGMA cache_size = -2000")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


@contextmanager
def get_db_cursor(path: Optional[str] = None):
    """Контекстный менеджер: commit/rollback и закрытие соединения."""
    conn = get_connection(path)
    try:
        cursor = conn.cursor()
        yield cursor
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _table_columns(cursor: sqlite3.Cursor, table: str) -> set:
    return {row["name"] for row in cursor.execute(f"PRAGMA table_info({table})")}


def _ensure_columns(cursor: sqlite3.Cursor, table: str, columns: dict) -> None:
    """Добавляет отсутствующие колонки в существующую таблицу (миграция)."""
    existing = _table_columns(cursor, table)
    for name, decl in columns.items():
        if name not in existing:
            cursor.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


def init_schema() -> None:
    """Создаёт все таблицы и выполняет мягкие миграции существующей БД."""
    ensure_db_dir()

    with get_db_cursor() as cursor:
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                chat_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                last_name TEXT,
                role TEXT DEFAULT 'user',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_active TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS active_trackings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                from_station TEXT NOT NULL,
                to_station TEXT NOT NULL,
                date TEXT NOT NULL,
                passengers INTEGER NOT NULL,
                train_time TEXT NOT NULL,
                train_num TEXT,
                heartbeat_enabled BOOLEAN DEFAULT 0,
                heartbeat_interval INTEGER DEFAULT 1800,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                requests_count INTEGER DEFAULT 0,
                last_request_count INTEGER DEFAULT 0,
                seats_available INTEGER DEFAULT 0,
                is_stopped BOOLEAN DEFAULT 0,
                unique_token TEXT,
                FOREIGN KEY (chat_id) REFERENCES users(chat_id) ON DELETE CASCADE
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS search_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                from_station TEXT NOT NULL,
                to_station TEXT NOT NULL,
                date TEXT NOT NULL,
                passengers INTEGER NOT NULL,
                searched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (chat_id) REFERENCES users(chat_id) ON DELETE CASCADE
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS popular_stations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                station_name TEXT UNIQUE NOT NULL,
                usage_count INTEGER DEFAULT 1,
                last_used TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS favorite_routes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                from_station TEXT NOT NULL,
                to_station TEXT NOT NULL,
                passengers INTEGER DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (chat_id) REFERENCES users(chat_id) ON DELETE CASCADE
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS user_stats (
                chat_id INTEGER PRIMARY KEY,
                total_searches INTEGER DEFAULT 0,
                successful_bookings INTEGER DEFAULT 0,
                total_savings REAL DEFAULT 0.0,
                last_stats_update TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (chat_id) REFERENCES users(chat_id) ON DELETE CASCADE
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS feature_flags (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                flag_name TEXT UNIQUE NOT NULL,
                is_enabled BOOLEAN DEFAULT 0,
                description TEXT,
                ab_test_group TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS webhook_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_type TEXT NOT NULL,
                payload TEXT,
                chat_id INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                sent BOOLEAN DEFAULT 0,
                retry_count INTEGER DEFAULT 0
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS user_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                action TEXT NOT NULL,
                details TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (chat_id) REFERENCES users(chat_id) ON DELETE CASCADE
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS bot_errors (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                error_type TEXT NOT NULL,
                error_message TEXT,
                stack_trace TEXT,
                chat_id INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (chat_id) REFERENCES users(chat_id) ON DELETE SET NULL
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS admin_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                admin_username TEXT NOT NULL,
                action TEXT NOT NULL,
                details TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                ip_address TEXT
            )
        """)

        # Флаги синхронизации между админкой и ботом.
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS sync_flags (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                train_time TEXT NOT NULL,
                tracking_id INTEGER,
                action TEXT NOT NULL,
                admin_username TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                processed BOOLEAN DEFAULT 0,
                processed_at TIMESTAMP
            )
        """)

        # История завершённых трекингов (счётчик запросов).
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS request_counter_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tracking_id INTEGER,
                chat_id INTEGER NOT NULL,
                requests_count INTEGER DEFAULT 0,
                reason TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # История наличия мест и цен (P2: тренды/алерты по цене).
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS availability_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tracking_id INTEGER,
                chat_id INTEGER NOT NULL,
                train_num TEXT,
                train_time TEXT,
                seats_available INTEGER DEFAULT 0,
                min_price REAL,
                checked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Настройки пользователя (P2: тихие часы, фильтры, пассажиры по умолчанию).
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS user_settings (
                chat_id INTEGER PRIMARY KEY,
                quiet_from TEXT,
                quiet_to TEXT,
                notify_threshold INTEGER DEFAULT 1,
                default_passengers INTEGER DEFAULT 1,
                carriage_filter TEXT,
                max_price REAL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (chat_id) REFERENCES users(chat_id) ON DELETE CASCADE
            )
        """)

        # Мягкие миграции для БД, созданных старыми версиями.
        _ensure_columns(cursor, "active_trackings", {
            "updated_at": "TIMESTAMP",
            "last_request_count": "INTEGER DEFAULT 0",
            "is_stopped": "BOOLEAN DEFAULT 0",
            "unique_token": "TEXT",
        })
        _ensure_columns(cursor, "sync_flags", {
            "tracking_id": "INTEGER",
            "admin_username": "TEXT",
        })
        _ensure_columns(cursor, "users", {
            "role": "TEXT DEFAULT 'user'",
        })

        cursor.execute("CREATE INDEX IF NOT EXISTS idx_trackings_chat ON active_trackings(chat_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_trackings_route ON active_trackings(chat_id, train_time)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_trackings_id ON active_trackings(id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_history_chat ON search_history(chat_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_stations_name ON popular_stations(station_name)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_favorites_chat ON favorite_routes(chat_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_feature_flags_name ON feature_flags(flag_name)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_user_logs_chat ON user_logs(chat_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_user_logs_created ON user_logs(created_at)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_bot_errors_type ON bot_errors(error_type)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_bot_errors_created ON bot_errors(created_at)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_bot_errors_chat ON bot_errors(chat_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_sync_chat ON sync_flags(chat_id, processed)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_sync_pending ON sync_flags(processed, created_at)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_sync_tracking ON sync_flags(tracking_id, processed)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_history_tracking ON availability_history(tracking_id, checked_at)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_request_history_chat ON request_counter_history(chat_id)")

        cursor.execute("""
            INSERT OR IGNORE INTO feature_flags (flag_name, is_enabled, description, ab_test_group)
            VALUES
                ('context_menu_enabled', 1, 'Включить контекстное меню под результатами', NULL),
                ('price_alerts_enabled', 1, 'Уведомления об изменении цены', NULL),
                ('smart_suggestions', 1, 'Умные подсказки маршрутов', NULL),
                ('new_ui_layout', 0, 'Новый UI layout результатов', NULL)
        """)


# ============================================
# ВРЕМЯ
# ============================================

def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def to_local(value) -> Optional[datetime]:
    """Преобразует UTC-строку/дату из SQLite в локальное время."""
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip().replace("T", " ")
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                dt = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
        else:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(LOCAL_TZ)


def fmt_local(value, fmt: str = "%d.%m.%Y %H:%M") -> str:
    """Человекочитаемое локальное время (Минск по умолчанию)."""
    dt = to_local(value)
    return dt.strftime(fmt) if dt else "—"


def local_now() -> datetime:
    return datetime.now(LOCAL_TZ)


# ============================================
# ОЧИСТКА СТАРЫХ ДАННЫХ
# ============================================

def cleanup_old_data(retention_days: int = 30) -> int:
    """Удаляет старые технические логи и историю. Возвращает число удалённых строк."""
    days = max(1, int(retention_days))
    deleted = 0
    with get_db_cursor() as cursor:
        for table, column in (
            ("user_logs", "created_at"),
            ("bot_errors", "created_at"),
            ("admin_logs", "created_at"),
            ("search_history", "searched_at"),
            ("availability_history", "checked_at"),
            ("request_counter_history", "created_at"),
        ):
            cursor.execute(
                f"DELETE FROM {table} WHERE {column} < datetime('now', ?)",
                (f"-{days} days",),
            )
            deleted += cursor.rowcount or 0
        cursor.execute(
            "DELETE FROM webhook_events WHERE sent = 1 AND created_at < datetime('now', ?)",
            (f"-{days} days",),
        )
        deleted += cursor.rowcount or 0
    return deleted


def iter_chunks(items: Iterable, size: int):
    chunk = []
    for item in items:
        chunk.append(item)
        if len(chunk) >= size:
            yield chunk
            chunk = []
    if chunk:
        yield chunk
