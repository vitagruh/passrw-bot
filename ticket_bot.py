import telebot
import time
import threading
import re
import sqlite3
import json
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta, date as date_cls
from urllib.parse import urlencode
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from bs4 import BeautifulSoup
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton
from telebot_calendar import Calendar, RUSSIAN_LANGUAGE
from dotenv import load_dotenv
import os
import logging
from logging.handlers import RotatingFileHandler
import traceback
from contextlib import contextmanager
from typing import Optional, Dict, List, Any

# .env должен быть загружен до импорта db/tracking_sync: они читают пути при импорте.
load_dotenv()

import db

# Импорт модуля синхронизации для интеграции с админ-панелью
from tracking_sync import (
    check_stop_request,
    confirm_tracking_stopped,
    cleanup_old_sync_flags,
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
CHECK_INTERVAL = max(10, int(os.getenv("CHECK_INTERVAL", 60)))
DATABASE_PATH = os.getenv("DATABASE_PATH", "data/ticket_bot.db")
HTTP_CACHE_TTL = max(0, int(os.getenv("HTTP_CACHE_TTL", 30)))
REQUEST_TIMEOUT = max(5, int(os.getenv("REQUEST_TIMEOUT", 15)))
MAX_PASSENGERS = max(1, int(os.getenv("MAX_PASSENGERS", 9)))
MAX_TRACKINGS_PER_USER = max(1, int(os.getenv("MAX_TRACKINGS_PER_USER", 5)))
STOP_ON_SUCCESS = os.getenv("STOP_ON_SUCCESS", "true").lower() in ("1", "true", "yes")
MAINTENANCE_INTERVAL = max(300, int(os.getenv("MAINTENANCE_INTERVAL", 21600)))
RETENTION_DAYS = max(1, int(os.getenv("RETENTION_DAYS", 30)))
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "").strip()
NOTIFY_COOLDOWN = max(0, int(os.getenv("NOTIFY_COOLDOWN", 300)))

if not TOKEN:
    logger.critical("❌ TOKEN не найден в .env. Выход.")
    exit(1)

bot = telebot.TeleBot(TOKEN, threaded=True)

# Инициализация календаря для выбора даты
calendar = Calendar(language=RUSSIAN_LANGUAGE)

# Статический пул User-Agent: быстрее и надёжнее, чем fake-useragent на каждый запрос.
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36 Edg/122.0.0.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64; rv:124.0) Gecko/20100101 Firefox/124.0",
]

# ============================================
# БАЗА ДАННЫХ (ПЕРСИСТЕНТНОЕ ХРАНИЛИЩЕ)
# ============================================

# Глобальная блокировка для синхронизации доступа к БД (общая с другими модулями)
db_lock = db.DB_LOCK

def get_db_connection():
    """Создает подключение к базе данных с едиными pragma (см. db.py)."""
    return db.get_connection(DATABASE_PATH)

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
    """Инициализация единой схемы БД и миграций (единственный источник схемы — db.py)."""
    db.init_schema()
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

