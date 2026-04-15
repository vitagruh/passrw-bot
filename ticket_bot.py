import telebot
import time
import threading
import re
import sqlite3
import json
from datetime import datetime, timedelta
from urllib.parse import urlencode
import requests
from bs4 import BeautifulSoup
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton
from telebot_calendar import Calendar, RUSSIAN_LANGUAGE
from dotenv import load_dotenv
import os
import logging
from logging.handlers import RotatingFileHandler
import traceback
from fake_useragent import UserAgent
from contextlib import contextmanager
from typing import Optional, Dict, List, Any

# Импорт модуля синхронизации для интеграции с админ-панелью
from tracking_sync import (
    check_stop_request,
    is_tracking_active_in_db,
    confirm_tracking_stopped,
    cleanup_old_sync_flags,
    get_pending_sync_actions
)


# ============================================
# НАСТРОЙКА ЛОГИРОВАНИЯ (BEST PRACTICES)
# ============================================

def setup_logger(name: str = 'TicketBot') -> logging.Logger:
    """
    Создает и настраивает логгер с консольным и файловым выводом.
    
    Best practices:
    - RotatingFileHandler для автоматической ротации логов
    - Структурированный формат с информацией о месте вызова
    - Разделение уровней ERROR/CRITICAL в отдельный файл
    - Поддержка уровня логирования через переменную окружения
    """
    logger = logging.getLogger(name)
    
    # Получаем уровень логирования из переменной окружения (по умолчанию INFO)
    log_level = os.getenv('LOG_LEVEL', 'INFO').upper()
    numeric_level = getattr(logging, log_level, logging.INFO)
    
    logger.setLevel(numeric_level)  # Устанавливаем уровень логирования из переменной окружения
    
    # Формат с детальной информацией (время, уровень, модуль, функция, строка, сообщение)
    detailed_formatter = logging.Formatter(
        fmt='%(asctime)s | %(levelname)-8s | %(name)s | %(funcName)s:%(lineno)d | %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    
    # Упрощенный формат для консоли (короче и читаемее)
    console_formatter = logging.Formatter(
        fmt='%(asctime)s | %(levelname)-8s | %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    
    # Консольный обработчик (выводит всё от DEBUG и выше)
    console_handler = logging.StreamHandler()
    console_handler.setLevel(numeric_level)
    console_handler.setFormatter(console_formatter)
    logger.addHandler(console_handler)
    
    # Файловый обработчик с ротацией
    # maxBytes=10MB, backupCount=5 файлов (итого до 50MB логов)
    log_file = os.getenv('LOG_FILE', 'logs/bot.log')
    log_dir = os.path.dirname(log_file)
    if log_dir and not os.path.exists(log_dir):
        os.makedirs(log_dir, exist_ok=True)
    
    file_handler = RotatingFileHandler(
        filename=log_file,
        encoding='utf-8',
        maxBytes=10 * 1024 * 1024,  # 10 MB
        backupCount=5,
        delay=False  # Создавать файл сразу при инициализации
    )
    file_handler.setLevel(numeric_level)  # В файл пишем от уровня LOG_LEVEL
    file_handler.setFormatter(detailed_formatter)
    logger.addHandler(file_handler)
    
    # Отдельный файл для ошибок (ERROR и CRITICAL)
    error_log_file = os.getenv('ERROR_LOG_FILE', 'logs/error.log')
    error_log_dir = os.path.dirname(error_log_file)
    if error_log_dir and not os.path.exists(error_log_dir):
        os.makedirs(error_log_dir, exist_ok=True)
    
    error_handler = RotatingFileHandler(
        filename=error_log_file,
        encoding='utf-8',
        maxBytes=5 * 1024 * 1024,  # 5 MB
        backupCount=3,
        delay=False  # Создавать файл сразу при инициализации
    )
    error_handler.setLevel(logging.ERROR)
    error_handler.setFormatter(detailed_formatter)
    logger.addHandler(error_handler)
    
    # Запрещаем распространение логов вверх по иерархии
    logger.propagate = False
    
    return logger


# Создаем основной логгер
logger = setup_logger('TicketBot')


def get_user_profile_info(message) -> str:
    """
    Формирует строку с информацией о пользователе для логирования.
    
    Примечание по безопасности и архитектуре:
    - При использовании polling (bot.polling()) мы НЕ имеем доступа к реальному IP клиента.
      Запросы идут от серверов Telegram, поэтому IP в логах будет принадлежать Telegram.
    - Для получения реального IP необходимо использовать Webhook режим и анализировать 
      заголовки X-Forwarded-For, но это требует настройки HTTPS и reverse-proxy (nginx).
    - Мы логируем доступные данные профиля: ID, Username, Имя, Язык.
    """
    if not message or not message.from_user:
        return "Unknown User"
    
    user = message.from_user
    username = f"@{user.username}" if user.username else "NoUsername"
    full_name = user.full_name.replace('|', ' ')  # Экранируем разделитель
    lang = user.language_code or "unknown"
    
    # Формат: ID[12345] User[@name] Name[Ivan] Lang[ru]
    return f"ID[{user.id}] User[{username}] Name[{full_name}] Lang[{lang}]"


def log_action(message, action: str, details: str = ""):
    """
    Единая функция логирования действий пользователя с расширенной информацией.
    Также записывает действие в базу данных для отображения в админ-панели.
    
    :param message: Объект сообщения или callback query
    :param action: Тип действия (START, SEARCH, ERROR, XSS_ATTEMPT и т.д.)
    :param details: Дополнительные детали (маршрут, дата, ошибка)
    """
    # Обработка CallbackQuery (для кнопок календаря и inline кнопок)
    if hasattr(message, 'message') and hasattr(message, 'data'):
        # Это CallbackQuery
        user_info = get_user_profile_info(message.message)
        chat_id = message.message.chat.id
    else:
        # Обычное сообщение
        user_info = get_user_profile_info(message)
        chat_id = message.chat.id if hasattr(message, 'chat') else "Unknown"

    # Проверка на потенциальные XSS атаки в деталях
    is_security_threat = False
    if details and any(tag in details.lower() for tag in ['<script', 'javascript:', 'onerror=', 'onclick=']):
        is_security_threat = True
        action = "SECURITY_XSS_ATTEMPT"
    
    log_entry = f"{user_info} | Action[{action}] | Details[{details}]"
    
    if is_security_threat:
        logger.warning(f"🚨 {log_entry}")
    elif action.startswith("ERROR") or action == "CRITICAL":
        logger.error(f"❌ {log_entry}")
    else:
        logger.info(f"ℹ️ {log_entry}")
    
    # Запись в базу данных для админ-панели (если chat_id числовой)
    if isinstance(chat_id, int):
        try:
            log_user_action(chat_id, action, details)
        except Exception as e:
            logger.error(f"Ошибка записи лога в БД: {e}")


def log_exception(logger_instance: logging.Logger, message: str = "Произошла ошибка", chat_id: int = None):
    """
    Вспомогательная функция для логирования исключений с полным traceback.
    Также записывает ошибку в базу данных для отображения в админ-панели.
    
    :param logger_instance: Экземпляр логгера
    :param message: Сообщение об ошибке
    :param chat_id: ID пользователя (опционально, для привязки к пользователю)
    """
    error_traceback = traceback.format_exc()
    logger_instance.error(f"{message}: {error_traceback}")
    
    # Запись ошибки в базу данных для админ-панели
    if chat_id is not None and isinstance(chat_id, int):
        try:
            log_user_action(chat_id, f"ERROR: {message}", error_traceback[:500])  # Ограничиваем длину
        except Exception as e:
            logger_instance.error(f"Ошибка записи исключения в БД: {e}")
    
    # Запись в таблицу bot_errors для глобального мониторинга ошибок
    try:
        with get_db_cursor_locked() as cursor:
            cursor.execute("""
                INSERT INTO bot_errors (error_type, error_message, stack_trace, chat_id, created_at)
                VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
            """, (message.split(':')[0], message, error_traceback[:2000], chat_id))
    except Exception as e:
        logger_instance.error(f"Ошибка записи в таблицу bot_errors: {e}")


# --- ЗАГРУЗКА .ENV ---
load_dotenv()

TOKEN = os.getenv("TELEGRAM_TOKEN")
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", 60))
DATABASE_PATH = os.getenv("DATABASE_PATH", "data/ticket_bot.db")

if not TOKEN:
    logger.critical("❌ TOKEN не найден в .env. Выход.")
    exit(1)

bot = telebot.TeleBot(TOKEN, threaded=True)

# Инициализация календаря для выбора даты
calendar = Calendar(language=RUSSIAN_LANGUAGE)

# Инициализация UserAgent с обработкой ошибок
try:
    ua = UserAgent(browsers=['chrome', 'firefox', 'edge'])
    logger.info("✅ UserAgent успешно инициализирован")
except Exception as e:
    logger.warning(f"⚠️ Ошибка инициализации UserAgent: {e}. Используем запасной вариант.")
    ua = None

# ============================================
# БАЗА ДАННЫХ (ПЕРСИСТЕНТНОЕ ХРАНИЛИЩЕ)
# ============================================

# Глобальная блокировка для синхронизации доступа к БД
db_lock = threading.Lock()

def get_db_connection():
    """Создает подключение к базе данных с поддержкой внешних ключей и таймаутом"""
    conn = sqlite3.connect(DATABASE_PATH, timeout=60.0, check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # Устанавливаем режим WAL для лучшей поддержки конкурентного доступа
    conn.execute("PRAGMA journal_mode = WAL")
    # Устанавливаем busy_timeout (в миллисекундах)
    conn.execute("PRAGMA busy_timeout = 60000")
    # Увеличиваем размер кэша страниц
    conn.execute("PRAGMA cache_size = -2000")
    # Синхронизация - NORMAL для баланса между производительностью и надежностью
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn

@contextmanager
def get_db_cursor():
    """Контекстный менеджер для работы с базой данных с блокировкой для записи"""
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        yield cursor
        conn.commit()
    except Exception as e:
        conn.rollback()
        logger.error(f"❌ Ошибка базы данных: {e}")
        raise
    finally:
        conn.close()

@contextmanager
def get_db_cursor_locked():
    """Контекстный менеджер с явной блокировкой для операций записи"""
    with db_lock:
        conn = get_db_connection()
        try:
            cursor = conn.cursor()
            yield cursor
            conn.commit()
        except Exception as e:
            conn.rollback()
            logger.error(f"❌ Ошибка базы данных: {e}")
            raise
        finally:
            conn.close()

def init_database():
    """Инициализация таблиц базы данных"""
    # Создаем директорию для базы данных если не существует
    db_dir = os.path.dirname(DATABASE_PATH)
    if db_dir and not os.path.exists(db_dir):
        os.makedirs(db_dir, exist_ok=True)
    
    with get_db_cursor_locked() as cursor:
        # Таблица пользователей с ролевой моделью
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                chat_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                last_name TEXT,
                role TEXT DEFAULT 'user',  -- 'user', 'admin', 'moderator'
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_active TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        # Таблица активных трекингов
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
                requests_count INTEGER DEFAULT 0,
                seats_available INTEGER DEFAULT 0,
                FOREIGN KEY (chat_id) REFERENCES users(chat_id) ON DELETE CASCADE
            )
        """)
        
        # Таблица истории поисков
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
        
        # Таблица популярных станций
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS popular_stations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                station_name TEXT UNIQUE NOT NULL,
                usage_count INTEGER DEFAULT 1,
                last_used TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        # Таблица избранных маршрутов пользователей
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
        
        # Таблица статистики пользователей
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
        
        # Таблица feature flags для A/B тестирования
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS feature_flags (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                flag_name TEXT UNIQUE NOT NULL,
                is_enabled BOOLEAN DEFAULT 0,
                description TEXT,
                ab_test_group TEXT,  -- 'A', 'B', или NULL для всех
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        # Таблица событий для webhook интеграции
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
        
        # Таблица логов действий пользователей (для админ-панели)
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
        
        # Таблица ошибок бота (для мониторинга в админ-панели)
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
        
        # Индексы для ускорения поиска
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_trackings_chat ON active_trackings(chat_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_history_chat ON search_history(chat_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_stations_name ON popular_stations(station_name)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_favorites_chat ON favorite_routes(chat_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_feature_flags_name ON feature_flags(flag_name)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_user_logs_chat ON user_logs(chat_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_user_logs_created ON user_logs(created_at)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_bot_errors_type ON bot_errors(error_type)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_bot_errors_created ON bot_errors(created_at)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_bot_errors_chat ON bot_errors(chat_id)")
        
        # Инициализация default feature flags
        cursor.execute("""
            INSERT OR IGNORE INTO feature_flags (flag_name, is_enabled, description, ab_test_group)
            VALUES 
                ('context_menu_enabled', 1, 'Включить контекстное меню под результатами', NULL),
                ('price_alerts_enabled', 0, 'Уведомления об изменении цены', NULL),
                ('smart_suggestions', 1, 'Умные подсказки маршрутов', 'A'),
                ('new_ui_layout', 0, 'Новый UI layout результатов', 'B')
        """)
        
        logger.info("✅ База данных успешно инициализирована")

def save_user(chat_id: int, username: str = None, first_name: str = None, last_name: str = None, role: str = None):
    """Сохраняет или обновляет информацию о пользователе с поддержкой ролей"""
    with get_db_cursor_locked() as cursor:
        if role:
            cursor.execute("""
                INSERT INTO users (chat_id, username, first_name, last_name, role, last_active)
                VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(chat_id) DO UPDATE SET
                    username = excluded.username,
                    first_name = excluded.first_name,
                    last_name = excluded.last_name,
                    role = excluded.role,
                    last_active = CURRENT_TIMESTAMP
            """, (chat_id, username, first_name, last_name, role))
        else:
            cursor.execute("""
                INSERT INTO users (chat_id, username, first_name, last_name, last_active)
                VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(chat_id) DO UPDATE SET
                    username = excluded.username,
                    first_name = excluded.first_name,
                    last_name = excluded.last_name,
                    last_active = CURRENT_TIMESTAMP
            """, (chat_id, username, first_name, last_name))

def get_user_role(chat_id: int) -> str:
    """Получает роль пользователя"""
    with get_db_cursor() as cursor:
        cursor.execute("SELECT role FROM users WHERE chat_id = ?", (chat_id,))
        row = cursor.fetchone()
        return row['role'] if row else 'user'

def is_admin(chat_id: int) -> bool:
    """Проверяет, является ли пользователь администратором"""
    role = get_user_role(chat_id)
    return role in ['admin', 'moderator']

def log_user_action(chat_id: int, action: str, details: str = ""):
    """Логирует действия пользователей в базу данных для админ-панели"""
    with get_db_cursor_locked() as cursor:
        cursor.execute("""
            INSERT INTO user_logs (chat_id, action, details, created_at)
            VALUES (?, ?, ?, CURRENT_TIMESTAMP)
        """, (chat_id, action, details))

def save_tracking_to_db(chat_id: int, from_station: str, to_station: str, 
                        date: str, passengers: int, train_time: str, 
                        heartbeat_enabled: bool = False, heartbeat_interval: int = 1800):
    """Сохраняет активный трекинг в базу данных"""
    with get_db_cursor_locked() as cursor:
        cursor.execute("""
            INSERT INTO active_trackings 
            (chat_id, from_station, to_station, date, passengers, train_time, 
             heartbeat_enabled, heartbeat_interval)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (chat_id, from_station, to_station, date, passengers, train_time,
              1 if heartbeat_enabled else 0, heartbeat_interval))

def remove_tracking_from_db(chat_id: int, tracking_id: int = None, train_time: str = None):
    """Удаляет трекинг из базы данных по ID или train_time"""
    with get_db_cursor_locked() as cursor:
        if tracking_id:
            # Удаляем по уникальному ID (предпочтительный способ)
            cursor.execute("""
                DELETE FROM active_trackings 
                WHERE id = ? AND chat_id = ?
            """, (tracking_id, chat_id))
        elif train_time:
            # Удаляем по train_time (для обратной совместимости)
            cursor.execute("""
                DELETE FROM active_trackings 
                WHERE chat_id = ? AND train_time = ?
            """, (chat_id, train_time))
        else:
            # Удаляем все трекинги пользователя
            cursor.execute("""
                DELETE FROM active_trackings 
                WHERE chat_id = ?
            """, (chat_id,))

def get_user_trackings(chat_id: int) -> List[sqlite3.Row]:
    """Получает все активные трекинги пользователя"""
    with get_db_cursor() as cursor:
        cursor.execute("""
            SELECT * FROM active_trackings 
            WHERE chat_id = ?
        """, (chat_id,))
        return cursor.fetchall()

def update_tracking_status(chat_id: int, train_time: str, seats_available: int, 
                           train_num: str = None, requests_count: int = None):
    """Обновляет статус трекинга в базе данных"""
    with get_db_cursor_locked() as cursor:
        updates = []
        params = []
        
        if seats_available is not None:
            updates.append("seats_available = ?")
            params.append(seats_available)
        
        if train_num is not None:
            updates.append("train_num = ?")
            params.append(train_num)
        
        if requests_count is not None:
            updates.append("requests_count = ?")
            params.append(requests_count)
        
        if updates:
            params.extend([chat_id, train_time])
            query = f"UPDATE active_trackings SET {', '.join(updates)} WHERE chat_id = ? AND train_time = ?"
            cursor.execute(query, params)

def save_search_history(chat_id: int, from_station: str, to_station: str, 
                        date: str, passengers: int):
    """Сохраняет поиск в историю"""
    with get_db_cursor_locked() as cursor:
        cursor.execute("""
            INSERT INTO search_history 
            (chat_id, from_station, to_station, date, passengers)
            VALUES (?, ?, ?, ?, ?)
        """, (chat_id, from_station, to_station, date, passengers))
        
        # Обновляем статистику пользователя (+1 поиск)
        update_user_stats_locked(chat_id, searches_increment=1)
        
        # Обновляем счетчик использования станций
        for station in [from_station, to_station]:
            cursor.execute("""
                INSERT INTO popular_stations (station_name, usage_count, last_used)
                VALUES (?, 1, CURRENT_TIMESTAMP)
                ON CONFLICT(station_name) DO UPDATE SET
                    usage_count = usage_count + 1,
                    last_used = CURRENT_TIMESTAMP
            """, (station,))

def get_user_search_history(chat_id: int, limit: int = 5) -> List[sqlite3.Row]:
    """Получает последние поиски пользователя"""
    with get_db_cursor() as cursor:
        cursor.execute("""
            SELECT * FROM search_history 
            WHERE chat_id = ?
            ORDER BY searched_at DESC
            LIMIT ?
        """, (chat_id, limit))
        return cursor.fetchall()

def get_popular_stations(limit: int = 10) -> List[sqlite3.Row]:
    """Получает популярные станции"""
    with get_db_cursor() as cursor:
        cursor.execute("""
            SELECT station_name, usage_count 
            FROM popular_stations 
            ORDER BY usage_count DESC, last_used DESC
            LIMIT ?
        """, (limit,))
        return cursor.fetchall()

# ============================================
# НОВЫЕ ФУНКЦИИ ДЛЯ HIGH PRIORITY ФУНКЦИОНАЛА
# ============================================

def save_favorite_route(chat_id: int, from_station: str, to_station: str, passengers: int = 1):
    """Сохраняет маршрут в избранные"""
    with get_db_cursor_locked() as cursor:
        cursor.execute("""
            INSERT INTO favorite_routes (chat_id, from_station, to_station, passengers)
            VALUES (?, ?, ?, ?)
        """, (chat_id, from_station, to_station, passengers))
        logger.info(f"✅ Маршрут {from_station} -> {to_station} добавлен в избранное для пользователя {chat_id}")

def get_user_favorite_routes(chat_id: int) -> List[sqlite3.Row]:
    """Получает избранные маршруты пользователя"""
    with get_db_cursor() as cursor:
        cursor.execute("""
            SELECT * FROM favorite_routes 
            WHERE chat_id = ?
            ORDER BY created_at DESC
        """, (chat_id,))
        return cursor.fetchall()

def remove_favorite_route(chat_id: int, route_id: int) -> bool:
    """Удаляет маршрут из избранных"""
    with get_db_cursor_locked() as cursor:
        cursor.execute("""
            DELETE FROM favorite_routes 
            WHERE id = ? AND chat_id = ?
        """, (route_id, chat_id))
        deleted = cursor.rowcount > 0
        if deleted:
            logger.info(f"✅ Маршрут ID={route_id} удален из избранных для пользователя {chat_id}")
        return deleted

def update_user_stats(chat_id: int, searches_increment: int = 0, bookings_increment: int = 0, savings: float = 0.0):
    """Обновляет статистику пользователя (без блокировки - для внутреннего использования)"""
    with get_db_cursor() as cursor:
        # Сначала проверяем, есть ли запись
        cursor.execute("SELECT chat_id FROM user_stats WHERE chat_id = ?", (chat_id,))
        exists = cursor.fetchone()
        
        if exists:
            cursor.execute("""
                UPDATE user_stats 
                SET total_searches = total_searches + ?,
                    successful_bookings = successful_bookings + ?,
                    total_savings = total_savings + ?,
                    last_stats_update = CURRENT_TIMESTAMP
                WHERE chat_id = ?
            """, (searches_increment, bookings_increment, savings, chat_id))
        else:
            cursor.execute("""
                INSERT INTO user_stats (chat_id, total_searches, successful_bookings, total_savings)
                VALUES (?, ?, ?, ?)
            """, (chat_id, searches_increment, bookings_increment, savings))
        
        logger.info(f"📊 Статистика пользователя {chat_id} обновлена: +{searches_increment} поисков, +{bookings_increment} бронирований, +{savings} BYN savings")

def update_user_stats_locked(chat_id: int, searches_increment: int = 0, bookings_increment: int = 0, savings: float = 0.0):
    """Обновляет статистику пользователя с блокировкой (для вызова извне)"""
    with get_db_cursor_locked() as cursor:
        # Сначала проверяем, есть ли запись
        cursor.execute("SELECT chat_id FROM user_stats WHERE chat_id = ?", (chat_id,))
        exists = cursor.fetchone()
        
        if exists:
            cursor.execute("""
                UPDATE user_stats 
                SET total_searches = total_searches + ?,
                    successful_bookings = successful_bookings + ?,
                    total_savings = total_savings + ?,
                    last_stats_update = CURRENT_TIMESTAMP
                WHERE chat_id = ?
            """, (searches_increment, bookings_increment, savings, chat_id))
        else:
            cursor.execute("""
                INSERT INTO user_stats (chat_id, total_searches, successful_bookings, total_savings)
                VALUES (?, ?, ?, ?)
            """, (chat_id, searches_increment, bookings_increment, savings))
        
        logger.info(f"📊 Статистика пользователя {chat_id} обновлена: +{searches_increment} поисков, +{bookings_increment} бронирований, +{savings} BYN savings")

def get_user_stats(chat_id: int) -> Optional[sqlite3.Row]:
    """Получает статистику пользователя"""
    with get_db_cursor() as cursor:
        cursor.execute("""
            SELECT * FROM user_stats 
            WHERE chat_id = ?
        """, (chat_id,))
        return cursor.fetchone()

def is_feature_enabled(flag_name: str, chat_id: int = None) -> bool:
    """
    Проверяет, включен ли feature flag.
    Поддерживает A/B тестирование через ab_test_group.
    """
    with get_db_cursor() as cursor:
        cursor.execute("""
            SELECT is_enabled, ab_test_group FROM feature_flags 
            WHERE flag_name = ?
        """, (flag_name,))
        result = cursor.fetchone()
        
        if not result:
            logger.warning(f"⚠️ Feature flag '{flag_name}' не найден, возвращаем False")
            return False
        
        is_enabled, ab_test_group = result['is_enabled'], result['ab_test_group']
        
        # Если нет A/B группы или chat_id не передан, возвращаем общий статус
        if not ab_test_group or chat_id is None:
            return bool(is_enabled)
        
        # Логика A/B тестирования: распределяем пользователей по группам
        # Пользователи с четным chat_id -> группа A, нечетным -> группа B
        user_group = 'A' if chat_id % 2 == 0 else 'B'
        
        if user_group == ab_test_group:
            return bool(is_enabled)
        else:
            return False

def set_feature_flag(flag_name: str, is_enabled: bool, description: str = ""):
    """Устанавливает значение feature flag (используется в админ-панели)"""
    with get_db_cursor_locked() as cursor:
        cursor.execute("""
            INSERT INTO feature_flags (flag_name, is_enabled, description, updated_at)
            VALUES (?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(flag_name) DO UPDATE SET
                is_enabled = excluded.is_enabled,
                description = excluded.description,
                updated_at = CURRENT_TIMESTAMP
        """, (flag_name, 1 if is_enabled else 0, description))
        logger.info(f"🚩 Feature flag '{flag_name}' установлен в {is_enabled}")

def get_all_feature_flags() -> List[sqlite3.Row]:
    """Получает все feature flags (для админ-панели)"""
    with get_db_cursor() as cursor:
        cursor.execute("""
            SELECT * FROM feature_flags 
            ORDER BY flag_name
        """)
        return cursor.fetchall()

def log_webhook_event(event_type: str, payload: Dict, chat_id: int = None):
    """Логгирует событие для webhook интеграции"""
    with get_db_cursor_locked() as cursor:
        cursor.execute("""
            INSERT INTO webhook_events (event_type, payload, chat_id)
            VALUES (?, ?, ?)
        """, (event_type, json.dumps(payload), chat_id))
        logger.info(f"🔗 Webhook событие '{event_type}' залоггировано для chat_id={chat_id}")

def restore_active_trackings(bot_instance):
    """Восстанавливает активные трекинги после перезапуска бота"""
    with get_db_cursor() as cursor:
        cursor.execute("SELECT * FROM active_trackings")
        trackings = cursor.fetchall()
    
    restored_count = 0
    for tracking in trackings:
        chat_id = tracking['chat_id']
        
        # Восстанавливаем данные в памяти
        user_data[chat_id] = {
            'from': tracking['from_station'],
            'to': tracking['to_station'],
            'date': tracking['date'],
            'passengers': tracking['passengers']
        }
        
        # Восстанавливаем heartbeat настройки
        if tracking['heartbeat_enabled']:
            heartbeat_enabled.add(chat_id)
            heartbeat_intervals[chat_id] = tracking['heartbeat_interval']
        
        # Запускаем поток трекинга
        log_message = f"From: {tracking['from_station']}, To: {tracking['to_station']}, Date: {tracking['date']}, Train: {tracking['train_time']}"
        # Создаем фейковый объект message для логирования (так как восстановление происходит без сообщения)
        class FakeMessage:
            from_user = type('User', (), {'id': chat_id, 'username': None, 'full_name': 'RestoredTrack', 'language_code': 'unknown', 'is_bot': False})()
            chat = type('Chat', (), {'id': chat_id})()
        fake_msg = FakeMessage()
        log_action(fake_msg, "TRACKING_RESTORED", log_message)
        
        thread = threading.Thread(
            target=tracking_worker,
            args=(chat_id, tracking['from_station'], tracking['to_station'], 
                  tracking['date'], tracking['train_time']),
            daemon=True
        )
        thread.start()
        
        # Восстанавливаем статус трекинга с сохранением счетчика запросов из БД
        tracking_status[chat_id] = {
            'id': tracking['id'],  # Сохраняем ID для корректного удаления
            'train_num': tracking['train_num'],
            'train_time': tracking['train_time'],
            'seats_available': tracking['seats_available'],
            'requests_count': tracking['requests_count'] if tracking['requests_count'] else 0
        }
        
        # Также сохраняем id в active_jobs для корректной остановки
        active_jobs[chat_id] = {
            'thread': thread, 
            'stop_flag': False,
            'id': tracking['id'],
            'train_time': tracking['train_time']
        }
        
        restored_count += 1
        logger.info(f"🔄 Восстановлен трекинг для пользователя {chat_id}: {tracking['from_station']} → {tracking['to_station']}")
    
    if restored_count > 0:
        logger.info(f"✅ Восстановлено {restored_count} активных трекингов после перезапуска")
    
    # Очищаем старые флаги синхронизации при старте
    cleanup_old_sync_flags(max_age_hours=24)
    logger.info("🧹 Выполнена очистка старых флагов синхронизации")

# Инициализация базы данных при старте
init_database()

# Запрещенные символы для XSS защиты
FORBIDDEN_CHARS_PATTERN = re.compile(r'''[<>"'&]''')


# --- ХРАНИЛИЩЕ ДАННЫХ И СОСТОЯНИЙ (IN-MEMORY) ---
active_jobs = {}  # {chat_id: {'thread': thread, 'stop_flag': False}}
user_steps = {}   # {chat_id: 'step_name'} - текущий шаг пользователя
user_data = {}    # {chat_id: {'from': ..., 'to': ..., 'date': ..., 'passengers': ...}}
heartbeat_enabled = set()  # Множество chat_id, у которых включен heartbeat

# Хранилище интервалов heartbeat: {chat_id: interval_seconds}
heartbeat_intervals = {}

# Хранилище статусов отслеживания: {chat_id: {'train_num': ..., 'train_time': ..., 'seats_available': ..., 'requests_count': ...}}
tracking_status = {}

# Rate limiting: {chat_id: {'last_request': timestamp, 'request_count': int}}
rate_limit_store = {}
RATE_LIMIT_WINDOW = 60  # Окно в секундах
RATE_LIMIT_MAX_REQUESTS = 30  # Максимум запросов в окно

# Популярные станции по умолчанию (если база еще пуста)
DEFAULT_STATIONS = [
    "Минск", "Москва", "Санкт-Петербург", "Гомель", "Брест",
    "Витебск", "Гродно", "Могилев", "Калининград", "Смоленск"
]

# --- ФУНКЦИИ ПАРСИНГА ---

def sanitize_input(text: str) -> str:
    """
    Очистка пользовательского ввода от потенциально опасных символов.
    Защита от XSS и инъекций.
    """
    if not text:
        return ""
    # Удаляем опасные символы
    text = FORBIDDEN_CHARS_PATTERN.sub('', text)
    # Ограничиваем длину
    return text[:100]


def check_rate_limit(chat_id: int) -> bool:
    """
    Проверка rate limiting для пользователя.
    Возвращает True если запрос разрешен, False если превышен лимит.
    """
    current_time = time.time()
    
    if chat_id not in rate_limit_store:
        rate_limit_store[chat_id] = {'last_request': current_time, 'request_count': 1}
        return True
    
    store = rate_limit_store[chat_id]
    
    # Если окно времени истекло, сбрасываем счетчик
    if current_time - store['last_request'] > RATE_LIMIT_WINDOW:
        rate_limit_store[chat_id] = {'last_request': current_time, 'request_count': 1}
        return True
    
    # Если в пределах окна
    if store['request_count'] >= RATE_LIMIT_MAX_REQUESTS:
        logger.warning(f"⚠️ Rate limit превышен для пользователя {chat_id}")
        return False
    
    # Увеличиваем счетчик
    rate_limit_store[chat_id]['request_count'] += 1
    return True


def get_headers():
    """Получение заголовков с случайным User-Agent"""
    if ua:
        try:
            return {
                "User-Agent": ua.random,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
                "Accept-Encoding": "gzip, deflate, br",
                "Connection": "keep-alive",
            }
        except Exception as e:
            logger.warning(f"⚠️ Ошибка получения User-Agent: {e}")
    
    # Запасной вариант
    return {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }

def get_trains_list(from_station, to_station, date, chat_id=None):
    """
    Получение списка поездов с логированием запроса.
    
    Args:
        from_station: станция отправления
        to_station: станция назначения
        date: дата поездки
        chat_id: ID чата пользователя (для логирования)
    """
    headers = get_headers()

    params = {"from": from_station, "to": to_station, "date": date}
    url = f"https://pass.rw.by/ru/route/?" + urlencode(params)

    # Логирование запроса с информацией о пользователе
    user_info = f"User(chat_id={chat_id})" if chat_id else "System"
    logger.info(f"🔍 Запрос поиска билетов: {user_info} | Маршрут: {from_station} → {to_station} | Дата: {date}")

    try:
        response = requests.get(url, headers=headers, timeout=15)
        response.raise_for_status()
        # Логирование успешного HTTP запроса
        logger.debug(f"✅ HTTP запрос успешен (status={response.status_code}) для {user_info}")
    except requests.Timeout:
        logger.error(f"⏱ Таймаут запроса к {url} | {user_info}")
        return []
    except requests.RequestException as e:
        logger.error(f"🌐 Ошибка запроса к {url}: {e} | {user_info}")
        return []

    soup = BeautifulSoup(response.text, 'html.parser')
    rows = soup.find_all('div', class_='sch-table__row-wrap')

    trains = []
    for row in rows:
        try:
            time_elem = row.find(class_='train-from-time')
            time_from = time_elem.get_text(strip=True) if time_elem else 'N/A'
            
            train_elem = row.find(class_='train-number')
            train_num = train_elem.get_text(strip=True) if train_elem else 'N/A'

            duration_elem = row.find(class_='train-duration-time') or row.find(class_='sch-table__duration')
            duration = duration_elem.get_text(strip=True) if duration_elem else 'N/A'

            status_cell = row.find(class_='cell-4')
            parsed_info = parse_carriage_info(status_cell) if status_cell else []

            trains.append({
                'time': time_from,
                'num': train_num,
                'duration': duration,
                'parsed_info': parsed_info
            })
        except Exception as e:
            logger.warning(f"⚠️ Пропущена строка поезда: {e} | {user_info}")
            continue

    # Логирование с номерами поездов и пользователем
    train_numbers = [t['num'] for t in trains]
    logger.info(f"✅ Найдено {len(trains)} поездов для {user_info} | Поезда №№: {', '.join(train_numbers) if train_numbers else 'нет данных'}")
    return trains

def parse_carriage_info(status_cell):
    carriages = []
    items = status_cell.find_all('div', class_='sch-table__t-item')

    for item in items:
        try:
            type_elem = item.find('div', class_='sch-table__t-name')
            carriage_type = type_elem.get_text(strip=True) if type_elem else "Неизвестный"

            quant_elem = item.find('a', class_='sch-table__t-quant')
            seats_span = quant_elem.find('span') if quant_elem else None
            seats_raw = seats_span.get_text(strip=True) if seats_span else "?"
            
            # Очищаем количество мест (убираем текст "мест", оставляем цифры или ставим 0)
            seats = "0"
            if seats_raw != "?":
                match = re.search(r'\d+', seats_raw)
                if match:
                    seats = match.group()
                else:
                    seats = "0"

            price_elem = item.find('span', class_='js-price')
            price_byn = price_elem.get('data-cost-byn') if price_elem else None
            if not price_byn:
                cost_span = item.find('span', class_='ticket-cost')
                price_byn = cost_span.get_text(strip=True) if cost_span else "?"

            carriages.append({
                'type': carriage_type,
                'seats': seats,
                'price_byn': price_byn
            })
        except Exception as e:
            logger.warning(f"⚠️ Ошибка парсинга вагона: {e}")
            continue

    return carriages

# --- ФУНКЦИИ ОТСЛЕЖИВАНИЯ ---

def tracking_worker(chat_id, from_station, to_station, date, selected_time):
    # Создаем фейковый объект message для логирования внутри потока
    class FakeMessage:
        from_user = type('User', (), {'id': chat_id, 'username': None, 'full_name': 'TrackingWorker', 'language_code': 'unknown', 'is_bot': False})()
        chat = type('Chat', (), {'id': chat_id})()
    fake_msg = FakeMessage()
    
    log_action(fake_msg, "TRACKING_STARTED", f"From: {from_station}, To: {to_station}, Date: {date}, Train: {selected_time}")
    
    num_passengers = user_data[chat_id].get('passengers', 1)
    last_heartbeat = time.time()
    # Получаем интервал heartbeat для этого пользователя (по умолчанию 1800 сек = 30 мин)
    hb_interval = heartbeat_intervals.get(chat_id, 1800)
    
    # Загружаем текущий requests_count и ID из базы данных (если трекинг уже существовал)
    existing_tracking = None
    tracking_id = None
    with get_db_cursor() as cursor:
        cursor.execute("""
            SELECT id, requests_count FROM active_trackings 
            WHERE chat_id = ? AND train_time = ?
        """, (chat_id, selected_time))
        row = cursor.fetchone()
        if row:
            existing_tracking = row['requests_count']
            tracking_id = row['id']
    
    # Инициализация статуса отслеживания для пользователя
    tracking_status[chat_id] = {
        'id': tracking_id,  # Сохраняем ID для корректного удаления
        'train_num': None,
        'train_time': selected_time,
        'seats_available': 0,
        'requests_count': existing_tracking if existing_tracking is not None else 0
    }

    while chat_id in active_jobs:
        try:
            # === ПРОВЕРКА СИНХРОНИЗАЦИИ С АДМИН-ПАНЕЛЬЮ ===
            # Проверяем наличие запроса на остановку от админ-панели
            if check_stop_request(chat_id, selected_time):
                logger.info(f"🛑 Получен запрос на остановку трекинга от админ-панели: {chat_id}:{selected_time}")
                break
            
            # Проверяем существование трекинга в БД (защита от прямого удаления через админку)
            if not is_tracking_active_in_db(chat_id, selected_time):
                logger.warning(f"⚠️ Трекинг {chat_id}:{selected_time} удален из БД напрямую. Завершаем поток.")
                break
            # ==================================================
            
            # Увеличиваем счетчик запросов
            if chat_id in tracking_status:
                tracking_status[chat_id]['requests_count'] += 1
            
            # Отправка heartbeat сообщения если включено и прошел заданный интервал
            if chat_id in heartbeat_enabled and (time.time() - last_heartbeat) >= hb_interval:
                log_action(fake_msg, "HEARTBEAT_SENT", f"Train: {selected_time}, Interval: {hb_interval}s")
                bot.send_message(chat_id, "💓 Бот работает, проверяю билеты...")
                last_heartbeat = time.time()

            trains = get_trains_list(from_station, to_station, date, chat_id)
            current_train = next((t for t in trains if t['time'] == selected_time), None)

            if not current_train:
                log_action(fake_msg, "TRACKING_TRAIN_NOT_FOUND", f"Train: {selected_time} not found in results")
                time.sleep(CHECK_INTERVAL)
                continue
            
            # Обновляем информацию о поезде в статусе
            if chat_id in tracking_status:
                tracking_status[chat_id]['train_num'] = current_train['num']
                # Находим максимальное количество доступных мест среди всех вагонов
                max_seats = 0
                for c in current_train['parsed_info']:
                    if c['seats'].isdigit():
                        seats = int(c['seats'])
                        if seats > max_seats:
                            max_seats = seats
                tracking_status[chat_id]['seats_available'] = max_seats
            
            # Обновляем статус в базе данных (с использованием ID для точного обновления)
            update_tracking_status(
                chat_id, 
                selected_time, 
                tracking_status[chat_id]['seats_available'],
                tracking_status[chat_id]['train_num'],
                tracking_status[chat_id]['requests_count']
            )

            suitable = [
                c for c in current_train['parsed_info']
                if c['seats'].isdigit() and int(c['seats']) >= num_passengers
            ]

            if suitable:
                msg = f"🎉 <b>УСПЕХ!</b> Места для {num_passengers} чел. в поезде {selected_time} появились!\n\n"
                msg += f"📍 {from_station} → {to_station}\n📅 {date}"
                bot.send_message(chat_id, msg, parse_mode="HTML")
                send_detailed_train_info(
                    chat_id, 
                    current_train, 
                    num_passengers,
                    with_button=True,
                    from_station=from_station,
                    to_station=to_station,
                    date=date
                )
                
                active_jobs.pop(chat_id, None)
                heartbeat_enabled.discard(chat_id)  # Убираем из heartbeat при успехе
                heartbeat_intervals.pop(chat_id, None)  # Очищаем интервал при успехе
                tracking_status.pop(chat_id, None)  # Очищаем статус при успехе
                
                # Удаляем из базы данных после успешного завершения
                remove_tracking_from_db(chat_id, selected_time)
                
                log_action(fake_msg, "TRACKING_SUCCESS", f"Train: {current_train['num']} ({selected_time}), Seats found")
                return

            logger.debug(f"[{datetime.now().strftime('%H:%M')}] Нет мест для {chat_id} | Поезд: {selected_time}. Ждем...")
            time.sleep(CHECK_INTERVAL)
            
        except Exception as e:
            logger.error(f"Ошибка в потоке трекинга {chat_id}: {e}", exc_info=True)
            time.sleep(CHECK_INTERVAL)
    
    # === ГРАЦИОЗНОЕ ЗАВЕРШЕНИЕ ПОТОКА ===
    # Поток завершился (по любой причине: stop_flag, удаление из БД, успех)
    # Очищаем in-memory данные и подтверждаем остановку
    logger.info(f"🏁 Завершение потока трекинга для {chat_id}:{selected_time}")
    
    active_jobs.pop(chat_id, None)
    heartbeat_enabled.discard(chat_id)
    heartbeat_intervals.pop(chat_id, None)
    tracking_status.pop(chat_id, None)
    
    # Подтверждаем остановку в системе синхронизации
    confirm_tracking_stopped(chat_id, selected_time)
    
    log_action(fake_msg, "TRACKING_STOPPED", f"Train: {selected_time}, Graceful shutdown completed")

def send_detailed_train_info(chat_id, train, num_passengers=None, with_button=True, from_station=None, to_station=None, date=None):
    """
    Отправляет детальную информацию о поезде с опциональной кнопкой перехода на сайт.
    
    Args:
        chat_id: ID чата пользователя
        train: словарь с информацией о поезде
        num_passengers: количество пассажиров
        with_button: если True, добавляет кнопку с ссылкой на покупку билета
        from_station: станция отправления (для формирования ссылки)
        to_station: станция назначения (для формирования ссылки)
        date: дата поездки (для формирования ссылки)
    """
    lines = [
        f"🚂 <b>Поезд №{train['num']}</b>",
        f"⏱ Отправление: {train['time']}",
        f"⏳ В пути: {train['duration']}",
        f"👥 Нужно мест: {num_passengers}",
        "------------------",
        "<b>Доступные вагоны:</b>"
    ]

    for c in train['parsed_info']:
        seats_int = int(c['seats']) if c['seats'].isdigit() else 0
        is_enough = seats_int >= num_passengers if num_passengers else False
        
        status = "✅" if is_enough else "❌"
        icon = "🪑" if "Сидячий" in c['type'] else "🛏"
        
        lines.append(f"{status} {icon} <b>{c['type']}</b>: {c['seats']} мест ({c['price_byn']} BYN)")

    full_text = "\n".join(lines)
    
    # Формируем клавиатуру с кнопкой перехода на сайт
    keyboard = None
    if with_button:
        try:
            # Генерируем ссылку с параметрами для прямого перехода к поиску
            base_url = "https://pass.rw.by/ru/route/"
            
            # Если переданы параметры маршрута, добавляем их в URL
            if from_station and to_station and date:
                params = {
                    'from': from_station,
                    'to': to_station,
                    'date': date
                }
                # Кодируем параметры для URL (кириллица корректно обрабатывается)
                query_string = urlencode(params, encoding='utf-8')
                buy_url = f"{base_url}?{query_string}"
            else:
                buy_url = base_url
            
            keyboard = InlineKeyboardMarkup(row_width=1)
            buy_button = InlineKeyboardButton(
                text="🎫 Купить билет на сайте RW.BY",
                url=buy_url
            )
            keyboard.add(buy_button)
            
            logger.info(f"🔗 Добавлена кнопка покупки для поезда {train['num']} (chat_id={chat_id}): {buy_url}")
        except Exception as e:
            logger.warning(f"⚠️ Не удалось создать кнопку покупки: {e}")
    
    # Добавляем текстовую ссылку в сообщение для надежности (если клиент не показывает кнопки)
    link_text = f"\n\n🔗 <a href='{buy_url}'>Открыть страницу покупки в браузере</a>" if with_button and buy_url else ""
    
    bot.send_message(chat_id, full_text + link_text, parse_mode="HTML", reply_markup=keyboard)

# --- МАШИНА СОСТОЯНИЙ (ПОШАГОВЫЙ ВВОД) ---

@bot.message_handler(commands=['start'])
def send_welcome(message):
    chat_id = message.chat.id
    user_steps.pop(chat_id, None)
    user_data.pop(chat_id, None)
    
    # Сохраняем пользователя в базу данных
    username = message.from_user.username if hasattr(message.from_user, 'username') else None
    first_name = message.from_user.first_name if hasattr(message.from_user, 'first_name') else None
    last_name = message.from_user.last_name if hasattr(message.from_user, 'last_name') else None
    save_user(chat_id, username, first_name, last_name)
    
    # Создаем клавиатуру с популярными станциями и командами
    keyboard = ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=False)
    keyboard.add(KeyboardButton("🚂 Начать поиск"))
    keyboard.add(KeyboardButton("📊 Мои трекинги"), KeyboardButton("📜 История"))
    keyboard.add(KeyboardButton("⭐ Избранное"), KeyboardButton("📈 Моя статистика"))
    keyboard.add(KeyboardButton("❓ Помощь"))
    
    text = (
        f"👋 Привет, {first_name or 'путешественник'}! Я бот для отслеживания билетов БЖД.\n\n"
        "✨ <b>Мои возможности:</b>\n"
        "• 🔍 Поиск билетов по маршруту\n"
        "• 🔔 Уведомления при появлении мест\n"
        "• 📊 Отслеживание нескольких поездов одновременно\n"
        "• 💓 Heartbeat-сообщения о статусе проверки\n"
        "• 📜 История ваших поисков\n"
        "• ⭐ Избранные маршруты для быстрого доступа\n"
        "• 📈 Личная статистика использования\n\n"
        "<b>Команды:</b>\n"
        "/track - Начать поиск билетов\n"
        "/mytracks - Показать все активные трекинги\n"
        "/status - Статус текущего трекинга\n"
        "/stop - Остановить трекинг\n"
        "/history - История поисков\n"
        "/favorites - ⭐ Избранные маршруты\n"
        "/stats - 📈 Личная статистика\n"
        "/help - Подробная справка\n\n"
        "Нажми кнопку ниже или отправь /track чтобы начать!"
    )
    bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=keyboard)
    log_action(message, "START", f"User started bot. Name: {first_name}")

