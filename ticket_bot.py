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


def log_exception(logger_instance: logging.Logger, message: str = "Произошла ошибка"):
    """
    Вспомогательная функция для логирования исключений с полным traceback.
    """
    logger_instance.error(f"{message}: {traceback.format_exc()}")

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

def get_db_connection():
    """Создает подключение к базе данных с поддержкой внешних ключей"""
    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn

@contextmanager
def get_db_cursor():
    """Контекстный менеджер для работы с базой данных"""
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
    
    with get_db_cursor() as cursor:
        # Таблица пользователей
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                chat_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                last_name TEXT,
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
        
        # Индексы для ускорения поиска
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_trackings_chat ON active_trackings(chat_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_history_chat ON search_history(chat_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_stations_name ON popular_stations(station_name)")
        
        logger.info("✅ База данных успешно инициализирована")

def save_user(chat_id: int, username: str = None, first_name: str = None, last_name: str = None):
    """Сохраняет или обновляет информацию о пользователе"""
    with get_db_cursor() as cursor:
        cursor.execute("""
            INSERT INTO users (chat_id, username, first_name, last_name, last_active)
            VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(chat_id) DO UPDATE SET
                username = excluded.username,
                first_name = excluded.first_name,
                last_name = excluded.last_name,
                last_active = CURRENT_TIMESTAMP
        """, (chat_id, username, first_name, last_name))

def save_tracking_to_db(chat_id: int, from_station: str, to_station: str, 
                        date: str, passengers: int, train_time: str, 
                        heartbeat_enabled: bool = False, heartbeat_interval: int = 1800):
    """Сохраняет активный трекинг в базу данных"""
    with get_db_cursor() as cursor:
        cursor.execute("""
            INSERT INTO active_trackings 
            (chat_id, from_station, to_station, date, passengers, train_time, 
             heartbeat_enabled, heartbeat_interval)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (chat_id, from_station, to_station, date, passengers, train_time,
              1 if heartbeat_enabled else 0, heartbeat_interval))

def remove_tracking_from_db(chat_id: int, train_time: str = None):
    """Удаляет трекинг из базы данных"""
    with get_db_cursor() as cursor:
        if train_time:
            cursor.execute("""
                DELETE FROM active_trackings 
                WHERE chat_id = ? AND train_time = ?
            """, (chat_id, train_time))
        else:
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
    with get_db_cursor() as cursor:
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
    with get_db_cursor() as cursor:
        cursor.execute("""
            INSERT INTO search_history 
            (chat_id, from_station, to_station, date, passengers)
            VALUES (?, ?, ?, ?, ?)
        """, (chat_id, from_station, to_station, date, passengers))
        
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
        thread = threading.Thread(
            target=tracking_worker,
            args=(chat_id, tracking['from_station'], tracking['to_station'], 
                  tracking['date'], tracking['train_time']),
            daemon=True
        )
        thread.start()
        active_jobs[chat_id] = {'thread': thread, 'stop_flag': False}
        
        # Восстанавливаем статус трекинга
        tracking_status[chat_id] = {
            'train_num': tracking['train_num'],
            'train_time': tracking['train_time'],
            'seats_available': tracking['seats_available'],
            'requests_count': tracking['requests_count']
        }
        
        restored_count += 1
        logger.info(f"🔄 Восстановлен трекинг для пользователя {chat_id}: {tracking['from_station']} → {tracking['to_station']}")
    
    if restored_count > 0:
        logger.info(f"✅ Восстановлено {restored_count} активных трекингов после перезапуска")

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
    logger.info(f"🔄 Запуск трекинга: {chat_id} | Поезд: {selected_time} | Маршрут: {from_station} → {to_station}")
    num_passengers = user_data[chat_id].get('passengers', 1)
    last_heartbeat = time.time()
    # Получаем интервал heartbeat для этого пользователя (по умолчанию 1800 сек = 30 мин)
    hb_interval = heartbeat_intervals.get(chat_id, 1800)
    
    # Инициализация статуса отслеживания для пользователя
    tracking_status[chat_id] = {
        'train_num': None,
        'train_time': selected_time,
        'seats_available': 0,
        'requests_count': 0
    }

    while chat_id in active_jobs:
        try:
            # Увеличиваем счетчик запросов
            if chat_id in tracking_status:
                tracking_status[chat_id]['requests_count'] += 1
            
            # Отправка heartbeat сообщения если включено и прошел заданный интервал
            if chat_id in heartbeat_enabled and (time.time() - last_heartbeat) >= hb_interval:
                logger.info(f"💓 Heartbeat для {chat_id} (интервал: {hb_interval} сек) | Поезд: {selected_time}")
                bot.send_message(chat_id, "💓 Бот работает, проверяю билеты...")
                last_heartbeat = time.time()

            trains = get_trains_list(from_station, to_station, date, chat_id)
            current_train = next((t for t in trains if t['time'] == selected_time), None)

            if not current_train:
                logger.warning(f"Поезд {selected_time} не найден в списке для пользователя {chat_id}.")
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
            
            # Обновляем статус в базе данных
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
                send_detailed_train_info(chat_id, current_train, num_passengers)
                
                active_jobs.pop(chat_id, None)
                heartbeat_enabled.discard(chat_id)  # Убираем из heartbeat при успехе
                heartbeat_intervals.pop(chat_id, None)  # Очищаем интервал при успехе
                tracking_status.pop(chat_id, None)  # Очищаем статус при успехе
                
                # Удаляем из базы данных после успешного завершения
                remove_tracking_from_db(chat_id, selected_time)
                
                logger.info(f"✅ Трекинг завершен успешно для {chat_id} | Поезд №{current_train['num']} ({selected_time})")
                return

            logger.debug(f"[{datetime.now().strftime('%H:%M')}] Нет мест для {chat_id} | Поезд: {selected_time}. Ждем...")
            time.sleep(CHECK_INTERVAL)
            
        except Exception as e:
            logger.error(f"Ошибка в потоке трекинга {chat_id}: {e}", exc_info=True)
            time.sleep(CHECK_INTERVAL)


def send_detailed_train_info(chat_id, train, num_passengers=None):
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
    bot.send_message(chat_id, full_text, parse_mode="HTML")

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
    keyboard.add(KeyboardButton("❓ Помощь"))
    
    text = (
        f"👋 Привет, {first_name or 'путешественник'}! Я бот для отслеживания билетов БЖД.\n\n"
        "✨ <b>Мои возможности:</b>\n"
        "• 🔍 Поиск билетов по маршруту\n"
        "• 🔔 Уведомления при появлении мест\n"
        "• 📊 Отслеживание нескольких поездов одновременно\n"
        "• 💓 Heartbeat-сообщения о статусе проверки\n"
        "• 📜 История ваших поисков\n\n"
        "<b>Команды:</b>\n"
        "/track - Начать поиск билетов\n"
        "/mytracks - Показать все активные трекинги\n"
        "/status - Статус текущего трекинга\n"
        "/stop - Остановить трекинг\n"
        "/history - История поисков\n"
        "/help - Подробная справка\n\n"
        "Нажми кнопку ниже или отправь /track чтобы начать!"
    )
    bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=keyboard)
    logger.info(f"👋 Пользователь {chat_id} ({first_name}) запустил бота")

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
    logger.info(f"📘 Пользователь {chat_id} запросил справку")

@bot.message_handler(commands=['mytracks'])
def show_my_trackings(message):
    """Показывает все активные трекинги пользователя"""
    chat_id = message.chat.id
    
    trackings = get_user_trackings(chat_id)
    
    if not trackings:
        bot.reply_to(message, "ℹ️ У вас нет активных трекингов.\nИспользуйте /track для создания нового.")
        return
    
    text = f"📊 <b>Ваши активные трекинги ({len(trackings)}):</b>\n\n"
    
    for i, tracking in enumerate(trackings, 1):
        status_emoji = "✅" if tracking['seats_available'] > 0 else "🔄"
        text += f"{i}. {status_emoji} <b>{tracking['from_station']} → {tracking['to_station']}</b>\n"
        text += f"   📅 Дата: {tracking['date']}\n"
        text += f"   🚂 Поезд: {tracking['train_time']} (№{tracking['train_num'] or 'ожидание'})\n"
        text += f"   👥 Мест: {tracking['passengers']} | Доступно: {tracking['seats_available']}\n"
        text += f"   💓 Heartbeat: {'вкл' if tracking['heartbeat_enabled'] else 'выкл'}\n"
        text += f"   🔁 Запросов: {tracking['requests_count']}\n\n"
    
    keyboard = InlineKeyboardMarkup()
    keyboard.add(InlineKeyboardButton("🔙 Назад к списку", callback_data="back_to_list"))
    
    bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=keyboard)
    logger.info(f"📊 Пользователь {chat_id} запросил список трекингов ({len(trackings)} шт.)")

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
    logger.info(f"📜 Пользователь {chat_id} запросил историю поисков")

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
        logger.info(f"⏹ Пользователь {chat_id} запросил остановку трекинга ({len(trackings)} шт.)")
    else:
        # Только in-memory трекинг (старый формат)
        active_jobs.pop(chat_id)
        tracking_status.pop(chat_id, None)
        bot.reply_to(message, "⏹ Отслеживание остановлено.")
        user_steps.pop(chat_id, None)

@bot.callback_query_handler(func=lambda call: call.data.startswith("stop_tracking_") or call.data == "stop_all_trackings")
def on_stop_tracking_choice(call):
    """Обработчик выбора трекинга для остановки"""
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

    # Останавливаем конкретный трекинг
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
@bot.callback_query_handler(func=lambda call: isinstance(call.data, str) and call.data.startswith("cal-"))
def on_calendar_selection(call):
    """Обработчик выбора даты из календаря"""
    chat_id = call.message.chat.id
    
    # Проверяем, что пользователь находится на шаге выбора даты
    current_step = user_steps.get(chat_id)
    if current_step != 'ask_date':
        bot.answer_callback_query(call.id, "⚠️ Сейчас не требуется выбор даты", show_alert=True)
        return
    
    try:
        # Разбираем callback_data календаря: cal-name-YYYY-MM-DD или cal-name-IGNORE-YYYY-MM-DD
        parts = call.data.split("-")
        if len(parts) < 5:
            bot.answer_callback_query(call.id)
            return
        
        # Формат: cal-{name}-{action}-{year}-{month}-{day} или cal-{name}-{ignore}-{year}-{month}-{day}
        # action может быть: PREV-YEAR, PREV-MONTH, NEXT-YEAR, NEXT-MONTH, IGNORE, или день (01-31)
        action = parts[2]
        
        # Если это навигация (не выбор дня), просто обновляем календарь
        if action in ['PREV-YEAR', 'PREV-MONTH', 'NEXT-YEAR', 'NEXT-MONTH', 'IGNORE']:
            # Для навигации используем calendar_query_handler
            year = int(parts[3])
            month = int(parts[4])
            day = int(parts[5]) if len(parts) > 5 else 1
            
            result = calendar.calendar_query_handler(bot, call, parts[1], action, year, month, day)
            if result:
                # Дата выбрана
                selected_date = result
                date_str = selected_date.strftime("%Y-%m-%d")
                
                # Проверяем, что дата не в прошлом
                from datetime import date
                if selected_date < date.today():
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
                # Календарь обновлен, ждем дальнейшего выбора
                bot.answer_callback_query(call.id)
            return
        
        # Если action - это день (01-31), то дата выбрана
        if action.isdigit() and 1 <= int(action) <= 31:
            year = int(parts[3])
            month = int(parts[4])
            day = int(action)
            
            from datetime import datetime
            selected_date = datetime(year, month, day)
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
        logger.info(f"🎫 Поиск билетов для пользователя {chat_id}: {user_data[chat_id]['from']} → {user_data[chat_id]['to']} | Дата: {user_data[chat_id]['date']} | Пассажиров: {num_pax}")
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
    
    send_detailed_train_info(chat_id, train, info['passengers'])

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
        active_jobs[chat_id] = {'thread': thread, 'stop_flag': False}
        
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
    bot.answer_callback_query(call.id, "🔁 Повторяю поиск...")
    
    try:
        # Разбираем callback_data: repeat_search_FROM_TO_DATE_PASSENGERS
        parts = call.data.replace("repeat_search_", "").split("_")
        if len(parts) >= 4:
            # Последние два элемента - дата и пассажиры
            passengers = parts[-1]
            date = parts[-2]
            # Всё остальное - станции (могут содержать подчеркивания)
            station_parts = parts[:-2]
            # Предполагаем, что первая половина - from, вторая - to
            mid = len(station_parts) // 2
            from_station = "_".join(station_parts[:mid])
            to_station = "_".join(station_parts[mid:])
            
            # Заменяем подчеркивания на пробелы в названиях станций
            from_station = from_station.replace("_", " ")
            to_station = to_station.replace("_", " ")
            
            logger.info(f"🔁 Повтор поиска: {from_station} → {to_station} | {date} | {passengers}")
            
            # Сохраняем данные и начинаем поиск
            user_data[chat_id] = {
                'from': from_station,
                'to': to_station,
                'date': date,
                'passengers': int(passengers)
            }
            
            loading_msg = bot.send_message(
                chat_id, 
                f"🔍 Ищу поезда по маршруту {from_station} → {to_station} на {date}..."
            )
            
            trains = get_trains_list(from_station, to_station, date, chat_id)
            bot.delete_message(chat_id, loading_msg.message_id)
            
            if not trains:
                bot.send_message(chat_id, "❌ Поездов не найдено. Попробуйте другую дату или маршрут.")
                return
            
            show_train_list(chat_id, trains)
        else:
            bot.send_message(chat_id, "❌ Ошибка при разборе данных поиска. Используйте /history заново.")
    except Exception as e:
        logger.error(f"Ошибка повтора поиска: {e}")
        bot.send_message(chat_id, f"❌ Ошибка: {e}. Попробуйте начать поиск через /track")

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
            KeyboardButton("❓ Помощь")
        )
    )

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