def ensure_user(chat_id: int):
    """Гарантирует существование пользователя (нужно для внешних ключей)."""
    with get_db_cursor_locked() as cursor:
        cursor.execute("INSERT OR IGNORE INTO users (chat_id) VALUES (?)", (chat_id,))


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
                        heartbeat_enabled: bool = False, heartbeat_interval: int = 1800,
                        train_num: str = None) -> int:
    """
    Сохраняет активный трекинг и возвращает его ID.
    Повторный запуск того же поезда не создаёт дубликат, а обновляет настройки.
    """
    with get_db_cursor_locked() as cursor:
        cursor.execute("""
            SELECT id FROM active_trackings
            WHERE chat_id = ? AND from_station = ? AND to_station = ? AND date = ?
              AND train_time = ? AND COALESCE(train_num, '') = COALESCE(?, '')
            ORDER BY id DESC LIMIT 1
        """, (chat_id, from_station, to_station, date, train_time, train_num))
        existing = cursor.fetchone()
        if existing:
            cursor.execute("""
                UPDATE active_trackings
                SET heartbeat_enabled = ?, heartbeat_interval = ?, is_stopped = 0,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
            """, (1 if heartbeat_enabled else 0, heartbeat_interval, existing['id']))
            return existing['id']

        cursor.execute("""
            INSERT INTO active_trackings
            (chat_id, from_station, to_station, date, passengers, train_time, train_num,
             heartbeat_enabled, heartbeat_interval)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (chat_id, from_station, to_station, date, passengers, train_time, train_num,
              1 if heartbeat_enabled else 0, heartbeat_interval))
        return cursor.lastrowid


def get_tracking_by_id(tracking_id: int) -> Optional[sqlite3.Row]:
    with get_db_cursor() as cursor:
        cursor.execute("SELECT * FROM active_trackings WHERE id = ?", (tracking_id,))
        return cursor.fetchone()

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

def update_tracking_status(tracking_id: int, seats_available: int = None,
                           train_num: str = None, requests_count: int = None,
                           last_request_count: int = None):
    """Обновляет статус конкретного трекинга по его ID."""
    with get_db_cursor_locked() as cursor:
        updates = ["updated_at = CURRENT_TIMESTAMP"]
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

        if last_request_count is not None:
            updates.append("last_request_count = ?")
            params.append(last_request_count)

        params.append(tracking_id)
        cursor.execute(
            f"UPDATE active_trackings SET {', '.join(updates)} WHERE id = ?",
            params,
        )

def save_search_history(chat_id: int, from_station: str, to_station: str,
                        date: str, passengers: int) -> int:
    """Сохраняет поиск в историю и возвращает ID записи."""
    with get_db_cursor_locked() as cursor:
        cursor.execute("""
            INSERT INTO search_history
            (chat_id, from_station, to_station, date, passengers)
            VALUES (?, ?, ?, ?, ?)
        """, (chat_id, from_station, to_station, date, passengers))
        history_id = cursor.lastrowid
        
        # Обновляем статистику пользователя (+1 поиск) - напрямую, без вызова update_user_stats_locked
        # (чтобы избежать deadlock, т.к. мы уже внутри блокировки)
        cursor.execute("SELECT chat_id FROM user_stats WHERE chat_id = ?", (chat_id,))
        exists = cursor.fetchone()

        if exists:
            cursor.execute("""
                UPDATE user_stats
                SET total_searches = total_searches + 1,
                    last_stats_update = CURRENT_TIMESTAMP
                WHERE chat_id = ?
            """, (chat_id,))
        else:
            cursor.execute("""
                INSERT INTO user_stats (chat_id, total_searches, successful_bookings, total_savings)
                VALUES (?, 1, 0, 0)
            """, (chat_id,))
        
        # Обновляем счетчик использования станций
        for station in [from_station, to_station]:
            cursor.execute("""
                INSERT INTO popular_stations (station_name, usage_count, last_used)
                VALUES (?, 1, CURRENT_TIMESTAMP)
                ON CONFLICT(station_name) DO UPDATE SET
                    usage_count = usage_count + 1,
                    last_used = CURRENT_TIMESTAMP
            """, (station,))
        return history_id


def get_search_history_entry(history_id: int, chat_id: int) -> Optional[sqlite3.Row]:
    with get_db_cursor() as cursor:
        cursor.execute(
            "SELECT * FROM search_history WHERE id = ? AND chat_id = ?",
            (history_id, chat_id),
        )
        return cursor.fetchone()


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

def get_favorite_route(chat_id: int, route_id: int) -> Optional[sqlite3.Row]:
    """Возвращает избранный маршрут по ID с проверкой владельца."""
    with get_db_cursor() as cursor:
        cursor.execute(
            "SELECT * FROM favorite_routes WHERE id = ? AND chat_id = ?",
            (route_id, chat_id),
        )
        return cursor.fetchone()


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


# --- НАСТРОЙКИ ПОЛЬЗОВАТЕЛЯ (P2) ---

SETTINGS_FIELDS = (
    "quiet_from", "quiet_to", "notify_threshold",
    "default_passengers", "carriage_filter", "max_price",
)

# callback-ключ -> подстрока типа вагона (None = любой)
CARRIAGE_FILTERS = {
    "any": None,
    "plaz": "плацкарт",
    "kupe": "купе",
    "sv": "св",
    "sid": "сидяч",
    "lux": "люкс",
}

CARRIAGE_LABELS = {
    "any": "любой",
    "plaz": "плацкарт",
    "kupe": "купе",
    "sv": "СВ",
    "sid": "сидячий",
    "lux": "люкс",
}

QUIET_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)\s*-\s*([01]\d|2[0-3]):([0-5]\d)$")


def get_user_settings(chat_id: int) -> Dict[str, Any]:
    """Возвращает настройки пользователя с безопасными значениями по умолчанию."""
    settings: Dict[str, Any] = {
        "quiet_from": None,
        "quiet_to": None,
        "notify_threshold": 1,
        "default_passengers": 1,
        "carriage_filter": None,
        "max_price": None,
    }
    try:
        with get_db_cursor() as cursor:
            cursor.execute("SELECT * FROM user_settings WHERE chat_id = ?", (chat_id,))
            row = cursor.fetchone()
        if row:
            for name in SETTINGS_FIELDS:
                if row[name] is not None:
                    settings[name] = row[name]
    except Exception as e:
        logger.warning(f"⚠️ Не удалось прочитать настройки {chat_id}: {e}")
    return settings


def save_user_settings(chat_id: int, **fields) -> bool:
    """Сохраняет переданные поля настроек (остальные не меняются)."""
    values = {k: v for k, v in fields.items() if k in SETTINGS_FIELDS}
    if not values:
        return False
    ensure_user(chat_id)
    merged = get_user_settings(chat_id)
    merged.update(values)
    try:
        with get_db_cursor_locked() as cursor:
            cursor.execute("""
                INSERT INTO user_settings
                    (chat_id, quiet_from, quiet_to, notify_threshold,
                     default_passengers, carriage_filter, max_price, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(chat_id) DO UPDATE SET
                    quiet_from = excluded.quiet_from,
                    quiet_to = excluded.quiet_to,
                    notify_threshold = excluded.notify_threshold,
                    default_passengers = excluded.default_passengers,
                    carriage_filter = excluded.carriage_filter,
                    max_price = excluded.max_price,
                    updated_at = CURRENT_TIMESTAMP
            """, (
                chat_id,
                merged.get("quiet_from"),
                merged.get("quiet_to"),
                int(merged.get("notify_threshold") or 1),
                int(merged.get("default_passengers") or 1),
                merged.get("carriage_filter"),
                merged.get("max_price"),
            ))
        return True
    except Exception as e:
        logger.error(f"⚠️ Не удалось сохранить настройки {chat_id}: {e}")
        return False


def parse_quiet_hours(text: str):
    """'22:00-08:00' -> ('22:00', '08:00'); 'off' -> (None, None); иначе None."""
    if not text:
        return None
    cleaned = text.strip().lower()
    if cleaned in ("off", "выкл", "нет", "-", "0"):
        return None, None
    match = QUIET_RE.match(cleaned)
    if not match:
        return None
    start = f"{match.group(1)}:{match.group(2)}"
    end = f"{match.group(3)}:{match.group(4)}"
    if start == end:
        return None
    return start, end


def in_quiet_hours(settings: Dict[str, Any], now: Optional[datetime] = None) -> bool:
    """Проверяет попадание текущего времени в тихие часы (поддерживает через полночь)."""
    start_text = settings.get("quiet_from")
    end_text = settings.get("quiet_to")
    if not start_text or not end_text:
        return False
    try:
        start_h, start_m = (int(part) for part in str(start_text).split(":"))
        end_h, end_m = (int(part) for part in str(end_text).split(":"))
    except (ValueError, TypeError):
        return False
    current = now or db.local_now()
    cur = current.hour * 60 + current.minute
    start = start_h * 60 + start_m
    end = end_h * 60 + end_m
    if start == end:
        return False
    if start < end:
        return start <= cur < end
    return cur >= start or cur < end  # ночной интервал


def settings_summary(settings: Dict[str, Any]) -> str:
    """Человекочитаемое описание настроек."""
    quiet = (
        f"{settings['quiet_from']}–{settings['quiet_to']}"
        if settings.get("quiet_from") and settings.get("quiet_to") else "выключены"
    )
    carriage = settings.get("carriage_filter") or "любой"
    max_price = settings.get("max_price")
    return (
        f"🕓 Тихие часы: <b>{quiet}</b>\n"
        f"🎫 Тип вагона: <b>{carriage}</b>\n"
        f"💰 Макс. цена: <b>{f'{max_price:.2f} BYN' if max_price else 'не задана'}</b>\n"
        f"🔔 Порог мест: <b>{settings.get('notify_threshold') or 1}</b>\n"
        f"👥 Пассажиров по умолчанию: <b>{settings.get('default_passengers') or 1}</b>"
    )


def get_station_suggestions(query: str, limit: int = 5) -> List[str]:
    """Подсказки станций по подстроке (популярные + базовый справочник)."""
    text = (query or "").strip()
    if len(text) < 2:
        return []
    lowered = text.casefold()
    candidates: List[str] = []
    try:
        with get_db_cursor() as cursor:
            # LOWER() в SQLite не работает с кириллицей — фильтруем в Python.
            cursor.execute("""
                SELECT station_name FROM popular_stations
                ORDER BY usage_count DESC, station_name
            """)
            for row in cursor.fetchall():
                name = row["station_name"]
                if lowered in name.casefold():
                    candidates.append(name)
                    if len(candidates) >= limit:
                        break
    except Exception as e:
        logger.warning(f"⚠️ Не удалось получить подсказки станций: {e}")

    for station in DEFAULT_STATIONS:
        if lowered in station.casefold() and station not in candidates:
            candidates.append(station)

    # Точное совпадение не нуждается в подсказке.
    if any(name.casefold() == lowered for name in candidates):
        return []
    return candidates[:limit]


def build_trend_text(tracking_id: int, points: int = 20) -> Optional[str]:
    """Текст с историей наличия мест и цен для трекинга."""
    tracking = get_tracking_by_id(tracking_id)
    if not tracking:
        return None
    with get_db_cursor() as cursor:
        rows = cursor.execute("""
            SELECT seats_available, min_price, checked_at
            FROM availability_history
            WHERE tracking_id = ?
            ORDER BY id DESC
            LIMIT ?
        """, (tracking_id, points)).fetchall()

    if not rows:
        return (
            f"📈 <b>История наличия</b>\n"
            f"{tracking['from_station']} → {tracking['to_station']}, "
            f"{tracking['train_time']}\n\n"
            "Данных пока нет — бот ещё не успел сделать замеры."
        )

    rows = list(reversed(rows))
    seats = [int(row["seats_available"] or 0) for row in rows]
    top = max(seats) or 1
    blocks = "▁▂▃▄▅▆▇█"
    spark = "".join(blocks[min(7, int(value / top * 7))] for value in seats)

    prices = [float(row["min_price"]) for row in rows if row["min_price"] is not None]
    price_line = ""
    if prices:
        last_price = prices[-1]
        price_line = (
            f"💰 Цена: <b>{last_price:.2f} BYN</b> "
            f"(мин. {min(prices):.2f}, макс. {max(prices):.2f})\n"
        )
        if len(prices) >= 2 and last_price != prices[0]:
            delta = last_price - prices[0]
            arrow = "🔻" if delta < 0 else "🔺"
            price_line += f"{arrow} Изменение: {delta:+.2f} BYN\n"

    recent = "\n".join(
        f"{db.fmt_local(row['checked_at'], '%d.%m %H:%M')} — "
        f"{int(row['seats_available'] or 0)} мест"
        + (f", {float(row['min_price']):.2f} BYN" if row["min_price"] is not None else "")
        for row in rows[-8:]
    )

    return (
        f"📈 <b>История наличия</b>\n"
        f"{tracking['from_station']} → {tracking['to_station']}, "
        f"поезд {tracking['train_time']} (№{tracking['train_num'] or '—'})\n"
        f"<code>{spark}</code>\n"
        f"Текущее: <b>{seats[-1]}</b> мест\n"
        f"{price_line}\n"
        f"<b>Последние замеры:</b>\n{recent}"
    )


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

# --- ХРАНИЛИЩЕ ДАННЫХ И СОСТОЯНИЙ (IN-MEMORY) ---
# Каждый трекинг идентифицируется уникальным ID из БД. Это позволяет
# отслеживать несколько поездов одного пользователя без конфликтов.
@dataclass
class TrackingJob:
    tracking_id: int
    chat_id: int
    from_station: str
    to_station: str
    date: str
    train_time: str
    passengers: int
    train_num: Optional[str] = None
    heartbeat_enabled: bool = False
    heartbeat_interval: int = 1800
    stop_event: threading.Event = field(default_factory=threading.Event)
    thread: Optional[threading.Thread] = None
    last_signature: Optional[tuple] = None
    last_notify_ts: float = 0.0


active_jobs: Dict[int, TrackingJob] = {}  # {tracking_id: TrackingJob}
jobs_lock = threading.RLock()
user_steps: Dict[int, str] = {}   # {chat_id: 'step_name'}
user_data: Dict[int, Dict[str, Any]] = {}  # {chat_id: {'from': ..., 'selected': ...}}

# Тексты кнопок главного меню: их нельзя трактовать как ввод станции.
MENU_TEXTS = {
    "🚂 Начать поиск", "Начать поиск",
    "📊 Мои трекинги", "Мои трекинги",
    "📜 История", "История",
    "⭐ Избранное", "Избранное",
    "📈 Моя статистика", "Моя статистика",
    "⚙️ Настройки", "Настройки",
    "❓ Помощь", "Помощь",
    "🔙 Назад",
    "📊 Админ-статистика", "📜 Просмотр логов", "👥 Пользователи",
}


def get_user_jobs(chat_id: int) -> List[TrackingJob]:
    """Возвращает запущенные задачи пользователя."""
    with jobs_lock:
        return [job for job in active_jobs.values() if job.chat_id == chat_id]


def stop_job(tracking_id: int, chat_id: int = None, delete_from_db: bool = True,
             reason: str = "user_stop") -> Optional[TrackingJob]:
    """
    Останавливает задачу трекинга.

    При delete_from_db=True запись удаляется из active_trackings; поток
    завершится сам и подтвердит остановку через confirm_tracking_stopped().
    """
    with jobs_lock:
        job = active_jobs.get(tracking_id)
    owner = chat_id or (job.chat_id if job else None)
    if delete_from_db and owner is not None:
        remove_tracking_from_db(owner, tracking_id=tracking_id)
    if job:
        job.stop_event.set()
    logger.info(
        f"⏹ Остановка трекинга id={tracking_id} (chat_id={owner}, reason={reason})"
    )
    return job


def start_tracking_job(tracking_id: int, chat_id: int, from_station: str, to_station: str,
                       date: str, passengers: int, train_time: str,
                       train_num: str = None, heartbeat_enabled: bool = False,
                       heartbeat_interval: int = 1800) -> TrackingJob:
    """Создаёт (или переиспользует) поток отслеживания конкретного трекинга."""
    with jobs_lock:
        existing = active_jobs.get(tracking_id)
        if existing and not existing.stop_event.is_set():
            existing.heartbeat_enabled = bool(heartbeat_enabled)
            existing.heartbeat_interval = int(heartbeat_interval or 1800)
            existing.train_num = train_num or existing.train_num
            existing.passengers = int(passengers or existing.passengers)
            return existing
        if existing:
            # Поток уже завершается — создаём новый для перезапущенного трекинга.
            active_jobs.pop(tracking_id, None)

        job = TrackingJob(
            tracking_id=tracking_id,
            chat_id=chat_id,
            from_station=from_station,
            to_station=to_station,
            date=date,
            train_time=train_time,
            passengers=int(passengers or 1),
            train_num=train_num,
            heartbeat_enabled=bool(heartbeat_enabled),
            heartbeat_interval=int(heartbeat_interval or 1800),
        )
        thread = threading.Thread(
            target=tracking_worker,
            args=(job,),
            daemon=True,
            name=f"track-{tracking_id}",
        )
        job.thread = thread
        active_jobs[tracking_id] = job
        thread.start()
        return job


def restore_active_trackings(bot_instance=None):
    """Восстанавливает активные трекинги после перезапуска бота."""
    try:
        with get_db_cursor() as cursor:
            cursor.execute("SELECT * FROM active_trackings")
            trackings = cursor.fetchall()
    except Exception as e:
        logger.error(f"⚠️ Не удалось прочитать активные трекинги: {e}", exc_info=True)
        return

    restored_count = 0
    for tracking in trackings:
        # Остановленные админом записи не восстанавливаем, а корректно закрываем.
        if tracking['is_stopped']:
            confirm_tracking_stopped(
                tracking['chat_id'], tracking_id=tracking['id'], reason='admin_stop'
            )
            continue

        start_tracking_job(
            tracking_id=tracking['id'],
            chat_id=tracking['chat_id'],
            from_station=tracking['from_station'],
            to_station=tracking['to_station'],
            date=tracking['date'],
            passengers=tracking['passengers'],
            train_time=tracking['train_time'],
            train_num=tracking['train_num'],
            heartbeat_enabled=bool(tracking['heartbeat_enabled']),
            heartbeat_interval=tracking['heartbeat_interval'] or 1800,
        )
        restored_count += 1
        logger.info(
            f"🔄 Восстановлен трекинг #{tracking['id']} для {tracking['chat_id']}: "
            f"{tracking['from_station']} → {tracking['to_station']} ({tracking['train_time']})"
        )

    if restored_count:
        logger.info(f"✅ Восстановлено {restored_count} активных трекингов")
    cleanup_old_sync_flags(max_age_hours=24)


def maintenance_worker():
    """Периодическая очистка старых логов, истории и sync-флагов."""
    while True:
        try:
            deleted = db.cleanup_old_data(RETENTION_DAYS)
            flags = cleanup_old_sync_flags(max_age_hours=24)
            logger.info(
                f"🧹 Обслуживание БД: удалено строк — {deleted}, sync-флагов — {flags} "
                f"(хранение {RETENTION_DAYS} дн.)"
            )
        except Exception as e:
            logger.error(f"⚠️ Ошибка обслуживания БД: {e}", exc_info=True)
        time.sleep(MAINTENANCE_INTERVAL)


def start_maintenance_thread() -> threading.Thread:
    """Запускает фоновый поток обслуживания БД (daemon)."""
    thread = threading.Thread(target=maintenance_worker, name="db-maintenance", daemon=True)
    thread.start()
    logger.info(f"🧹 Поток обслуживания БД запущен (интервал {MAINTENANCE_INTERVAL}с)")
    return thread



# Инициализация базы данных при старте
init_database()

# Запрещенные символы для XSS защиты
FORBIDDEN_CHARS_PATTERN = re.compile(r'''[<>"'&]''')

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


DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
STATION_RE = re.compile(r"^[A-Za-zА-Яа-яЁё0-9\s\-\.\(\)]+$")


def validate_station_name(text: str) -> bool:
    """Станция: 2–64 символа, буквы/цифры/пробелы/дефис/точка/скобки."""
    if not text:
        return False
    text = text.strip()
    return 2 <= len(text) <= 64 and bool(STATION_RE.match(text))


def parse_travel_date(text: str) -> Optional[str]:
    """Проверяет дату поездки и возвращает её в формате YYYY-MM-DD."""
    if not text or not DATE_RE.match(text.strip()):
        return None
    try:
        parsed = datetime.strptime(text.strip(), "%Y-%m-%d").date()
    except ValueError:
        return None
    today = date_cls.today()
    if parsed < today or parsed > today + timedelta(days=365):
        return None
    return parsed.strftime("%Y-%m-%d")


def parse_passengers(text: str) -> Optional[int]:
    """Количество пассажиров: целое число от 1 до MAX_PASSENGERS."""
    if not text or not text.strip().isdigit():
        return None
    value = int(text.strip())
    return value if 1 <= value <= MAX_PASSENGERS else None


def get_headers() -> dict:
    """Заголовки браузера со случайным User-Agent из статического пула."""
    return {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
        "Accept-Encoding": "gzip, deflate",
        "Connection": "keep-alive",
    }


# Единая HTTP-сессия с keep-alive и автоматическими повторами.
_http_retry = Retry(
    total=3,
    connect=3,
    read=2,
    status=3,
    backoff_factor=0.6,
    status_forcelist=(429, 500, 502, 503, 504),
    allowed_methods=frozenset(["GET"]),
    raise_on_status=False,
)
HTTP_SESSION = requests.Session()
_adapter = HTTPAdapter(max_retries=_http_retry, pool_connections=8, pool_maxsize=8)
HTTP_SESSION.mount("https://", _adapter)
HTTP_SESSION.mount("http://", _adapter)

_HTTP_CACHE: Dict[tuple, tuple] = {}
_HTTP_CACHE_LOCK = threading.Lock()


def fetch_page(url: str, params: dict = None, user_info: str = "System") -> Optional[str]:
    """
    Загружает HTML с коротким кэшем и повторами.

    Возвращает None при сетевой ошибке, чтобы вызывающий код отличал
    «поездов нет» от «сайт недоступен».
    """
    key = (url, tuple(sorted((params or {}).items())))
    now = time.time()
    if HTTP_CACHE_TTL > 0:
        with _HTTP_CACHE_LOCK:
            cached = _HTTP_CACHE.get(key)
            if cached and now - cached[0] < HTTP_CACHE_TTL:
                return cached[1]

    try:
        response = HTTP_SESSION.get(
            url, params=params, headers=get_headers(), timeout=REQUEST_TIMEOUT
        )
        response.raise_for_status()
        logger.debug(f"✅ HTTP {response.status_code} | {user_info} | {response.url}")
    except requests.Timeout:
        logger.error(f"⏱ Таймаут запроса к {url} | {user_info}")
        return None
    except requests.RequestException as e:
        logger.error(f"🌐 Ошибка запроса к {url}: {e} | {user_info}")
        return None

    text = response.text
    if HTTP_CACHE_TTL > 0:
        with _HTTP_CACHE_LOCK:
            _HTTP_CACHE[key] = (now, text)
            if len(_HTTP_CACHE) > 128:
                oldest = sorted(_HTTP_CACHE.items(), key=lambda item: item[1][0])[:32]
                for old_key, _ in oldest:
                    _HTTP_CACHE.pop(old_key, None)
    return text


def get_trains_list(from_station, to_station, date, chat_id=None):
    """
    Получение списка поездов с логированием запроса.

    Args:
        from_station: станция отправления
        to_station: станция назначения
        date: дата поездки
        chat_id: ID чата пользователя (для логирования)
    """
    user_info = f"User(chat_id={chat_id})" if chat_id else "System"
    logger.info(
        f"🔍 Запрос поиска билетов: {user_info} | "
        f"Маршрут: {from_station} → {to_station} | Дата: {date}"
    )

    html = fetch_page(
        "https://pass.rw.by/ru/route/",
        params={"from": from_station, "to": to_station, "date": date},
        user_info=user_info,
    )
    if html is None:
        return []

    soup = BeautifulSoup(html, 'html.parser')
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
                'parsed_info': parsed_info,
            })
        except Exception as e:
            logger.warning(f"⚠️ Пропущена строка поезда: {e} | {user_info}")
            continue

    train_numbers = [t['num'] for t in trains]
    logger.info(
        f"✅ Найдено {len(trains)} поездов для {user_info} | "
        f"Поезда №№: {', '.join(train_numbers) if train_numbers else 'нет данных'}"
    )
    return trains

def parse_carriage_info(status_cell):
    carriages = []
    items = status_cell.find_all('div', class_='sch-table__t-item')

    for item in items:
        try:
            type_elem = item.find('div', class_='sch-table__t-name')
            carriage_type = type_elem.get_text(strip=True) if type_elem else ""
            carriage_type = carriage_type or "Неизвестный"

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

def _record_availability(job: "TrackingJob", train: dict, seats: int, min_price: Optional[float]):
    """Пишет точку истории наличия мест (основа для трендов и алертов).

    Дубликаты не пишутся: новая строка появляется только при изменении
    количества мест или минимальной цены. Это бережёт БД при частых опросах.
    """
    try:
        with get_db_cursor_locked() as cursor:
            last = cursor.execute("""
                SELECT seats_available, min_price FROM availability_history
                WHERE tracking_id = ? ORDER BY id DESC LIMIT 1
            """, (job.tracking_id,)).fetchone()
            if last:
                same_seats = (last['seats_available'] or 0) == (seats or 0)
                last_price = last['min_price']
                if last_price is None or min_price is None:
                    same_price = last_price is None and min_price is None
                else:
                    same_price = abs(float(last_price) - float(min_price)) < 0.01
                if same_seats and same_price:
                    return
            cursor.execute("""
                INSERT INTO availability_history
                (tracking_id, chat_id, train_num, train_time, seats_available, min_price)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                job.tracking_id, job.chat_id, train.get('num'),
                job.train_time, seats, min_price,
            ))
    except Exception as e:
        logger.warning(f"⚠️ Не удалось записать историю наличия: {e}")