@bot.message_handler(commands=['admin'])
def admin_menu(message):
    """Админ-меню для пользователей с ролью admin/moderator"""
    chat_id = message.chat.id
    
    if not is_admin(chat_id):
        bot.send_message(chat_id, "❌ У вас нет прав администратора.")
        log_user_action(chat_id, "ADMIN_ACCESS_DENIED", f"Non-admin user tried to access admin menu")
        return
    
    keyboard = ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    keyboard.add(KeyboardButton("📊 Админ-статистика"))
    keyboard.add(KeyboardButton("📜 Просмотр логов"))
    keyboard.add(KeyboardButton("👥 Пользователи"))
    keyboard.add(KeyboardButton("🔙 Назад"))
    
    text = (
        "🔐 <b>Панель администратора</b>\n\n"
        "Доступные функции:\n"
        "• 📊 Просмотр общей статистики\n"
        "• 📜 Чтение логов действий пользователей\n"
        "• 👥 Управление пользователями\n\n"
        "Выберите действие:"
    )
    bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=keyboard)
    log_user_action(chat_id, "ADMIN_MENU", "Admin menu accessed")

@bot.message_handler(func=lambda message: message.text == "📜 Просмотр логов")
def view_logs_menu(message):
    """Просмотр логов для администраторов"""
    chat_id = message.chat.id
    
    if not is_admin(chat_id):
        bot.send_message(chat_id, "❌ У вас нет прав администратора.")
        return
    
    keyboard = ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    keyboard.add(KeyboardButton("📊 Админ-статистика"))
    keyboard.add(KeyboardButton("👥 Пользователи"))
    keyboard.add(KeyboardButton("🔙 Назад"))
    
    with get_db_cursor() as cursor:
        cursor.execute("""
            SELECT u.chat_id, u.username, ul.action, ul.details, ul.created_at
            FROM user_logs ul
            JOIN users u ON ul.chat_id = u.chat_id
            ORDER BY ul.created_at DESC
            LIMIT 50
        """)
        logs = cursor.fetchall()
    
    if not logs:
        bot.send_message(chat_id, "📭 Логи пока пусты.", reply_markup=keyboard)
        return
    
    log_text = "📜 <b>Последние действия пользователей (50):</b>\n\n"
    for log in logs:
        username = f"@{log['username']}" if log['username'] else f"ID:{log['chat_id']}"
        time_str = log['created_at'].split(' ')[1][:5] if log['created_at'] else '?'
        log_text += f"⏰ {time_str} | {username}\n"
        log_text += f"   ├─ Действие: {log['action']}\n"
        if log['details']:
            details = log['details'][:100] + "..." if len(log['details']) > 100 else log['details']
            log_text += f"   └─ Детали: {details}\n"
        log_text += "\n"
    
    # Отправляем частями, так как Telegram имеет лимит на длину сообщения
    for i in range(0, len(log_text), 4000):
        bot.send_message(chat_id, log_text[i:i+4000], parse_mode="HTML", reply_markup=keyboard)
    
    log_user_action(chat_id, "VIEW_LOGS", f"Admin viewed {len(logs)} log entries")

@bot.message_handler(func=lambda message: message.text == "📊 Админ-статистика")
def admin_statistics(message):
    """Показывает общую статистику для администраторов"""
    chat_id = message.chat.id
    
    if not is_admin(chat_id):
        bot.send_message(chat_id, "❌ У вас нет прав администратора.")
        return
    
    keyboard = ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    keyboard.add(KeyboardButton("📜 Просмотр логов"))
    keyboard.add(KeyboardButton("👥 Пользователи"))
    keyboard.add(KeyboardButton("🔙 Назад"))
    
    with get_db_cursor() as cursor:
        # Общее количество пользователей
        cursor.execute("SELECT COUNT(*) as count FROM users")
        total_users = cursor.fetchone()['count']
        
        # Активные трекинги
        cursor.execute("SELECT COUNT(*) as count FROM active_trackings")
        active_trackings = cursor.fetchone()['count']
        
        # Всего поисков за сегодня
        cursor.execute("""
            SELECT COUNT(*) as count FROM search_history 
            WHERE date(searched_at) = date('now')
        """)
        today_searches = cursor.fetchone()['count']
        
        # Топ-5 пользователей по активности
        cursor.execute("""
            SELECT u.chat_id, u.username, COUNT(sh.id) as search_count
            FROM users u
            LEFT JOIN search_history sh ON u.chat_id = sh.chat_id
            GROUP BY u.chat_id
            ORDER BY search_count DESC
            LIMIT 5
        """)
        top_users = cursor.fetchall()
    
    stats_text = (
        "📊 <b>Админ-статистика</b>\n\n"
        f"👥 Всего пользователей: {total_users}\n"
        f"🚂 Активных трекингов: {active_trackings}\n"
        f"🔍 Поисков сегодня: {today_searches}\n\n"
        "<b>Топ-5 активных пользователей:</b>\n"
    )
    
    for i, user in enumerate(top_users, 1):
        username = f"@{user['username']}" if user['username'] else f"ID:{user['chat_id']}"
        stats_text += f"{i}. {username} — {user['search_count']} поисков\n"
    
    bot.send_message(chat_id, stats_text, parse_mode="HTML", reply_markup=keyboard)
    log_user_action(chat_id, "ADMIN_STATS", "Viewed admin statistics")