def _safe_send(chat_id: int, text: str, **kwargs) -> bool:
    """Отправка сообщения, не роняющая поток при блокировке бота пользователем."""
    try:
        bot.send_message(chat_id, text, **kwargs)
        return True
    except Exception as e:
        logger.warning(f"⚠️ Не удалось отправить сообщение {chat_id}: {e}")
        return False


def tracking_worker(job: "TrackingJob"):
    """Поток отслеживания одного конкретного трекинга."""
    class FakeMessage:
        from_user = type('User', (), {
            'id': job.chat_id, 'username': None, 'full_name': 'TrackingWorker',
            'language_code': 'unknown', 'is_bot': False,
        })()
        chat = type('Chat', (), {'id': job.chat_id})()

    fake_msg = FakeMessage()
    log_action(
        fake_msg, "TRACKING_STARTED",
        f"ID: {job.tracking_id}, From: {job.from_station}, To: {job.to_station}, "
        f"Date: {job.date}, Train: {job.train_time}",
    )

    last_heartbeat = time.time()
    reason = "stopped"
    logger.info(
        f"▶️ Трекинг #{job.tracking_id} запущен: {job.chat_id} | "
        f"{job.from_station} → {job.to_station} | {job.date} | {job.train_time}"
    )

    try:
        while not job.stop_event.is_set():
            # Синхронизация с админ-панелью: STOP/FORCE_STOP.
            if check_stop_request(
                job.chat_id, job.train_time, tracking_id=job.tracking_id
            ):
                reason = "admin_stop"
                logger.info(f"🛑 Получен запрос остановки трекинга #{job.tracking_id}")
                break

            # Читаем актуальные настройки из БД: админ мог изменить heartbeat
            # или удалить трекинг напрямую.
            row = get_tracking_by_id(job.tracking_id)
            if not row:
                reason = "deleted"
                logger.info(f"🗑 Трекинг #{job.tracking_id} удалён из БД, завершаем поток")
                break
            if row['is_stopped']:
                reason = "admin_stop"
                break

            job.heartbeat_enabled = bool(row['heartbeat_enabled'])
            job.heartbeat_interval = int(row['heartbeat_interval'] or 1800)
            job.train_num = row['train_num'] or job.train_num
            job.passengers = int(row['passengers'] or job.passengers)
            previous_seats = int(row['seats_available'] or 0)

            # Heartbeat-сообщение о том, что мониторинг жив.
            if job.heartbeat_enabled and (time.time() - last_heartbeat) >= job.heartbeat_interval:
                log_action(
                    fake_msg, "HEARTBEAT_SENT",
                    f"ID: {job.tracking_id}, Interval: {job.heartbeat_interval}s",
                )
                _safe_send(job.chat_id, "💓 Бот работает, проверяю билеты...")
                last_heartbeat = time.time()

            trains = get_trains_list(
                job.from_station, job.to_station, job.date, job.chat_id
            )
            current_train = None
            if trains:
                current_train = next(
                    (t for t in trains if t['time'] == job.train_time
                     and (not job.train_num or t['num'] == job.train_num)),
                    None,
                )
                # Если поезд перенумеровали — продолжаем следить за слотом времени.
                if current_train is None and job.train_num:
                    current_train = next(
                        (t for t in trains if t['time'] == job.train_time), None
                    )
                    if current_train:
                        logger.warning(
                            f"⚠️ Трекинг #{job.tracking_id}: номер поезда изменился "
                            f"{job.train_num} → {current_train['num']}"
                        )
                # Если сдвинулось время — ищем по номеру поезда.
                if current_train is None and job.train_num:
                    current_train = next(
                        (t for t in trains if t['num'] == job.train_num), None
                    )
                    if current_train:
                        logger.warning(
                            f"⚠️ Трекинг #{job.tracking_id}: время отправления изменилось "
                            f"{job.train_time} → {current_train['time']}"
                        )

            requests_count = int(row['requests_count'] or 0) + 1

            if not current_train:
                log_action(
                    fake_msg, "TRACKING_TRAIN_NOT_FOUND",
                    f"ID: {job.tracking_id}, Train: {job.train_time} not found",
                )
                update_tracking_status(
                    job.tracking_id, requests_count=requests_count,
                    last_request_count=previous_seats,
                )
                if job.stop_event.wait(CHECK_INTERVAL):
                    break
                continue

            max_seats = 0
            min_price = None
            for c in current_train['parsed_info']:
                if c['seats'].isdigit():
                    max_seats = max(max_seats, int(c['seats']))
                try:
                    price = float(str(c.get('price_byn')).replace(',', '.'))
                    if price > 0:
                        min_price = price if min_price is None else min(min_price, price)
                except (TypeError, ValueError):
                    pass

            update_tracking_status(
                job.tracking_id,
                seats_available=max_seats,
                train_num=current_train['num'],
                requests_count=requests_count,
                last_request_count=previous_seats,
            )
            _record_availability(job, current_train, max_seats, min_price)

            settings = get_user_settings(job.chat_id)
            threshold = max(
                int(job.passengers or 1),
                int(settings.get('notify_threshold') or 1),
            )
            carriage_filter = str(settings.get('carriage_filter') or '').casefold()
            max_price = settings.get('max_price')

            suitable = []
            for c in current_train['parsed_info']:
                if not c['seats'].isdigit() or int(c['seats']) < threshold:
                    continue
                if carriage_filter and carriage_filter not in str(c.get('type') or '').casefold():
                    continue
                if max_price:
                    try:
                        price = float(str(c.get('price_byn')).replace(',', '.'))
                    except (TypeError, ValueError):
                        continue  # цену проверить нельзя — не рискуем
                    if price > float(max_price):
                        continue
                suitable.append(c)

            if suitable:
                signature = tuple(sorted(
                    (c['type'], c['seats'], str(c['price_byn'])) for c in suitable
                ))
                now = time.time()
                should_notify = (
                    signature != job.last_signature
                    and (now - job.last_notify_ts) >= NOTIFY_COOLDOWN
                )
                if should_notify and in_quiet_hours(settings):
                    logger.debug(
                        f"🌙 Тихие часы: уведомление по трекингу #{job.tracking_id} "
                        f"отложено до окончания {settings.get('quiet_to')}"
                    )
                    should_notify = False
                if should_notify:
                    first_success = job.last_signature is None
                    job.last_signature = signature
                    job.last_notify_ts = now
                    if first_success:
                        try:
                            update_user_stats_locked(job.chat_id, bookings_increment=1)
                        except Exception as stats_error:
                            logger.warning(f"⚠️ Не удалось обновить статистику: {stats_error}")
                    msg = (
                        f"🎉 <b>УСПЕХ!</b> Места для {job.passengers} чел. "
                        f"в поезде {current_train['time']} появились!\n\n"
                        f"📍 {job.from_station} → {job.to_station}\n📅 {job.date}"
                    )
                    _safe_send(job.chat_id, msg, parse_mode="HTML")
                    send_detailed_train_info(
                        job.chat_id,
                        current_train,
                        job.passengers,
                        with_button=True,
                        from_station=job.from_station,
                        to_station=job.to_station,
                        date=job.date,
                    )
                    log_action(
                        fake_msg, "TRACKING_SUCCESS",
                        f"ID: {job.tracking_id}, Train: {current_train['num']} "
                        f"({job.train_time}), Seats: {max_seats}",
                    )
                    if STOP_ON_SUCCESS:
                        reason = "success"
                        break

            logger.debug(
                f"[{datetime.now().strftime('%H:%M')}] Трекинг #{job.tracking_id} | "
                f"Поезд: {job.train_time} | Мест: {max_seats} | Ждём..."
            )
            if job.stop_event.wait(CHECK_INTERVAL):
                break

    except Exception as e:
        reason = "error"
        logger.error(f"Ошибка в потоке трекинга #{job.tracking_id}: {e}", exc_info=True)
    finally:
        with jobs_lock:
            active_jobs.pop(job.tracking_id, None)
        try:
            confirm_tracking_stopped(
                job.chat_id, tracking_id=job.tracking_id, reason=reason
            )
        except Exception as e:
            logger.error(
                f"Ошибка подтверждения остановки трекинга #{job.tracking_id}: {e}",
                exc_info=True,
            )
        log_action(
            fake_msg, "TRACKING_STOPPED",
            f"ID: {job.tracking_id}, reason={reason}",
        )
        logger.info(f"🏁 Трекинг #{job.tracking_id} завершён (reason={reason})")

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
    buy_url = None
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
    keyboard.add(KeyboardButton("⚙️ Настройки"), KeyboardButton("❓ Помощь"))
    
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
        log_user_action(chat_id, "ADMIN_ACCESS_DENIED", "Non-admin user tried to access admin menu")
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
        created_at = db.fmt_local(tracking['created_at'], "%d.%m.%Y %H:%M")

        # Формируем понятное описание трекинга
        text += f"<b>№{i}</b> {status_emoji} <b>{tracking['from_station']} → {tracking['to_station']}</b>\n"
        text += f"   📅 Дата поездки: {tracking['date']}\n"
        text += f"   🚂 Поезд: {tracking['train_time']} (№{tracking['train_num'] or 'ожидание'})\n"
        text += f"   👥 Нужно мест: {tracking['passengers']} | Доступно: {tracking['seats_available']}\n"
        text += f"   💓 Heartbeat: {'вкл' if tracking['heartbeat_enabled'] else 'выкл'}\n"
        text += f"   🔁 Запросов: {tracking['requests_count']} | ⏰ создан: {created_at}\n\n"
        
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
        date_str = db.fmt_local(search['searched_at'], "%d.%m.%Y %H:%M")
        text += f"{i}. 🚂 <b>{search['from_station']} → {search['to_station']}</b>\n"
        text += f"   📅 {search['date']} | 👥 {search['passengers']} чел.\n"
        text += f"   ⏰ {date_str}\n\n"

        # Короткий callback: только ID записи истории (не упирается в лимит 64 байта).
        btn_text = f"🔁 Повторить: {search['from_station']} → {search['to_station']}"
        keyboard.add(InlineKeyboardButton(
            btn_text[:60], callback_data=f"repeat_search_{search['id']}"
        ))
    
    bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=keyboard)
    log_action(message, "HISTORY_REQUESTED", "User requested search history")