@bot.message_handler(func=lambda message: message.text == "👥 Пользователи")
def admin_users_menu(message):
    """Меню управления пользователями для администраторов"""
    chat_id = message.chat.id
    
    if not is_admin(chat_id):
        bot.send_message(chat_id, "❌ У вас нет прав администратора.")
        return
    
    keyboard = ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    keyboard.add(KeyboardButton("📊 Админ-статистика"))
    keyboard.add(KeyboardButton("📜 Просмотр логов"))
    keyboard.add(KeyboardButton("🔙 Назад"))
    
    with get_db_cursor() as cursor:
        cursor.execute("""
            SELECT chat_id, username, first_name, role, created_at, last_active
            FROM users
            ORDER BY last_active DESC
            LIMIT 20
        """)
        users = cursor.fetchall()
    
    users_text = "👥 <b>Последние пользователи (20):</b>\n\n"
    for user in users:
        username = f"@{user['username']}" if user['username'] else "NoUsername"
        name = user['first_name'] or "Unknown"
        role_emoji = "🔐" if user['role'] == 'admin' else "👤"
        last_active = user['last_active'].split(' ')[1][:5] if user['last_active'] else '?'
        users_text += f"{role_emoji} {name} ({username})\n"
        users_text += f"   Роль: {user['role']} | Активен: {last_active}\n\n"
    
    for i in range(0, len(users_text), 4000):
        bot.send_message(chat_id, users_text[i:i+4000], parse_mode="HTML", reply_markup=keyboard)
    
    log_user_action(chat_id, "VIEW_USERS", "Viewed users list")

@bot.message_handler(commands=['help'])
def show_help(message):
    """Показывает подробную справку по использованию бота"""
    chat_id = message.chat.id
    
    help_text = (
        "📘 <b>Полное руководство по использованию бота</b>\n\n"
        
        "🚀 <b>Быстрый старт:</b>\n"
        "1. Нажмите кнопку '🚂 Начать поиск' или отправьте /track\n"
        "2. Введите станцию отправления (например: Минск)\n"
        "3. Введите станцию назначения (например: Москва)\n"
        "4. Укажите дату поездки в формате ГГГГ-ММ-ДД\n"
        "5. Введите количество пассажиров\n"
        "6. Выберите поезд из списка\n"
        "7. Запустите мониторинг\n\n"
        
        "📊 <b>Отслеживание нескольких поездов:</b>\n"
        "Бот поддерживает одновременное отслеживание нескольких поездов!\n"
        "Просто запустите новый поиск через /track пока идет мониторинг другого поезда.\n\n"
        
        "💓 <b>Heartbeat-сообщения:</b>\n"
        "При запуске мониторинга вы можете выбрать интервал heartbeat:\n"
        "• 10 мин, 20 мин, 30 мин, 1 час\n"
        "• Или отключить heartbeat\n"
        "Heartbeat напоминает, что бот продолжает работу.\n\n"
        
        "📜 <b>История поисков:</b>\n"
        "Бот автоматически сохраняет историю ваших поисков.\n"
        "Используйте /history чтобы быстро повторить предыдущий запрос.\n\n"
        
        "🛡 <b>Безопасность:</b>\n"
        "• Все данные хранятся локально в защищенной базе данных\n"
        "• Rate limiting защищает от случайных перегрузок\n"
        "• Входные данные проходят санитизацию\n\n"
        
        "⚠️ <b>Важно:</b>\n"
        "• Не устанавливайте слишком маленький интервал проверки (рекомендуется 60 сек)\n"
        "• При перезапуске бота все активные трекинги восстанавливаются\n"
        "• Для остановки трекинга используйте /stop или /mytracks\n\n"
        
        "🆘 <b>Проблемы?</b>\n"
        "Проверьте логи или убедитесь, что названия станций указаны правильно."
    )
    
    keyboard = InlineKeyboardMarkup()
    keyboard.add(InlineKeyboardButton("🚂 Начать поиск", callback_data="quick_start"))
    
    bot.send_message(chat_id, help_text, parse_mode="HTML", reply_markup=keyboard)
    log_action(message, "HELP_REQUESTED", "User requested help documentation")

@bot.message_handler(commands=['mytracks'])
def show_my_trackings(message):
    """Показывает все активные трекинги пользователя с удобным меню удаления"""
    chat_id = message.chat.id
    
    trackings = get_user_trackings(chat_id)
    
    if not trackings:
        bot.reply_to(message, "ℹ️ У вас нет активных трекингов.\nИспользуйте /track для создания нового.")
        return
    
    text = f"📊 <b>Ваши активные трекинги ({len(trackings)}):</b>\n\n"
    
    # Формируем кнопки для каждого трекинга
    keyboard = InlineKeyboardMarkup(row_width=1)
    
    for i, tracking in enumerate(trackings, 1):
        status_emoji = "✅" if tracking['seats_available'] > 0 else "🔄"
        created_at = tracking['created_at'][:16].replace('T', ' ') if tracking['created_at'] else "неизвестно"
        
        # Формируем понятное описание трекинга
        text += f"<b>№{i}</b> {status_emoji} <b>{tracking['from_station']} → {tracking['to_station']}</b>\n"
        text += f"   📅 Дата поездки: {tracking['date']}\n"
        text += f"   🚂 Поезд: {tracking['train_time']} (№{tracking['train_num'] or 'ожидание'})\n"
        text += f"   👥 Мест: {tracking['passengers']} | Доступно: {tracking['seats_available']}\n"
        text += f"   💓 Heartbeat: {'вкл' if tracking['heartbeat_enabled'] else 'выкл'}\n"
        text += f"   🔁 Запросов: {tracking['requests_count']}\n"
        text += f"   ⏰ Создан: {created_at}\n\n"
        
        # Кнопка удаления с номером трекинга для удобства
        btn_text = f"❌ Удалить трек №{i} ({tracking['from_station']} → {tracking['to_station']})"
        callback_data = f"delete_tracking_{tracking['id']}"
        # Обрезаем callback_data до 64 символов (лимит Telegram)
        if len(callback_data) > 64:
            callback_data = callback_data[:64]
        keyboard.add(InlineKeyboardButton(btn_text[:50], callback_data=callback_data))
    
    keyboard.add(InlineKeyboardButton("🗑 Удалить все трекинги", callback_data="stop_all_trackings"))
    keyboard.add(InlineKeyboardButton("🔙 Назад к списку", callback_data="back_to_list"))
    
    bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=keyboard)
    log_action(message, "MYTRACKS_REQUESTED", f"User requested tracking list. Count: {len(trackings)}")

@bot.message_handler(commands=['history'])
def show_history(message):
    """Показывает историю поисков пользователя"""
    chat_id = message.chat.id
    
    history = get_user_search_history(chat_id, limit=5)
    
    if not history:
        bot.reply_to(message, "📜 У вас пока нет истории поисков.\nСовершите первый поиск через /track")
        return
    
    text = "📜 <b>Ваши последние поиски:</b>\n\n"
    
    keyboard = InlineKeyboardMarkup(row_width=1)
    
    for i, search in enumerate(history, 1):
        date_str = search['searched_at'][:16].replace('T', ' ')
        text += f"{i}. 🚂 <b>{search['from_station']} → {search['to_station']}</b>\n"
        text += f"   📅 {search['date']} | 👥 {search['passengers']} чел.\n"
        text += f"   ⏰ {date_str}\n\n"
        
        # Кнопка для быстрого повтора поиска
        btn_text = f"🔁 Повторить: {search['from_station']} → {search['to_station']}"
        callback_data = f"repeat_search_{search['from_station']}_{search['to_station']}_{search['date']}_{search['passengers']}"
        # Обрезаем callback_data до 64 символов (лимит Telegram)
        if len(callback_data) > 64:
            callback_data = callback_data[:64]
        keyboard.add(InlineKeyboardButton(btn_text[:50], callback_data=callback_data))
    
    bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=keyboard)
    log_action(message, "HISTORY_REQUESTED", "User requested search history")

@bot.message_handler(commands=['track'])
def start_track(message):
    chat_id = message.chat.id
    user_steps[chat_id] = 'ask_from'
    user_data[chat_id] = {}
    
    # Показываем популярные станции для быстрого выбора
    popular = get_popular_stations(limit=5)
    popular_names = [s['station_name'] for s in popular] if popular else DEFAULT_STATIONS[:5]
    
    keyboard = ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    row = []
    for station in popular_names:
        row.append(KeyboardButton(station))
        if len(row) == 2:
            keyboard.add(*row)
            row = []
    if row:
        keyboard.add(*row)
    keyboard.add(KeyboardButton("🔙 Назад"))
    
    bot.send_message(
        chat_id, 
        "1️⃣ <b>Откуда едем?</b>\n"
        "Напишите название станции отправления или выберите из популярных:\n\n"
        f"💡 <i>Популярные: {', '.join(popular_names)}</i>",
        parse_mode="HTML",
        reply_markup=keyboard
    )
    logger.info(f"Начат трек для {chat_id}. Шаг: ask_from")

@bot.message_handler(commands=['stop'])
def stop_tracking_cmd(message):
    chat_id = message.chat.id
    
    # Получаем активные трекинги из базы
    trackings = get_user_trackings(chat_id)
    
    if not trackings and chat_id not in active_jobs:
        bot.reply_to(message, "ℹ️ У вас нет активных задач.\nИспользуйте /mytracks для просмотра всех трекингов.")
        return
    
    # Если есть трекинги в базе, показываем список для выбора
    if trackings:
        keyboard = InlineKeyboardMarkup(row_width=1)
        
        for i, tracking in enumerate(trackings, 1):
            btn_text = f"❌ {tracking['from_station']} → {tracking['to_station']} ({tracking['train_time']})"
            callback_data = f"stop_tracking_{tracking['train_time']}"
            keyboard.add(InlineKeyboardButton(btn_text[:50], callback_data=callback_data))
        
        keyboard.add(InlineKeyboardButton("⛔ Остановить ВСЕ", callback_data="stop_all_trackings"))
        
        bot.send_message(
            chat_id,
            f"⏹ <b>Выберите трекинг для остановки ({len(trackings)} активных):</b>",
            parse_mode="HTML",
            reply_markup=keyboard
        )
        log_action(message, "STOP_REQUESTED", f"User requested to stop tracking. Count: {len(trackings)}")
    else:
        # Только in-memory трекинг (старый формат)
        active_jobs.pop(chat_id)
        tracking_status.pop(chat_id, None)
        bot.reply_to(message, "⏹ Отслеживание остановлено.")
        user_steps.pop(chat_id, None)