def _begin_search(chat_id: int):
    """Запускает пошаговый ввод маршрута (общая логика для кнопок и команд)."""
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
        reply_markup=keyboard,
    )
    logger.info(f"Начат трек для {chat_id}. Шаг: ask_from")


@bot.message_handler(commands=['track'])
def start_track(message):
    chat_id = message.chat.id
    ensure_user(chat_id)
    save_user(
        chat_id,
        getattr(message.from_user, 'username', None),
        getattr(message.from_user, 'first_name', None),
        getattr(message.from_user, 'last_name', None),
    )
    _begin_search(chat_id)

@bot.message_handler(commands=['stop'])
def stop_tracking_cmd(message):
    chat_id = message.chat.id

    trackings = get_user_trackings(chat_id)

    if not trackings:
        bot.reply_to(
            message,
            "ℹ️ У вас нет активных задач.\n"
            "Используйте /mytracks для просмотра всех трекингов.",
        )
        return

    keyboard = InlineKeyboardMarkup(row_width=1)
    for i, tracking in enumerate(trackings, 1):
        btn_text = (
            f"❌ №{i}: {tracking['from_station']} → {tracking['to_station']} "
            f"({tracking['train_time']})"
        )
        keyboard.add(InlineKeyboardButton(
            btn_text[:60], callback_data=f"stop_tracking_{tracking['id']}"
        ))
    keyboard.add(InlineKeyboardButton("⛔ Остановить ВСЕ", callback_data="stop_all_trackings"))

    bot.send_message(
        chat_id,
        f"⏹ <b>Выберите трекинг для остановки ({len(trackings)} активных):</b>",
        parse_mode="HTML",
        reply_markup=keyboard,
    )
    log_action(message, "STOP_REQUESTED", f"User requested to stop tracking. Count: {len(trackings)}")


@bot.callback_query_handler(
    func=lambda call: call.data == "stop_all_trackings"
    or call.data.startswith("stop_tracking_")
    or call.data.startswith("delete_tracking_")
)
def on_stop_tracking_choice(call):
    """Остановка/удаление конкретного трекинга по его ID."""
    chat_id = call.message.chat.id

    if call.data == "stop_all_trackings":
        trackings = get_user_trackings(chat_id)
        if not trackings:
            bot.answer_callback_query(call.id, "Нет активных трекингов")
            return
        for tracking in trackings:
            stop_job(tracking['id'], chat_id=chat_id, reason="user_stop_all")
        bot.answer_callback_query(call.id, "✅ Все трекинги остановлены")
        bot.send_message(chat_id, f"⏹ Остановлено трекингов: {len(trackings)}.")
        log_action(call, "STOP_ALL", f"count={len(trackings)}")
        return

    prefix = "delete_tracking_" if call.data.startswith("delete_tracking_") else "stop_tracking_"
    try:
        tracking_id = int(call.data[len(prefix):])
    except ValueError:
        bot.answer_callback_query(call.id, "Ошибка формата данных", show_alert=True)
        return

    tracking = get_tracking_by_id(tracking_id)
    if not tracking or tracking['chat_id'] != chat_id:
        bot.answer_callback_query(call.id, "❌ Трекинг не найден", show_alert=True)
        bot.send_message(chat_id, "❌ Трекинг не найден или уже был удалён.")
        return

    stop_job(tracking_id, chat_id=chat_id, reason="user_stop")
    bot.answer_callback_query(call.id, "✅ Трекинг остановлен")
    bot.send_message(
        chat_id,
        f"⏹ Трекинг #{tracking_id} "
        f"({tracking['from_station']} → {tracking['to_station']}, "
        f"{tracking['train_time']}) остановлен.",
    )
    log_action(
        call, "TRACKING_STOPPED",
        f"ID: {tracking_id}, route: {tracking['from_station']} → {tracking['to_station']}",
    )


# Обработчик календаря для выбора даты
@bot.callback_query_handler(func=lambda call: isinstance(call.data, str) and call.data.startswith("calendar:"))
def on_calendar_selection(call):
    """Обработчик выбора даты из календаря telebot-calendar."""
    chat_id = call.message.chat.id

    if user_steps.get(chat_id) != 'ask_date':
        bot.answer_callback_query(call.id, "⚠️ Сейчас не требуется выбор даты", show_alert=True)
        return

    try:
        parts = call.data.split(":")
        if len(parts) < 5:
            bot.answer_callback_query(call.id)
            return

        action = parts[1]
        year, month = int(parts[2]), int(parts[3])
        day = int(parts[4]) if parts[4] != '!' else 1

        # name должен совпадать с префиксом callback_data ("calendar"),
        # иначе кнопки навигации перестанут обрабатываться.
        result = calendar.calendar_query_handler(
            bot, call, "calendar", action, year, month, day
        )

        # telebot-calendar возвращает datetime для выбранного дня
        # и кортеж/None для навигации, IGNORE и CANCEL.
        if isinstance(result, datetime):
            selected_date = result.date()
            if selected_date < date_cls.today():
                bot.send_message(chat_id, "❌ Нельзя выбрать прошедшую дату. Выберите другую.")
                return
            if selected_date > date_cls.today() + timedelta(days=365):
                bot.send_message(chat_id, "❌ Дата слишком далеко. Выберите дату в пределах года.")
                return

            date_str = selected_date.strftime("%Y-%m-%d")
            user_data.setdefault(chat_id, {})['date'] = date_str
            user_steps[chat_id] = 'ask_passengers'

            _ask_passengers(chat_id, date_str)
            return

        if isinstance(result, tuple) and result and result[0] == "CANCEL":
            user_steps.pop(chat_id, None)
            user_data.pop(chat_id, None)
            bot.send_message(chat_id, "Выбор даты отменён. Используйте /track для нового поиска.")
            return

        # Навигация (MONTHS/MONTH/PREVIOUS/NEXT) и IGNORE уже обработаны библиотекой.
    except ValueError as e:
        logger.warning(f"Ошибка разбора даты из календаря: {e}, data={call.data}")
        bot.answer_callback_query(call.id)
    except Exception as e:
        logger.error(f"Ошибка обработки календаря: {e}", exc_info=True)
        try:
            bot.answer_callback_query(
                call.id, "❌ Ошибка при выборе даты. Попробуйте ещё раз.", show_alert=True
            )
        except Exception:
            pass


@bot.message_handler(commands=['status'])
def show_tracking_status(message):
    """Показывает статус всех активных трекингов пользователя."""
    chat_id = message.chat.id
    trackings = get_user_trackings(chat_id)

    if not trackings:
        bot.reply_to(
            message,
            "ℹ️ У вас нет активного отслеживания.\n"
            "Используйте /track для создания запроса.",
        )
        return

    text = f"📊 <b>Активные трекинги ({len(trackings)}):</b>\n\n"
    keyboard = InlineKeyboardMarkup(row_width=1)

    for i, tracking in enumerate(trackings, 1):
        created = db.fmt_local(tracking['created_at'], "%d.%m.%Y %H:%M")
        seats = int(tracking['seats_available'] or 0)
        status_emoji = "✅" if seats > 0 else "🔄"
        text += f"{i}. {status_emoji} <b>{tracking['from_station']} → {tracking['to_station']}</b>\n"
        text += f"   🚂 {tracking['train_time']} (№{tracking['train_num'] or 'ожидание'})\n"
        text += f"   📅 {tracking['date']} | 👥 нужно {tracking['passengers']}\n"
        text += f"   🪑 доступно: {seats} | 🔁 запросов: {tracking['requests_count']}\n"
        text += f"   💓 heartbeat: {'вкл' if tracking['heartbeat_enabled'] else 'выкл'}"
        text += f" | ⏰ создан: {created}\n\n"
        keyboard.add(
            InlineKeyboardButton(
                f"📈 История №{i}", callback_data=f"trend_{tracking['id']}"
            ),
            InlineKeyboardButton(
                f"🔗 Поделиться №{i}", callback_data=f"share_{tracking['id']}"
            ),
        )
        keyboard.add(InlineKeyboardButton(
            f"⏹ Остановить №{i}", callback_data=f"stop_tracking_{tracking['id']}"
        ))

    bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=keyboard)


@bot.callback_query_handler(func=lambda call: isinstance(call.data, str) and call.data.startswith("trend_"))
def on_trend(call):
    """Показывает историю наличия мест и цен по трекингу."""
    chat_id = call.message.chat.id
    try:
        tracking_id = int(call.data.split("_", 1)[1])
    except (IndexError, ValueError):
        bot.answer_callback_query(call.id, "❌ Некорректный трекинг", show_alert=True)
        return

    tracking = get_tracking_by_id(tracking_id)
    if not tracking or tracking['chat_id'] != chat_id:
        bot.answer_callback_query(call.id, "❌ Трекинг не найден", show_alert=True)
        return

    bot.answer_callback_query(call.id)
    text = build_trend_text(tracking_id)
    keyboard = InlineKeyboardMarkup()
    keyboard.add(InlineKeyboardButton("🔁 Обновить", callback_data=f"trend_{tracking_id}"))
    bot.send_message(
        chat_id, text or "📈 Данных пока нет.", parse_mode="HTML", reply_markup=keyboard
    )


@bot.callback_query_handler(func=lambda call: isinstance(call.data, str) and call.data.startswith("share_"))
def on_share(call):
    """Делится маршрутом трекинга ссылкой."""
    chat_id = call.message.chat.id
    try:
        tracking_id = int(call.data.split("_", 1)[1])
    except (IndexError, ValueError):
        bot.answer_callback_query(call.id, "❌ Некорректный трекинг", show_alert=True)
        return

    tracking = get_tracking_by_id(tracking_id)
    if not tracking or tracking['chat_id'] != chat_id:
        bot.answer_callback_query(call.id, "❌ Трекинг не найден", show_alert=True)
        return

    route_url = "https://pass.rw.by/ru/route/?" + urlencode({
        "from": tracking['from_station'],
        "to": tracking['to_station'],
        "date": tracking['date'],
    })
    share_text = (
        f"Отслеживаю билеты {tracking['from_station']} → {tracking['to_station']} "
        f"на {tracking['date']} ({tracking['train_time']}, №{tracking['train_num'] or '—'})"
    )
    share_url = "https://t.me/share/url?" + urlencode({
        "url": route_url,
        "text": share_text,
    })

    bot.answer_callback_query(call.id)
    keyboard = InlineKeyboardMarkup()
    keyboard.add(InlineKeyboardButton("📤 Поделиться в Telegram", url=share_url))
    keyboard.add(InlineKeyboardButton("🎫 Открыть маршрут на RW.BY", url=route_url))
    bot.send_message(
        chat_id,
        "🔗 <b>Поделиться маршрутом</b>\n\n"
        f"{tracking['from_station']} → {tracking['to_station']}\n"
        f"📅 {tracking['date']} | 🚂 {tracking['train_time']} "
        f"(№{tracking['train_num'] or '—'})",
        parse_mode="HTML",
        reply_markup=keyboard,
    )

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


# --- НАСТРОЙКИ (P2) ---