@bot.callback_query_handler(func=lambda call: call.data.startswith("stop_tracking_") or call.data == "stop_all_trackings" or call.data.startswith("delete_tracking_"))
def on_stop_tracking_choice(call):
    """Обработчик выбора трекинга для остановки/удаления"""
    chat_id = call.message.chat.id
    
    if call.data == "stop_all_trackings":
        # Останавливаем все трекинги пользователя
        remove_tracking_from_db(chat_id)
        
        # Очищаем in-memory данные
        if chat_id in active_jobs:
            active_jobs.pop(chat_id)
        tracking_status.pop(chat_id, None)
        heartbeat_enabled.discard(chat_id)
        heartbeat_intervals.pop(chat_id, None)
        user_steps.pop(chat_id, None)
        bot.answer_callback_query(call.id, "✅ Все трекинги остановлены")
        bot.send_message(chat_id, "⏹ Все трекинги остановлены.")
        return
    
    # Обработка удаления трекинга через /mytracks (используем ID)
    if call.data.startswith("delete_tracking_"):
        tracking_id = int(call.data.replace("delete_tracking_", ""))
        
        # Сначала получаем информацию о трекинге ДО удаления
        trackings_before = get_user_trackings(chat_id)
        deleted_tracking_info = None
        for t in trackings_before:
            if t['id'] == tracking_id:
                deleted_tracking_info = t
                break
        
        if not deleted_tracking_info:
            bot.answer_callback_query(call.id, "❌ Трекинг не найден", show_alert=True)
            bot.send_message(chat_id, "❌ Трекинг не найден или уже был удален.")
            return
        
        deleted_train_time = deleted_tracking_info['train_time']
        
        # Удаляем только по ID - это гарантирует удаление конкретного трекинга
        remove_tracking_from_db(chat_id, tracking_id=tracking_id)
        
        # Очищаем in-memory данные если этот трекинг активен
        if chat_id in active_jobs:
            job = active_jobs[chat_id]
            # Сравниваем по ID - это точное совпадение
            if job.get('id') == tracking_id:
                job['stop_flag'] = True
                active_jobs.pop(chat_id)
        
        if chat_id in tracking_status:
            status = tracking_status[chat_id]
            # Сравниваем по ID - это точное совпадение
            if status.get('id') == tracking_id:
                tracking_status.pop(chat_id, None)
                heartbeat_enabled.discard(chat_id)
                heartbeat_intervals.pop(chat_id, None)
        
        bot.answer_callback_query(call.id, "✅ Трекинг удален")
        bot.send_message(chat_id, f"✅ Трекинг №{deleted_tracking_info['id']} ({deleted_tracking_info['from_station']} → {deleted_tracking_info['to_station']}, {deleted_train_time}) удален.")
        return
    
    # Останавливаем конкретный трекинг (старый функционал через /stop)
    train_time = call.data.replace("stop_tracking_", "")
    remove_tracking_from_db(chat_id, train_time)
    
    # Очищаем in-memory данные
    if chat_id in active_jobs:
        job = active_jobs[chat_id]
        if job.get('train_time') == train_time:
            job['stop_flag'] = True
            active_jobs.pop(chat_id)
    
    tracking_status.pop(chat_id, None)
    heartbeat_enabled.discard(chat_id)
    heartbeat_intervals.pop(chat_id, None)
    user_steps.pop(chat_id, None)
    
    bot.answer_callback_query(call.id, "✅ Трекинг остановлен")
    bot.send_message(chat_id, f"⏹ Трекинг на {train_time} остановлен.")


# Обработчик календаря для выбора даты
@bot.callback_query_handler(func=lambda call: isinstance(call.data, str) and call.data.startswith("calendar:"))
def on_calendar_selection(call):
    """Обработчик выбора даты из календаря"""
    chat_id = call.message.chat.id
    
    # Проверяем, что пользователь находится на шаге выбора даты
    current_step = user_steps.get(chat_id)
    if current_step != 'ask_date':
        bot.answer_callback_query(call.id, "⚠️ Сейчас не требуется выбор даты", show_alert=True)
        return
    
    try:
        # Формат callback_data от telebot-calendar: calendar:ACTION:YEAR:MONTH:DAY
        # ACTION может быть: MONTHS (навигация по месяцам), DAYS (выбор дня), IGNORE
        parts = call.data.split(":")
        if len(parts) < 4:
            bot.answer_callback_query(call.id)
            return
        
        action = parts[1]  # MONTHS, DAYS, IGNORE
        year = int(parts[2])
        month = int(parts[3])
        day = int(parts[4]) if len(parts) > 4 and parts[4] != '!' else 1
        
        # Используем встроенный обработчик календаря
        result = calendar.calendar_query_handler(bot, call, "date_select", action, year, month, day)
        
        if result:
            # Дата выбрана
            selected_date = result
            date_str = selected_date.strftime("%Y-%m-%d")
            
            # Проверяем, что дата не в прошлом
            from datetime import date
            if selected_date.date() < date.today():
                bot.answer_callback_query(call.id, "❌ Нельзя выбрать прошедшую дату", show_alert=True)
                return
            
            # Сохраняем дату и переходим к следующему шагу
            user_data[chat_id]['date'] = date_str
            user_steps[chat_id] = 'ask_passengers'
            
            bot.answer_callback_query(call.id, f"✅ Дата выбрана: {date_str}")
            bot.send_message(
                chat_id,
                f"4️⃣ <b>Сколько пассажиров?</b>\nДата: <i>{date_str}</i>\nВведите число (1, 2, 3...):",
                parse_mode="HTML"
            )
        else:
            # Календарь обновлен (навигация), ждем дальнейшего выбора
            bot.answer_callback_query(call.id)
        
    except ValueError as e:
        logger.warning(f"Ошибка разбора даты из календаря: {e}, data={call.data}")
        bot.answer_callback_query(call.id)
    except Exception as e:
        logger.error(f"Ошибка обработки календаря: {e}")
        bot.answer_callback_query(call.id, "❌ Ошибка при выборе даты. Попробуйте еще раз.", show_alert=True)

@bot.message_handler(commands=['status'])
def show_tracking_status(message):
    """Показывает статус отслеживания для текущего пользователя"""
    chat_id = message.chat.id
    
    # Сначала проверяем базу данных
    trackings = get_user_trackings(chat_id)
    
    if not trackings and chat_id not in tracking_status:
        bot.reply_to(message, "ℹ️ У вас нет активного отслеживания.\nИспользуйте /track для создания запроса.")
        return
    
    if trackings:
        # Показываем первый активный трекинг или список
        if len(trackings) == 1:
            tracking = trackings[0]
            msg = f"📊 <b>Статус отслеживания</b>\n\n"
            msg += f"🚂 Поезд №: {tracking['train_num'] or 'Ожидание...'}\n"
            msg += f"⏰ Время отправления: {tracking['train_time']}\n"
            msg += f"🪑 Доступно мест: {tracking['seats_available']}\n"
            msg += f"🔄 Запросов выполнено: {tracking['requests_count']}\n"
            msg += f"👥 Нужно мест: {tracking['passengers']}\n"
            msg += f"📍 Маршрут: {tracking['from_station']} → {tracking['to_station']}\n"
            msg += f"📅 Дата: {tracking['date']}\n"
            msg += f"💓 Heartbeat: {'включен' if tracking['heartbeat_enabled'] else 'выключен'}\n"
            
            keyboard = InlineKeyboardMarkup()
            keyboard.add(InlineKeyboardButton("⏹ Остановить этот трекинг", callback_data=f"stop_tracking_{tracking['train_time']}"))
            
            bot.send_message(chat_id, msg, parse_mode="HTML", reply_markup=keyboard)
        else:
            # Несколько трекингов - показываем краткий список
            bot.reply_to(message, f"У вас {len(trackings)} активных трекингов.\nИспользуйте /mytracks для просмотра полного списка.")
    else:
        # In-memory статус (старый формат)
        status = tracking_status[chat_id]
        info = user_data.get(chat_id, {})
        
        msg = f"📊 <b>Статус отслеживания</b>\n\n"
        msg += f"🚂 Поезд №: {status['train_num'] or 'Ожидание...'}\n"
        msg += f"⏰ Время: {status['train_time']}\n"
        msg += f"🪑 Доступно мест: {status['seats_available']}\n"
        msg += f"🔄 Запросов выполнено: {status['requests_count']}\n"
        msg += f"👥 Нужно мест: {info.get('passengers', 'N/A')}\n"
        msg += f"📍 Маршрут: {info.get('from', 'N/A')} → {info.get('to', 'N/A')}\n"
        
        bot.send_message(chat_id, msg, parse_mode="HTML")

@bot.message_handler(commands=['favorites'])
def show_favorites_command(message):
    """Показывает избранные маршруты пользователя"""
    chat_id = message.chat.id
    show_favorites_impl(message, chat_id)

@bot.message_handler(commands=['stats'])
def show_stats_command(message):
    """Показывает статистику пользователя"""
    chat_id = message.chat.id
    show_user_stats_impl(message, chat_id)

@bot.message_handler(func=lambda message: message.chat.id in user_steps)
def handle_step_input(message):
    chat_id = message.chat.id
    current_step = user_steps[chat_id]
    
    # Проверка rate limiting
    if not check_rate_limit(chat_id):
        bot.send_message(chat_id, "⚠️ Слишком много запросов. Пожалуйста, подождите немного.")
        logger.warning(f"⚠️ Rate limit для пользователя {chat_id} на шаге {current_step}")
        return
    
    # Очистка ввода от опасных символов
    text = sanitize_input(message.text.strip())
    
    # Логирование ввода пользователя
    logger.info(f"📝 Пользователь {chat_id} вводит на шаге {current_step}: '{text}'")

    if current_step == 'ask_from':
        user_data[chat_id]['from'] = text
        user_steps[chat_id] = 'ask_to'
        bot.send_message(
            chat_id,
            f"2️⃣ <b>Куда едем?</b>\nВы указали: <i>{text}</i>\nТеперь напишите станцию назначения:",
            parse_mode="HTML"
        )

    elif current_step == 'ask_to':
        user_data[chat_id]['to'] = text
        user_steps[chat_id] = 'ask_date'
        # Создаем календарь с завтрашней датой по умолчанию
        from datetime import datetime, timedelta
        try:
            default_date = datetime.now() + timedelta(days=1)
            calendar_keyboard = calendar.create_calendar(year=default_date.year, month=default_date.month)
        except Exception as e:
            logger.warning(f"Ошибка создания календаря: {e}")
            calendar_keyboard = calendar.create_calendar()
        
        bot.send_message(
            chat_id,
            f"3️⃣ <b>Дата поездки?</b>\nМаршрут: <i>{user_data[chat_id]['from']} → {text}</i>\nВыберите дату в календаре:",
            parse_mode="HTML",
            reply_markup=calendar_keyboard
        )

    elif current_step == 'ask_date':
        if not re.match(r"\d{4}-\d{2}-\d{2}", text):
            bot.send_message(chat_id, "❌ Неверный формат даты. Используйте ГГГГ-ММ-ДД (например: 2026-05-20).\nПопробуйте еще раз:")
            return
        
        user_data[chat_id]['date'] = text
        user_steps[chat_id] = 'ask_passengers'
        bot.send_message(
            chat_id,
            f"4️⃣ <b>Сколько пассажиров?</b>\nДата: <i>{text}</i>\nВведите число (1, 2, 3...):",
            parse_mode="HTML"
        )

    elif current_step == 'ask_passengers':
        if not text.isdigit() or int(text) <= 0:
            bot.send_message(chat_id, "❌ Введите корректное число пассажиров (больше 0):")
            return
        
        num_pax = int(text)
        user_data[chat_id]['passengers'] = num_pax
        user_steps.pop(chat_id, None)

        loading_msg = bot.send_message(chat_id, f"🔍 Ищу поезда по маршруту {user_data[chat_id]['from']} → {user_data[chat_id]['to']} на {user_data[chat_id]['date']}...")
        
        # Логирование поиска с номером пользователя и параметрами
        log_action(message, "SEARCH_STARTED", f"From: {user_data[chat_id]['from']}, To: {user_data[chat_id]['to']}, Date: {user_data[chat_id]['date']}, Passengers: {num_pax}")
        trains = get_trains_list(user_data[chat_id]['from'], user_data[chat_id]['to'], user_data[chat_id]['date'], chat_id)
        
        bot.delete_message(chat_id, loading_msg.message_id)

        if not trains:
            bot.send_message(chat_id, "❌ К сожалению, поездов не найдено. Проверьте названия станций или дату.\nПопробуйте /track заново.")
            return

        show_train_list(chat_id, trains)

def show_train_list(chat_id, trains):
    keyboard = InlineKeyboardMarkup(row_width=1)
    pax = user_data[chat_id]['passengers']

    for train in trains:
        has_enough = any(
            c['seats'].isdigit() and int(c['seats']) >= pax
            for c in train['parsed_info']
        )
        status_emoji = "✅" if has_enough else "❌"
        
        summary_parts = []
        for c in train['parsed_info'][:2]:
            summary_parts.append(f"{c['type']}: {c['seats']}")
        if len(train['parsed_info']) > 2:
            summary_parts.append("...")
        
        summary = ", ".join(summary_parts) if summary_parts else "Нет данных"
        
        btn_text = f"{status_emoji} {train['time']} | №{train['num']} | {summary}"
        if len(btn_text) > 60:
            btn_text = btn_text[:57] + "..."

        keyboard.add(InlineKeyboardButton(
            text=btn_text,
            callback_data=f"preview_{train['time']}_{train['num']}"
        ))

    bot.send_message(chat_id, f"📋 Найдено поездов: {len(trains)}. Выберите нужный для деталей:", reply_markup=keyboard)

# --- ОБРАБОТКА НАЖАТИЙ (CALLBACKS) ---