def show_settings(chat_id: int):
    """Экран настроек пользователя."""
    settings = get_user_settings(chat_id)
    keyboard = InlineKeyboardMarkup(row_width=2)
    keyboard.add(
        InlineKeyboardButton("🕓 Тихие часы", callback_data="settings_quiet"),
        InlineKeyboardButton("💰 Макс. цена", callback_data="settings_price"),
    )
    keyboard.add(
        InlineKeyboardButton("🎫 Тип вагона", callback_data="settings_carriage"),
        InlineKeyboardButton("🔔 Порог мест", callback_data="settings_threshold"),
    )
    keyboard.add(InlineKeyboardButton("👥 Пассажиры по умолчанию", callback_data="settings_pax"))
    keyboard.add(InlineKeyboardButton("♻️ Сбросить настройки", callback_data="settings_reset"))
    bot.send_message(
        chat_id,
        "⚙️ <b>Настройки</b>\n\n" + settings_summary(settings) +
        "\n\nВыберите, что изменить:",
        parse_mode="HTML",
        reply_markup=keyboard,
    )


@bot.message_handler(commands=['settings'])
def settings_command(message):
    chat_id = message.chat.id
    ensure_user(chat_id)
    user_steps.pop(chat_id, None)
    show_settings(chat_id)


@bot.message_handler(func=lambda message: message.text in ["⚙️ Настройки", "Настройки"])
def on_settings_button(message):
    settings_command(message)


@bot.callback_query_handler(func=lambda call: call.data == "settings_open")
def on_settings_open(call):
    chat_id = call.message.chat.id
    bot.answer_callback_query(call.id)
    show_settings(chat_id)


@bot.callback_query_handler(func=lambda call: call.data == "settings_quiet")
def on_settings_quiet(call):
    chat_id = call.message.chat.id
    user_steps[chat_id] = 'settings_quiet'
    bot.answer_callback_query(call.id)
    bot.send_message(
        chat_id,
        "🕓 <b>Тихие часы</b>\n"
        "В это время уведомления не отправляются (мониторинг продолжается).\n\n"
        "Отправьте интервал в формате <code>22:00-08:00</code> "
        "или <code>off</code>, чтобы выключить:",
        parse_mode="HTML",
    )


@bot.callback_query_handler(func=lambda call: call.data == "settings_price")
def on_settings_price(call):
    chat_id = call.message.chat.id
    user_steps[chat_id] = 'settings_price'
    bot.answer_callback_query(call.id)
    bot.send_message(
        chat_id,
        "💰 <b>Максимальная цена</b>\n"
        "Уведомлять только о местах не дороже указанной суммы в BYN.\n\n"
        "Отправьте число (например <code>150</code>) или <code>off</code>:",
        parse_mode="HTML",
    )


@bot.callback_query_handler(func=lambda call: call.data == "settings_carriage")
def on_settings_carriage(call):
    chat_id = call.message.chat.id
    bot.answer_callback_query(call.id)
    keyboard = InlineKeyboardMarkup(row_width=2)
    for key, label in CARRIAGE_LABELS.items():
        keyboard.add(InlineKeyboardButton(label, callback_data=f"settings_carriage_set_{key}"))
    keyboard.add(InlineKeyboardButton("🔙 Назад", callback_data="settings_open"))
    bot.send_message(chat_id, "🎫 <b>Тип вагона</b>\nВыберите фильтр:", parse_mode="HTML", reply_markup=keyboard)


@bot.callback_query_handler(func=lambda call: call.data.startswith("settings_carriage_set_"))
def on_settings_carriage_set(call):
    chat_id = call.message.chat.id
    key = call.data.rsplit("_", 1)[-1]
    if key not in CARRIAGE_FILTERS:
        bot.answer_callback_query(call.id, "❌ Неизвестный тип", show_alert=True)
        return
    save_user_settings(chat_id, carriage_filter=CARRIAGE_FILTERS[key])
    bot.answer_callback_query(call.id, f"✅ {CARRIAGE_LABELS[key]}")
    show_settings(chat_id)


@bot.callback_query_handler(func=lambda call: call.data == "settings_threshold")
def on_settings_threshold(call):
    chat_id = call.message.chat.id
    bot.answer_callback_query(call.id)
    keyboard = InlineKeyboardMarkup(row_width=3)
    for value in range(1, MAX_PASSENGERS + 1):
        keyboard.add(InlineKeyboardButton(str(value), callback_data=f"settings_threshold_set_{value}"))
    keyboard.add(InlineKeyboardButton("🔙 Назад", callback_data="settings_open"))
    bot.send_message(
        chat_id,
        "🔔 <b>Порог мест</b>\nУведомлять, когда доступно не меньше N мест:",
        parse_mode="HTML",
        reply_markup=keyboard,
    )


@bot.callback_query_handler(func=lambda call: call.data.startswith("settings_threshold_set_"))
def on_settings_threshold_set(call):
    chat_id = call.message.chat.id
    value = int(call.data.rsplit("_", 1)[-1])
    if not 1 <= value <= MAX_PASSENGERS:
        bot.answer_callback_query(call.id, "❌ Некорректное значение", show_alert=True)
        return
    save_user_settings(chat_id, notify_threshold=value)
    bot.answer_callback_query(call.id, f"✅ Порог: {value}")
    show_settings(chat_id)


@bot.callback_query_handler(func=lambda call: call.data == "settings_pax")
def on_settings_pax(call):
    chat_id = call.message.chat.id
    bot.answer_callback_query(call.id)
    keyboard = InlineKeyboardMarkup(row_width=3)
    for value in range(1, 6):
        keyboard.add(InlineKeyboardButton(str(value), callback_data=f"settings_pax_set_{value}"))
    keyboard.add(InlineKeyboardButton("🔙 Назад", callback_data="settings_open"))
    bot.send_message(
        chat_id,
        "👥 <b>Пассажиры по умолчанию</b>\n"
        "Значение подставляется в новые поиски (если не указано иное):",
        parse_mode="HTML",
        reply_markup=keyboard,
    )


@bot.callback_query_handler(func=lambda call: call.data.startswith("settings_pax_set_"))
def on_settings_pax_set(call):
    chat_id = call.message.chat.id
    value = int(call.data.rsplit("_", 1)[-1])
    if not 1 <= value <= MAX_PASSENGERS:
        bot.answer_callback_query(call.id, "❌ Некорректное значение", show_alert=True)
        return
    save_user_settings(chat_id, default_passengers=value)
    bot.answer_callback_query(call.id, f"✅ Пассажиров: {value}")
    show_settings(chat_id)


@bot.callback_query_handler(func=lambda call: call.data == "settings_reset")
def on_settings_reset(call):
    chat_id = call.message.chat.id
    save_user_settings(
        chat_id,
        quiet_from=None, quiet_to=None, notify_threshold=1,
        default_passengers=1, carriage_filter=None, max_price=None,
    )
    bot.answer_callback_query(call.id, "♻️ Настройки сброшены")
    show_settings(chat_id)


def _is_step_input(message) -> bool:
    """Ввод в пошаговой форме, но не команды и не кнопки меню."""
    text = (message.text or "").strip()
    return (
        message.chat.id in user_steps
        and bool(text)
        and text not in MENU_TEXTS
        and not text.startswith("/")
    )


def _accept_from(chat_id: int, station: str):
    """Принимает станцию отправления и переходит к вводу назначения."""
    user_data.setdefault(chat_id, {})['from'] = station
    user_steps[chat_id] = 'ask_to'
    bot.send_message(
        chat_id,
        f"2️⃣ <b>Куда едем?</b>\nВы указали: <i>{station}</i>\n"
        "Теперь напишите станцию назначения:",
        parse_mode="HTML",
    )


def _accept_to(chat_id: int, station: str):
    """Принимает станцию назначения и показывает календарь."""
    user_data.setdefault(chat_id, {})['to'] = station
    user_steps[chat_id] = 'ask_date'
    try:
        default_date = datetime.now() + timedelta(days=1)
        calendar_keyboard = calendar.create_calendar(
            year=default_date.year, month=default_date.month
        )
    except Exception as e:
        logger.warning(f"Ошибка создания календаря: {e}")
        calendar_keyboard = calendar.create_calendar()

    bot.send_message(
        chat_id,
        f"3️⃣ <b>Дата поездки?</b>\n"
        f"Маршрут: <i>{user_data[chat_id]['from']} → {station}</i>\n"
        "Выберите дату в календаре или введите её в формате ГГГГ-ММ-ДД:",
        parse_mode="HTML",
        reply_markup=calendar_keyboard,
    )


def _ask_passengers(chat_id: int, date_str: str):
    """Спрашивает число пассажиров с быстрыми кнопками (по умолчанию — из настроек)."""
    default = int(get_user_settings(chat_id).get('default_passengers') or 1)
    keyboard = InlineKeyboardMarkup(row_width=4)
    buttons = []
    for value in range(1, min(5, MAX_PASSENGERS) + 1):
        label = f"⭐ {value}" if value == default else str(value)
        buttons.append(InlineKeyboardButton(label, callback_data=f"pax_set_{value}"))
    keyboard.add(*buttons)
    bot.send_message(
        chat_id,
        f"4️⃣ <b>Сколько пассажиров?</b>\nДата: <i>{date_str}</i>\n"
        f"Введите число от 1 до {MAX_PASSENGERS} или выберите:",
        parse_mode="HTML",
        reply_markup=keyboard,
    )


def _start_search_with_passengers(chat_id: int, num_pax: int, source=None):
    """Запускает поиск поездов для собранного маршрута."""
    info = user_data.get(chat_id) or {}
    if not info.get('from') or not info.get('to') or not info.get('date'):
        user_steps.pop(chat_id, None)
        bot.send_message(chat_id, "❌ Данные поиска устарели. Начните заново: /track")
        return

    info['passengers'] = num_pax
    info.pop('selected', None)
    info.pop('selected_train', None)
    user_steps.pop(chat_id, None)

    loading_msg = bot.send_message(
        chat_id,
        f"🔍 Ищу поезда по маршруту {info['from']} → {info['to']} "
        f"на {info['date']}...",
    )
    if source is not None:
        log_action(
            source, "SEARCH_STARTED",
            f"From: {info['from']}, To: {info['to']}, Date: {info['date']}, "
            f"Passengers: {num_pax}",
        )

    trains = get_trains_list(info['from'], info['to'], info['date'], chat_id)

    try:
        bot.delete_message(chat_id, loading_msg.message_id)
    except Exception:
        pass

    if not trains:
        kb = InlineKeyboardMarkup()
        kb.add(InlineKeyboardButton("🔁 Попробовать снова", callback_data="quick_start"))
        bot.send_message(
            chat_id,
            "❌ Поездов не найдено. Проверьте названия станций и дату, "
            "либо сайт временно недоступен.",
            reply_markup=kb,
        )
        return

    show_train_list(chat_id, trains)


@bot.callback_query_handler(func=lambda call: isinstance(call.data, str) and call.data.startswith("pax_set_"))
def on_passengers_quick(call):
    """Быстрый выбор числа пассажиров инлайн-кнопкой."""
    chat_id = call.message.chat.id
    try:
        value = int(call.data.rsplit("_", 1)[-1])
    except ValueError:
        bot.answer_callback_query(call.id, "❌ Некорректное значение", show_alert=True)
        return
    num_pax = parse_passengers(str(value))
    if num_pax is None:
        bot.answer_callback_query(call.id, "❌ Некорректное значение", show_alert=True)
        return
    bot.answer_callback_query(call.id, f"👥 {num_pax}")
    _start_search_with_passengers(chat_id, num_pax, source=call)


@bot.callback_query_handler(
    func=lambda call: isinstance(call.data, str)
    and (call.data.startswith("sugg_from_") or call.data.startswith("sugg_to_"))
)
def on_station_suggestion(call):
    """Выбор станции из подсказки."""
    chat_id = call.message.chat.id
    direction = "from" if call.data.startswith("sugg_from_") else "to"
    data = user_data.get(chat_id, {})

    if call.data.endswith("_keep"):
        name = data.get("pending_station")
    else:
        try:
            index = int(call.data.rsplit("_", 1)[-1])
        except ValueError:
            index = -1
        suggestions = data.get("station_suggestions") or []
        name = suggestions[index] if 0 <= index < len(suggestions) else None

    if not name:
        bot.answer_callback_query(call.id, "⚠️ Подсказка устарела, введите станцию заново", show_alert=True)
        return

    data.pop("pending_station", None)
    data.pop("station_suggestions", None)
    user_steps[chat_id] = "ask_from"
    bot.answer_callback_query(call.id, f"✅ {name}")
    if direction == "from":
        _accept_from(chat_id, name)
    else:
        _accept_to(chat_id, name)


@bot.message_handler(func=_is_step_input)
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
    logger.info(f"📝 Пользователь {chat_id} вводит на шаге {current_step}: '{text}'")

    if current_step == 'settings_quiet':
        parsed = parse_quiet_hours(text)
        if parsed is None:
            bot.send_message(
                chat_id,
                "❌ Неверный формат. Отправьте <code>22:00-08:00</code> или <code>off</code>:",
                parse_mode="HTML",
            )
            return
        start, end = parsed
        save_user_settings(chat_id, quiet_from=start, quiet_to=end)
        user_steps.pop(chat_id, None)
        bot.send_message(
            chat_id,
            "✅ Тихие часы обновлены: " + (f"{start}–{end}" if start and end else "выключены"),
        )
        show_settings(chat_id)
        return

    if current_step == 'settings_price':
        cleaned = text.casefold().replace(",", ".")
        if cleaned in ("off", "0", "нет", "-"):
            value = None
        else:
            try:
                value = float(cleaned)
            except ValueError:
                bot.send_message(chat_id, "❌ Введите число (например 150) или off:")
                return
            if not 1 <= value <= 100000:
                bot.send_message(chat_id, "❌ Цена должна быть от 1 до 100000 BYN:")
                return
        save_user_settings(chat_id, max_price=value)
        user_steps.pop(chat_id, None)
        bot.send_message(
            chat_id,
            "✅ Максимальная цена: " + (f"{value:.2f} BYN" if value else "не задана"),
        )
        show_settings(chat_id)
        return

    if current_step == 'ask_from':
        if not validate_station_name(text):
            bot.send_message(
                chat_id,
                "❌ Название станции должно содержать 2–64 символа: буквы, цифры, "
                "пробелы, дефис. Попробуйте ещё раз:",
            )
            return

        suggestions = get_station_suggestions(text)
        if suggestions:
            data = user_data.setdefault(chat_id, {})
            data['pending_station'] = text
            data['station_suggestions'] = suggestions
            keyboard = InlineKeyboardMarkup(row_width=1)
            for index, name in enumerate(suggestions):
                keyboard.add(InlineKeyboardButton(name, callback_data=f"sugg_from_{index}"))
            keyboard.add(InlineKeyboardButton(
                f"✅ Оставить «{text[:30]}»", callback_data="sugg_from_keep"
            ))
            bot.send_message(
                chat_id,
                f"🔎 Похожие станции для «{text}».\n"
                "Выберите нужную или оставьте введённое название:",
                reply_markup=keyboard,
            )
            return
        _accept_from(chat_id, text)

    elif current_step == 'ask_to':
        if not validate_station_name(text):
            bot.send_message(
                chat_id,
                "❌ Название станции должно содержать 2–64 символа: буквы, цифры, "
                "пробелы, дефис. Попробуйте ещё раз:",
            )
            return

        suggestions = get_station_suggestions(text)
        if suggestions:
            data = user_data.setdefault(chat_id, {})
            data['pending_station'] = text
            data['station_suggestions'] = suggestions
            keyboard = InlineKeyboardMarkup(row_width=1)
            for index, name in enumerate(suggestions):
                keyboard.add(InlineKeyboardButton(name, callback_data=f"sugg_to_{index}"))
            keyboard.add(InlineKeyboardButton(
                f"✅ Оставить «{text[:30]}»", callback_data="sugg_to_keep"
            ))
            bot.send_message(
                chat_id,
                f"🔎 Похожие станции для «{text}».\n"
                "Выберите нужную или оставьте введённое название:",
                reply_markup=keyboard,
            )
            return
        _accept_to(chat_id, text)

    elif current_step == 'ask_date':
        date_str = parse_travel_date(text)
        if not date_str:
            bot.send_message(
                chat_id,
                "❌ Некорректная дата. Используйте ГГГГ-ММ-ДД, дата не должна быть "
                "в прошлом (например: 2026-05-20).\nПопробуйте ещё раз:",
            )
            return

        user_data[chat_id]['date'] = date_str
        user_steps[chat_id] = 'ask_passengers'
        _ask_passengers(chat_id, date_str)

    elif current_step == 'ask_passengers':
        num_pax = parse_passengers(text)
        if num_pax is None:
            bot.send_message(
                chat_id,
                f"❌ Введите целое число пассажиров от 1 до {MAX_PASSENGERS}:",
            )
            return
        _start_search_with_passengers(chat_id, num_pax, source=message)

def show_train_list(chat_id, trains):
    info = user_data.setdefault(chat_id, {})
    info['trains'] = trains
    pax = int(info.get('passengers') or 1)

    keyboard = InlineKeyboardMarkup(row_width=1)

    for index, train in enumerate(trains):
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

        # Индекс вместо времени/номера: не переполняет лимит callback_data (64 байта).
        keyboard.add(InlineKeyboardButton(
            text=btn_text,
            callback_data=f"preview_{index}",
        ))

    keyboard.add(InlineKeyboardButton("🔙 Назад", callback_data="back_to_main"))
    bot.send_message(
        chat_id,
        f"📋 Найдено поездов: {len(trains)}. Выберите нужный для деталей:",
        reply_markup=keyboard,
    )

# --- ОБРАБОТКА НАЖАТИЙ (CALLBACKS) ---

@bot.callback_query_handler(func=lambda call: call.data.startswith("preview_"))
def on_preview(call):
    chat_id = call.message.chat.id
    info = user_data.get(chat_id)

    if not info:
        bot.answer_callback_query(call.id, "Сессия истекла. Начните /track", show_alert=True)
        return

    try:
        index = int(call.data.split("_", 1)[1])
        train = info['trains'][index]
    except (ValueError, IndexError, KeyError):
        bot.answer_callback_query(call.id, "Поезд не найден (обновите список)", show_alert=True)
        return

    info['selected'] = {'time': train['time'], 'num': train['num']}
    info['selected_train'] = train

    logger.info(
        f"👁 Пользователь {chat_id} просматривает поезд №{train['num']} "
        f"({train['time']}) | Маршрут: {info.get('from')} → {info.get('to')}"
    )
    bot.answer_callback_query(call.id)

    send_detailed_train_info(
        chat_id,
        train,
        info.get('passengers'),
        with_button=True,
        from_station=info.get('from'),
        to_station=info.get('to'),
        date=info.get('date'),
    )

    kb = InlineKeyboardMarkup(row_width=1)
    kb.add(InlineKeyboardButton("▶️ Запустить мониторинг", callback_data="confirm_start"))
    kb.add(InlineKeyboardButton("⭐ Добавить маршрут в избранное", callback_data="fav_add_current"))
    kb.add(InlineKeyboardButton("🔙 Назад к списку", callback_data="back_to_list"))

    bot.send_message(
        chat_id,
        f"Запустить непрерывную проверку для поезда №{train['num']} ({train['time']})?",
        reply_markup=kb,
    )

    if get_user_jobs(chat_id):
        bot.send_message(
            chat_id,
            "ℹ️ У вас уже есть активные трекинги. Используйте /status или /mytracks.",
        )


@bot.callback_query_handler(func=lambda call: call.data == "confirm_start")
def on_confirm(call):
    chat_id = call.message.chat.id
    info = user_data.get(chat_id)
    selected = (info or {}).get('selected')

    if not info or not selected:
        bot.answer_callback_query(call.id, "Ошибка сессии. Начните /track", show_alert=True)
        return

    user_trackings = get_user_trackings(chat_id)
    existing = next((
        t for t in user_trackings
        if t['train_time'] == selected['time']
        and (t['train_num'] or '') == (selected.get('num') or '')
        and t['from_station'] == info.get('from')
        and t['to_station'] == info.get('to')
        and t['date'] == info.get('date')
    ), None)

    if len(user_trackings) >= MAX_TRACKINGS_PER_USER and not existing:
        bot.answer_callback_query(
            call.id,
            f"⚠️ Максимум {MAX_TRACKINGS_PER_USER} активных трекингов! "
            "Остановите один из них.",
            show_alert=True,
        )
        return

    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("10 мин", callback_data="hb_600"),
        InlineKeyboardButton("20 мин", callback_data="hb_1200"),
    )
    kb.add(
        InlineKeyboardButton("30 мин", callback_data="hb_1800"),
        InlineKeyboardButton("1 час", callback_data="hb_3600"),
    )
    kb.add(InlineKeyboardButton("❌ Без heartbeat", callback_data="hb_no"))

    bot.answer_callback_query(call.id)
    bot.send_message(
        chat_id,
        "💓 Выберите интервал сообщений «Бот работает»:",
        reply_markup=kb,
    )