@bot.callback_query_handler(func=lambda call: call.data.startswith("preview_"))
def on_preview(call):
    chat_id = call.message.chat.id
    data = call.data.split("_", 2)
    if len(data) < 3:
        return
    
    sel_time, sel_num = data[1], data[2]
    info = user_data.get(chat_id)
    
    if not info:
        bot.answer_callback_query(call.id, "Сессия истекла. Начните /track", show_alert=True)
        return

    trains = get_trains_list(info['from'], info['to'], info['date'], chat_id)
    train = next((t for t in trains if t['time'] == sel_time and t['num'] == sel_num), None)

    if not train:
        bot.answer_callback_query(call.id, "Поезд не найден (обновите список)", show_alert=True)
        return

    # Логирование просмотра деталей поезда пользователем
    logger.info(f"👁 Пользователь {chat_id} просматривает детали поезда №{sel_num} ({sel_time}) | Маршрут: {info['from']} → {info['to']}")
    
    send_detailed_train_info(
        chat_id, 
        train, 
        info['passengers'],
        with_button=True,
        from_station=info['from'],
        to_station=info['to'],
        date=info['date']
    )

    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("▶️ Запустить мониторинг", callback_data=f"confirm_{sel_time}_{sel_num}"))
    kb.add(InlineKeyboardButton("🔙 Назад к списку", callback_data="back_to_list"))
    
    bot.send_message(chat_id, f"Запустить непрерывную проверку для поезда №{sel_num} ({sel_time})?", reply_markup=kb)
    
    # Если уже есть активный трекинг, добавляем кнопку просмотра статуса
    if chat_id in tracking_status:
        status_kb = InlineKeyboardMarkup()
        status_kb.add(InlineKeyboardButton("📊 Просмотреть статус", callback_data="view_status"))
        bot.send_message(
            chat_id, 
            "ℹ️ У вас уже есть активное отслеживание. Используйте /status или кнопку ниже для просмотра статуса.",
            reply_markup=status_kb
        )

@bot.callback_query_handler(func=lambda call: call.data.startswith("confirm_"))
def on_confirm(call):
    chat_id = call.message.chat.id
    data = call.data.split("_", 2)
    sel_time, sel_num = data[1], data[2]
    
    # Проверяем количество активных трекингов (ограничение для безопасности)
    user_trackings = get_user_trackings(chat_id)
    if len(user_trackings) >= 5:
        bot.answer_callback_query(call.id, "⚠️ Максимум 5 активных трекингов! Остановите один из них.", show_alert=True)
        return

    info = user_data.get(chat_id)
    if not info:
        bot.answer_callback_query(call.id, "Ошибка сессии", show_alert=True)
        return

    # Логирование запуска мониторинга пользователем
    logger.info(f"▶️ Пользователь {chat_id} запустил мониторинг поезда №{sel_num} ({sel_time}) | Маршрут: {info['from']} → {info['to']}")

    # Предлагаем выбрать интервал heartbeat
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("10 мин", callback_data=f"hb_interval_600_{sel_time}_{sel_num}"),
        InlineKeyboardButton("20 мин", callback_data=f"hb_interval_1200_{sel_time}_{sel_num}")
    )
    kb.add(
        InlineKeyboardButton("30 мин", callback_data=f"hb_interval_1800_{sel_time}_{sel_num}"),
        InlineKeyboardButton("1 час", callback_data=f"hb_interval_3600_{sel_time}_{sel_num}")
    )
    kb.add(InlineKeyboardButton("❌ Без heartbeat", callback_data=f"heartbeat_no_{sel_time}_{sel_num}"))
    
    bot.send_message(chat_id, "💓 Выберите интервал сообщений 'Бот работает':", reply_markup=kb)

@bot.callback_query_handler(func=lambda call: call.data.startswith("hb_interval_") or call.data.startswith("heartbeat_no_"))
def on_heartbeat_choice(call):
    chat_id = call.message.chat.id
    
    # Правильный парсинг callback_data
    # Формат кнопок: hb_interval_<seconds>_<sel_time>_<sel_num>
    # При split('_') получаем: ['hb', 'interval', '<seconds>', '<sel_time>', '<sel_num>']
    if call.data.startswith("hb_interval_"):
        parts = call.data.split("_")
        if len(parts) < 5:
            logger.error(f"Ошибка формата callback данных у пользователя {chat_id}: {call.data}")
            bot.answer_callback_query(call.id, "Ошибка формата данных", show_alert=True)
            return
        choice_type = "hb_interval"
        try:
            interval_seconds = int(parts[2])
        except ValueError:
            logger.error(f"Неверный формат интервала в callback у пользователя {chat_id}: {call.data}")
            bot.answer_callback_query(call.id, "Ошибка интервала", show_alert=True)
            return
        sel_time = parts[3]
        sel_num = parts[4]
        
    elif call.data.startswith("heartbeat_no_"):
        # Формат: heartbeat_no_<sel_time>_<sel_num>
        # При split('_'): ['heartbeat', 'no', '<sel_time>', '<sel_num>']
        parts = call.data.split("_")
        if len(parts) < 4:
            logger.error(f"Ошибка формата callback данных у пользователя {chat_id}: {call.data}")
            bot.answer_callback_query(call.id, "Ошибка формата данных", show_alert=True)
            return
        choice_type = "heartbeat_no"
        interval_seconds = None
        sel_time = parts[2]
        sel_num = parts[3]
    else:
        logger.warning(f"Неизвестная команда callback от пользователя {chat_id}: {call.data}")
        bot.answer_callback_query(call.id, "Неизвестная команда", show_alert=True)
        return
    
    if choice_type == "hb_interval":
        
        info = user_data.get(chat_id)
        if not info:
            bot.answer_callback_query(call.id, "Ошибка сессии", show_alert=True)
            return
        
        # Сохраняем интервал и включаем heartbeat
        heartbeat_intervals[chat_id] = interval_seconds
        heartbeat_enabled.add(chat_id)
        
        minutes = interval_seconds // 60
        if minutes >= 60:
            hours = minutes // 60
            msg_text = f"✅ Heartbeat включен! Сообщения каждые {hours} ч."
        else:
            msg_text = f"✅ Heartbeat включен! Сообщения каждые {minutes} мин."
        
        bot.answer_callback_query(call.id, msg_text, show_alert=False)
        bot.send_message(chat_id, f"💓 {msg_text} Мониторинг запущен.")
        
    else:  # heartbeat_no
        info = user_data.get(chat_id)
        if not info:
            bot.answer_callback_query(call.id, "Ошибка сессии", show_alert=True)
            return
        
        # Отключаем heartbeat
        heartbeat_enabled.discard(chat_id)
        heartbeat_intervals.pop(chat_id, None)
        bot.answer_callback_query(call.id, "✅ Мониторинг запущен без heartbeat.", show_alert=False)
    
    # Запуск трекинга (для обоих случаев)
    if choice_type == "hb_interval" or choice_type == "heartbeat_no":
        info = user_data.get(chat_id)
        if not info:
            bot.answer_callback_query(call.id, "Ошибка сессии", show_alert=True)
            return
        
        # Сохраняем трекинг в базу данных
        save_tracking_to_db(
            chat_id, 
            info['from'], 
            info['to'], 
            info['date'], 
            info['passengers'], 
            sel_time,
            chat_id in heartbeat_enabled,
            heartbeat_intervals.get(chat_id, 1800)
        )
        
        # Сохраняем поиск в историю
        save_search_history(chat_id, info['from'], info['to'], info['date'], info['passengers'])
        
        thread = threading.Thread(
            target=tracking_worker,
            args=(chat_id, info['from'], info['to'], info['date'], sel_time),
            daemon=True
        )
        thread.start()
        
        # Получаем ID только что созданного трекинга из БД
        with get_db_cursor() as cursor:
            cursor.execute("""
                SELECT id FROM active_trackings 
                WHERE chat_id = ? AND train_time = ?
                ORDER BY created_at DESC LIMIT 1
            """, (chat_id, sel_time))
            row = cursor.fetchone()
            tracking_id = row['id'] if row else None
        
        # Сохраняем ID в active_jobs для корректного удаления
        active_jobs[chat_id] = {
            'thread': thread, 
            'stop_flag': False,
            'id': tracking_id,
            'train_time': sel_time
        }
        
        hb_status = "включен" if chat_id in heartbeat_enabled else "выключен"
        logger.info(f"✅ Мониторинг активирован: Пользователь {chat_id} -> Поезд №{sel_num} ({sel_time}) | heartbeat={hb_status}")
        
        # Отправляем сообщение об успешном создании задачи отслеживания
        bot.send_message(
            chat_id, 
            f"✅ <b>Задача отслеживания успешно создана!</b>\n\n"
            f"🚂 Поезд №{sel_num} ({sel_time})\n"
            f"📍 {info['from']} → {info['to']}\n"
            f"📅 {info['date']}\n"
            f"💓 Heartbeat: {hb_status}\n\n"
            f"Используйте /status для просмотра статуса, /mytracks для управления или /stop для остановки.",
            parse_mode="HTML"
        )

@bot.callback_query_handler(func=lambda call: call.data == "back_to_list")
def on_back(call):
    chat_id = call.message.chat.id
    info = user_data.get(chat_id)
    if not info:
        logger.warning(f"Пользователь {chat_id} попытался вернуться к списку, но сессия утеряна")
        bot.send_message(chat_id, "Сессия утеряна. Используйте /track")
        return

    logger.info(f"🔙 Пользователь {chat_id} вернулся к списку поездов | Маршрут: {info['from']} → {info['to']}")
    bot.answer_callback_query(call.id, "Обновляю список...")
    trains = get_trains_list(info['from'], info['to'], info['date'], chat_id)
    
    if not trains:
        bot.send_message(chat_id, "Список пуст.")
        return

    show_train_list(chat_id, trains)

@bot.callback_query_handler(func=lambda call: call.data == "view_status")
def on_view_status(call):
    """Обработчик кнопки просмотра статуса"""
    chat_id = call.message.chat.id
    
    if chat_id not in tracking_status and not get_user_trackings(chat_id):
        logger.warning(f"Пользователь {chat_id} попытался просмотреть статус, но отслеживание не активно")
        bot.answer_callback_query(call.id, "ℹ️ У вас нет активного отслеживания", show_alert=True)
        return
    
    # Получаем статус из БД если нет в памяти
    if chat_id not in tracking_status:
        trackings = get_user_trackings(chat_id)
        if trackings:
            tracking = trackings[0]
            status = {
                'train_num': tracking['train_num'],
                'train_time': tracking['train_time'],
                'seats_available': tracking['seats_available'],
                'requests_count': tracking['requests_count']
            }
        else:
            bot.answer_callback_query(call.id, "ℹ️ У вас нет активного отслеживания", show_alert=True)
            return
    else:
        status = tracking_status[chat_id]
    
    info = user_data.get(chat_id, {})
    if not info and get_user_trackings(chat_id):
        tracking = get_user_trackings(chat_id)[0]
        info = {
            'from': tracking['from_station'],
            'to': tracking['to_station'],
            'passengers': tracking['passengers']
        }
    
    # Логирование просмотра статуса пользователем
    logger.info(f"📊 Пользователь {chat_id} просмотрел статус | Поезд №{status['train_num'] or 'N/A'} ({status['train_time']}) | Мест доступно: {status['seats_available']}")
    
    msg = f"📊 <b>Статус отслеживания</b>\n\n"
    msg += f"🚂 Поезд №: {status['train_num'] or 'Ожидание...'}\n"
    msg += f"⏰ Время: {status['train_time']}\n"
    msg += f"🪑 Доступно мест: {status['seats_available']}\n"
    msg += f"🔄 Запросов выполнено: {status['requests_count']}\n"
    msg += f"👥 Нужно мест: {info.get('passengers', 'N/A')}\n"
    msg += f"📍 Маршрут: {info.get('from', 'N/A')} → {info.get('to', 'N/A')}\n"
    
    bot.answer_callback_query(call.id, "Статус обновлен", show_alert=False)
    bot.send_message(chat_id, msg, parse_mode="HTML")

# Обработчик кнопок главного меню и быстрых действий
@bot.callback_query_handler(func=lambda call: call.data in ["quick_start", "back_to_main"])
def on_quick_start(call):
    """Обработчик кнопки быстрого старта поиска"""
    chat_id = call.message.chat.id
    bot.answer_callback_query(call.id)
    # Имитируем команду /track
    call.message.text = "/track"
    start_track(call.message)

@bot.callback_query_handler(func=lambda call: isinstance(call.data, str) and call.data.startswith("repeat_search_"))
def on_repeat_search(call):
    """Обработчик повтора поиска из истории"""
    chat_id = call.message.chat.id
    data = call.data.replace("repeat_search_", "").split("_")
    
    if len(data) < 4:
        bot.answer_callback_query(call.id, "Ошибка формата данных", show_alert=True)
        return
    
    # Парсим данные: from_to_date_passengers
    passengers = int(data[-1])
    date = data[-2]
    to_station = data[-3]
    from_station = "_".join(data[:-3])  # Все что до to_station (может содержать _)
    
    logger.info(f"🔁 Пользователь {chat_id} повторил поиск: {from_station} → {to_station} ({date})")
    
    # Сохраняем в user_data и запускаем процесс поиска
    user_data[chat_id] = {
        'from': from_station,
        'to': to_station,
        'date': date,
        'passengers': passengers
    }
    
    bot.answer_callback_query(call.id, f"Поиск: {from_station} → {to_station}")
    
    # Сразу показываем календарь для выбора даты (или используем сохраненную)
    calendar_result = calendar.get_calendar()
    bot.send_message(
        chat_id, 
        f"📅 Выберите дату поездки для {from_station} → {to_station}:",
        reply_markup=calendar_result
    )