@bot.callback_query_handler(
    func=lambda call: call.data in {"hb_600", "hb_1200", "hb_1800", "hb_3600", "hb_no"}
)
def on_heartbeat_choice(call):
    chat_id = call.message.chat.id
    info = user_data.get(chat_id)
    selected = (info or {}).get('selected')

    if not info or not selected:
        bot.answer_callback_query(call.id, "Ошибка сессии. Начните /track", show_alert=True)
        return

    interval_seconds = {
        "hb_600": 600,
        "hb_1200": 1200,
        "hb_1800": 1800,
        "hb_3600": 3600,
        "hb_no": None,
    }[call.data]

    ensure_user(chat_id)

    tracking_id = save_tracking_to_db(
        chat_id,
        info['from'],
        info['to'],
        info['date'],
        info['passengers'],
        selected['time'],
        heartbeat_enabled=interval_seconds is not None,
        heartbeat_interval=interval_seconds or 1800,
        train_num=selected.get('num'),
    )
    save_search_history(
        chat_id, info['from'], info['to'], info['date'], info['passengers']
    )

    start_tracking_job(
        tracking_id=tracking_id,
        chat_id=chat_id,
        from_station=info['from'],
        to_station=info['to'],
        date=info['date'],
        passengers=info['passengers'],
        train_time=selected['time'],
        train_num=selected.get('num'),
        heartbeat_enabled=interval_seconds is not None,
        heartbeat_interval=interval_seconds or 1800,
    )

    hb_status = "выключен"
    if interval_seconds:
        minutes = interval_seconds // 60
        hb_status = f"каждые {minutes // 60} ч." if minutes >= 60 else f"каждые {minutes} мин."

    bot.answer_callback_query(call.id, "✅ Мониторинг запущен")
    bot.send_message(
        chat_id,
        f"✅ <b>Задача отслеживания создана!</b>\n\n"
        f"🚂 Поезд №{selected.get('num')} ({selected['time']})\n"
        f"📍 {info['from']} → {info['to']}\n"
        f"📅 {info['date']}\n"
        f"💓 Heartbeat: {hb_status}\n\n"
        f"ID трекинга: <b>{tracking_id}</b>\n"
        "Используйте /status, /mytracks или /stop для управления.",
        parse_mode="HTML",
    )
    logger.info(
        f"✅ Мониторинг активирован: chat_id={chat_id}, tracking_id={tracking_id}, "
        f"поезд №{selected.get('num')} ({selected['time']}), heartbeat={hb_status}"
    )


@bot.callback_query_handler(func=lambda call: call.data == "back_to_list")
def on_back(call):
    chat_id = call.message.chat.id
    info = user_data.get(chat_id)
    if not info or not info.get('date') or not info.get('from') or not info.get('to'):
        logger.warning(f"Пользователь {chat_id} попытался вернуться к списку, но сессия утеряна")
        bot.answer_callback_query(call.id, "Сессия утеряна", show_alert=True)
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
    """Совместимость со старыми кнопками: показываем актуальный статус."""
    bot.answer_callback_query(call.id, "Статус обновлён")
    show_tracking_status(call.message)


# Обработчик кнопок главного меню и быстрых действий
@bot.callback_query_handler(func=lambda call: call.data in ["quick_start", "back_to_main"])
def on_quick_start(call):
    """Обработчик кнопки быстрого старта поиска"""
    chat_id = call.message.chat.id
    bot.answer_callback_query(call.id)
    _begin_search(chat_id)


@bot.callback_query_handler(func=lambda call: call.data.startswith("repeat_search_"))
def on_repeat_search(call):
    """Повтор поиска из истории по ID записи."""
    chat_id = call.message.chat.id
    try:
        history_id = int(call.data.split("_", 2)[2])
    except (ValueError, IndexError):
        bot.answer_callback_query(call.id, "Ошибка формата данных", show_alert=True)
        return

    entry = get_search_history_entry(history_id, chat_id)
    if not entry:
        bot.answer_callback_query(call.id, "Запись истории не найдена", show_alert=True)
        return

    user_data[chat_id] = {
        'from': entry['from_station'],
        'to': entry['to_station'],
        'date': entry['date'],
        'passengers': entry['passengers'],
    }
    user_steps[chat_id] = 'ask_date'

    default_date = datetime.now() + timedelta(days=1)
    try:
        calendar_keyboard = calendar.create_calendar(
            year=default_date.year, month=default_date.month
        )
    except Exception as e:
        logger.warning(f"Ошибка создания календаря: {e}")
        calendar_keyboard = calendar.create_calendar()

    bot.answer_callback_query(call.id, "Выберите новую дату")
    bot.send_message(
        chat_id,
        f"🔁 <b>Повтор поиска</b>\n"
        f"📍 {entry['from_station']} → {entry['to_station']}\n"
        f"👥 {entry['passengers']} чел.\n\n"
        "📅 Выберите дату поездки:",
        parse_mode="HTML",
        reply_markup=calendar_keyboard,
    )


@bot.callback_query_handler(func=lambda call: call.data == "fav_add_current")
def on_add_to_favorites(call):
    """Добавляет текущий выбранный маршрут в избранное."""
    chat_id = call.message.chat.id
    info = user_data.get(chat_id) or {}
    from_station = info.get('from')
    to_station = info.get('to')
    passengers = int(info.get('passengers') or 1)

    if not from_station or not to_station:
        bot.answer_callback_query(call.id, "Сначала выберите маршрут", show_alert=True)
        return

    existing = next((
        fav for fav in get_user_favorite_routes(chat_id)
        if fav['from_station'] == from_station
        and fav['to_station'] == to_station
        and int(fav['passengers']) == passengers
    ), None)
    if existing:
        bot.answer_callback_query(call.id, "Этот маршрут уже в избранном", show_alert=True)
        return

    try:
        save_favorite_route(chat_id, from_station, to_station, passengers)
        log_webhook_event("favorite_added", {
            "chat_id": chat_id,
            "from_station": from_station,
            "to_station": to_station,
            "passengers": passengers,
        }, chat_id)
        bot.answer_callback_query(
            call.id, f"⭐ {from_station} → {to_station} добавлен в избранное!",
            show_alert=True,
        )
    except Exception as e:
        logger.error(f"Ошибка при добавлении в избранное: {e}")
        bot.answer_callback_query(call.id, "❌ Ошибка при добавлении в избранное", show_alert=True)


@bot.callback_query_handler(func=lambda call: call.data.startswith("fav_search_"))
def on_favorite_search(call):
    """Быстрый поиск по избранному маршруту."""
    chat_id = call.message.chat.id
    try:
        route_id = int(call.data.split("_", 2)[2])
    except (ValueError, IndexError):
        bot.answer_callback_query(call.id, "Ошибка формата данных", show_alert=True)
        return

    route = get_favorite_route(chat_id, route_id)
    if not route:
        bot.answer_callback_query(call.id, "Маршрут не найден", show_alert=True)
        return

    user_data[chat_id] = {
        'from': route['from_station'],
        'to': route['to_station'],
        'date': None,
        'passengers': route['passengers'],
        'favorite_route_id': route_id,
    }
    user_steps[chat_id] = 'ask_date'

    default_date = datetime.now() + timedelta(days=1)
    try:
        calendar_keyboard = calendar.create_calendar(
            year=default_date.year, month=default_date.month
        )
    except Exception as e:
        logger.warning(f"Ошибка создания календаря: {e}")
        calendar_keyboard = calendar.create_calendar()

    bot.answer_callback_query(call.id, f"🔍 {route['from_station']} → {route['to_station']}")
    bot.send_message(
        chat_id,
        f"📅 Выберите дату поездки для "
        f"{route['from_station']} → {route['to_station']}:",
        reply_markup=calendar_keyboard,
    )


@bot.callback_query_handler(func=lambda call: call.data.startswith("fav_delete_"))
def on_delete_favorite(call):
    """Удаление маршрута из избранного."""
    chat_id = call.message.chat.id
    try:
        route_id = int(call.data.split("_", 2)[2])
    except (ValueError, IndexError):
        bot.answer_callback_query(call.id, "Ошибка формата ID", show_alert=True)
        return

    try:
        if remove_favorite_route(chat_id, route_id):
            bot.answer_callback_query(call.id, "✅ Маршрут удалён из избранного")
            show_favorites_impl(call.message, chat_id)
        else:
            bot.answer_callback_query(call.id, "❌ Маршрут не найден", show_alert=True)
    except Exception as e:
        logger.error(f"Ошибка при удалении из избранного: {e}")
        bot.answer_callback_query(call.id, "❌ Ошибка при удалении", show_alert=True)


@bot.callback_query_handler(func=lambda call: call.data == "fav_add_new")
def on_add_new_favorite(call):
    """Добавление нового избранного маршрута через обычный поиск."""
    chat_id = call.message.chat.id
    bot.answer_callback_query(call.id)
    _begin_search(chat_id)


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
            KeyboardButton("📈 Моя статистика"),
            KeyboardButton("⚙️ Настройки")
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
        keyboard.add(InlineKeyboardButton(
            "🔍 Быстрый поиск", callback_data=f"fav_search_{route_id}"
        ))

        # Кнопка удаления
        keyboard.add(InlineKeyboardButton(
            "❌ Удалить", callback_data=f"fav_delete_{route_id}"
        ))
        
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
    msg += f"🎯 Успешных находок мест: <b>{successful_bookings}</b>\n"
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

    # Фоновая очистка логов/истории (P1)
    start_maintenance_thread()
    
    try:
        bot.polling(none_stop=True)
    except KeyboardInterrupt:
        logger.info("🛑 Остановка бота пользователем.")
    except Exception as e:
        logger.critical(f"💥 Критическая ошибка: {e}", exc_info=True)