@bot.callback_query_handler(func=lambda call: isinstance(call.data, str) and call.data.startswith("fav_add_"))
def on_add_to_favorites(call):
    """Обработчик добавления маршрута в избранное"""
    chat_id = call.message.chat.id
    data = call.data.replace("fav_add_", "").split("_")
    
    if len(data) < 3:
        bot.answer_callback_query(call.id, "Ошибка формата данных", show_alert=True)
        return
    
    # Парсим данные: from_to_passengers
    passengers = int(data[-1])
    to_station = data[-2]
    from_station = "_".join(data[:-2])  # Все что до to_station
    
    try:
        save_favorite_route(chat_id, from_station, to_station, passengers)
        bot.answer_callback_query(call.id, f"⭐ Маршрут {from_station} → {to_station} добавлен в избранное!", show_alert=True)
        
        # Логируем webhook событие
        log_webhook_event("favorite_added", {
            "chat_id": chat_id,
            "from_station": from_station,
            "to_station": to_station,
            "passengers": passengers
        }, chat_id)
    except Exception as e:
        logger.error(f"Ошибка при добавлении в избранное: {e}")
        bot.answer_callback_query(call.id, "❌ Ошибка при добавлении в избранное", show_alert=True)

@bot.callback_query_handler(func=lambda call: isinstance(call.data, str) and call.data.startswith("fav_search_"))
def on_favorite_search(call):
    """Обработчик быстрого поиска по избранному маршруту"""
    chat_id = call.message.chat.id
    data = call.data.replace("fav_search_", "").split("_")
    
    if len(data) < 4:
        bot.answer_callback_query(call.id, "Ошибка формата данных", show_alert=True)
        return
    
    # Парсим данные: id_from_to_passengers
    route_id = int(data[0])
    passengers = int(data[-1])
    to_station = data[-2]
    from_station = "_".join(data[1:-2])  # Все между id и to_station
    
    logger.info(f"⭐ Пользователь {chat_id} запустил поиск по избранному: {from_station} → {to_station}")
    
    bot.answer_callback_query(call.id, f"🔍 Поиск: {from_station} → {to_station}")
    
    # Сохраняем в user_data и запускаем процесс поиска
    user_data[chat_id] = {
        'from': from_station,
        'to': to_station,
        'date': None,  # Дата будет выбрана через календарь
        'passengers': passengers,
        'favorite_route_id': route_id
    }
    
    # Показываем календарь
    calendar_result = calendar.get_calendar()
    bot.send_message(
        chat_id, 
        f"📅 Выберите дату поездки для {from_station} → {to_station}:",
        reply_markup=calendar_result
    )

@bot.callback_query_handler(func=lambda call: isinstance(call.data, str) and call.data.startswith("fav_delete_"))
def on_delete_favorite(call):
    """Обработчик удаления маршрута из избранного"""
    chat_id = call.message.chat.id
    data = call.data.replace("fav_delete_", "")
    
    try:
        route_id = int(data)
        if remove_favorite_route(chat_id, route_id):
            bot.answer_callback_query(call.id, "✅ Маршрут удален из избранного", show_alert=True)
            
            # Обновляем сообщение со списком избранных
            show_favorites(call.message)
        else:
            bot.answer_callback_query(call.id, "❌ Не удалось удалить маршрут", show_alert=True)
    except ValueError:
        bot.answer_callback_query(call.id, "Ошибка формата ID", show_alert=True)
    except Exception as e:
        logger.error(f"Ошибка при удалении из избранного: {e}")
        bot.answer_callback_query(call.id, "❌ Ошибка при удалении", show_alert=True)

@bot.callback_query_handler(func=lambda call: call.data == "fav_add_new")
def on_add_new_favorite(call):
    """Обработчик добавления нового избранного маршрута"""
    chat_id = call.message.chat.id
    bot.answer_callback_query(call.id)
    # Имитируем команду /track
    call.message.text = "/track"
    start_track(call.message)

@bot.callback_query_handler(func=lambda call: call.data == "refresh_stats")
def on_refresh_stats(call):
    """Обработчик обновления статистики"""
    chat_id = call.message.chat.id
    bot.answer_callback_query(call.id, "Обновление...")
    show_user_stats_impl(call.message, chat_id)

@bot.message_handler(func=lambda message: message.text in ["🚂 Начать поиск", "Начать поиск"])
def on_start_search_button(message):
    """Обработчик кнопки 'Начать поиск' из главного меню"""
    chat_id = message.chat.id
    logger.info(f"Пользователь {chat_id} нажал кнопку 'Начать поиск'")
    start_track(message)

@bot.message_handler(func=lambda message: message.text in ["📊 Мои трекинги", "Мои трекинги"])
def on_my_trackings_button(message):
    """Обработчик кнопки 'Мои трекинги' из главного меню"""
    chat_id = message.chat.id
    logger.info(f"Пользователь {chat_id} нажал кнопку 'Мои трекинги'")
    show_my_trackings(message)

@bot.message_handler(func=lambda message: message.text in ["📜 История", "История"])
def on_history_button(message):
    """Обработчик кнопки 'История' из главного меню"""
    chat_id = message.chat.id
    logger.info(f"Пользователь {chat_id} нажал кнопку 'История'")
    show_history(message)

@bot.message_handler(func=lambda message: message.text in ["❓ Помощь", "Помощь"])
def on_help_button(message):
    """Обработчик кнопки 'Помощь' из главного меню"""
    chat_id = message.chat.id
    logger.info(f"Пользователь {chat_id} нажал кнопку 'Помощь'")
    show_help(message)

@bot.message_handler(func=lambda message: message.text in ["⭐ Избранное", "Избранное"])
def on_favorites_button(message):
    """Обработчик кнопки 'Избранное' из главного меню"""
    chat_id = message.chat.id
    logger.info(f"Пользователь {chat_id} нажал кнопку 'Избранное'")
    show_favorites_impl(message, chat_id)

@bot.message_handler(func=lambda message: message.text in ["📈 Моя статистика", "Моя статистика"])
def on_user_stats_button(message):
    """Обработчик кнопки 'Моя статистика' из главного меню"""
    chat_id = message.chat.id
    logger.info(f"Пользователь {chat_id} нажал кнопку 'Моя статистика'")
    show_user_stats_impl(message, chat_id)

@bot.message_handler(func=lambda message: message.text == "🔙 Назад")
def on_back_button(message):
    """Обработчик кнопки 'Назад' - сбрасывает текущий шаг"""
    chat_id = message.chat.id
    user_steps.pop(chat_id, None)
    user_data.pop(chat_id, None)
    bot.send_message(
        chat_id,
        "🔙 Возврат в главное меню. Выберите действие:",
        reply_markup=ReplyKeyboardMarkup(resize_keyboard=True).add(
            KeyboardButton("🚂 Начать поиск"),
            KeyboardButton("📊 Мои трекинги"),
            KeyboardButton("📜 История"),
            KeyboardButton("❓ Помощь"),
            KeyboardButton("⭐ Избранное"),
            KeyboardButton("📈 Моя статистика")
        )
    )

def show_favorites_impl(message, chat_id):
    """Показывает избранные маршруты пользователя"""
    with get_db_cursor() as cursor:
        cursor.execute("""
            SELECT * FROM favorite_routes 
            WHERE chat_id = ?
            ORDER BY created_at DESC
        """, (chat_id,))
        favorites = cursor.fetchall()
    
    if not favorites:
        msg = "⭐ <b>Избранные маршруты</b>\n\n"
        msg += "У вас пока нет избранных маршрутов.\n\n"
        msg += "Добавляйте маршруты в избранное:\n"
        msg += "• Нажмите кнопку ⭐ под результатами поиска\n"
        msg += "• Или используйте /track для нового поиска"
        
        keyboard = InlineKeyboardMarkup()
        keyboard.add(InlineKeyboardButton("➕ Добавить новый маршрут", callback_data="fav_add_new"))
        
        if hasattr(message, 'reply_to') and callable(getattr(message, 'reply_to', None)):
            message.reply_to(msg, parse_mode="HTML", reply_markup=keyboard)
        else:
            bot.send_message(chat_id, msg, parse_mode="HTML", reply_markup=keyboard)
        return
    
    msg = "⭐ <b>Избранные маршруты</b>\n\n"
    keyboard = InlineKeyboardMarkup(row_width=1)
    
    for fav in favorites:
        from_st = fav['from_station']
        to_st = fav['to_station']
        passengers = fav['passengers']
        route_id = fav['id']
        
        msg += f"🚂 {from_st} → {to_st} ({passengers} пасс.)\n"
        
        # Кнопка быстрого поиска
        btn_text = f"🔍 Быстрый поиск"
        callback_data = f"f av_search_{route_id}_{from_st}_{to_st}_{passengers}".replace(" ", "")
        keyboard.add(InlineKeyboardButton(btn_text, callback_data=callback_data))
        
        # Кнопка удаления
        del_btn = InlineKeyboardButton("❌ Удалить", callback_data=f"f av_delete_{route_id}".replace(" ", ""))
        keyboard.row(del_btn)
        
        msg += "\n"
    
    keyboard.add(InlineKeyboardButton("➕ Добавить новый маршрут", callback_data="fav_add_new"))
    
    if hasattr(message, 'reply_to') and callable(getattr(message, 'reply_to', None)):
        message.reply_to(msg, parse_mode="HTML", reply_markup=keyboard)
    else:
        bot.send_message(chat_id, msg, parse_mode="HTML", reply_markup=keyboard)

def show_user_stats_impl(message, chat_id):
    """Показывает статистику пользователя"""
    stats = get_user_stats(chat_id)
    
    # Получаем количество активных трекингов и избранных маршрутов
    active_trackings = len(get_user_trackings(chat_id))
    
    with get_db_cursor() as cursor:
        cursor.execute("SELECT COUNT(*) as count FROM favorite_routes WHERE chat_id = ?", (chat_id,))
        fav_count = cursor.fetchone()['count']
    
    if not stats:
        # Если статистики еще нет, создаем запись с нулями
        update_user_stats(chat_id, 0, 0, 0.0)
        stats = get_user_stats(chat_id)
    
    total_searches = stats['total_searches'] if stats else 0
    successful_bookings = stats['successful_bookings'] if stats else 0
    total_savings = stats['total_savings'] if stats else 0.0
    
    msg = "📈 <b>Ваша статистика</b>\n\n"
    msg += f"🔍 Всего поисков: <b>{total_searches}</b>\n"
    msg += f"🎫 Успешных бронирований: <b>{successful_bookings}</b>\n"
    msg += f"💰 Сэкономлено: <b>{total_savings:.2f} BYN</b>\n"
    msg += f"🔔 Активных трекингов: <b>{active_trackings}</b>\n"
    msg += f"⭐ Избранных маршрутов: <b>{fav_count}</b>\n"
    
    # Конверсия
    if total_searches > 0:
        conversion = (successful_bookings / total_searches) * 100
        msg += f"📊 Конверсия: <b>{conversion:.1f}%</b>\n"
    
    keyboard = InlineKeyboardMarkup()
    keyboard.add(InlineKeyboardButton("🔁 Обновить", callback_data="refresh_stats"))
    
    if hasattr(message, 'reply_to') and callable(getattr(message, 'reply_to', None)):
        message.reply_to(msg, parse_mode="HTML", reply_markup=keyboard)
    else:
        bot.send_message(chat_id, msg, parse_mode="HTML", reply_markup=keyboard)

# --- ЗАПУСК ---
if __name__ == '__main__':
    logger.info("🚀 Бот запущен в пошаговом режиме.")
    logger.info("📋 Логирование: пользователь, номер поезда, маршрут и все запросы к сайту записываются в лог")
    logger.info("💾 База данных: SQLite для персистентного хранения трекингов")
    logger.info("🔒 Безопасность: rate limiting, санитизация ввода, ограничение на количество трекингов")
    
    # Восстанавливаем активные трекинги после перезапуска
    try:
        restore_active_trackings(bot)
    except Exception as e:
        logger.error(f"⚠️ Ошибка восстановления трекингов: {e}")
    
    try:
        bot.polling(none_stop=True)
    except KeyboardInterrupt:
        logger.info("🛑 Остановка бота пользователем.")
    except Exception as e:
        logger.critical(f"💥 Критическая ошибка: {e}", exc_info=True)
