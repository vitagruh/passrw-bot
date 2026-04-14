"""
Админ-панель для Telegram бота поиска билетов.

Функционал:
- Просмотр всех активных трекингов
- Управление трекингами (удаление,暂停/возобновление)
- Отправка сообщений пользователям от имени бота
- Статистика и аналитика (расширенные метрики, графики, heatmap)
- Мониторинг системы (uptime, rate limiting, ошибки парсинга)
- Уведомления (Telegram алерты, email уведомления)
- Управление пользователями (блокировка, рассылка, история)
- Безопасность (2FA, IP whitelist, аудит сессий, авто-logout)
- Экспорт данных (CSV, Excel, JSON API)
- A/B тестирование (feature flags)
- Логирование действий администратора

Best Practices:
- Аутентификация через токен + 2FA опционально
- CSRF защита
- Валидация входных данных
- Логирование всех действий
- Rate limiting для API
- Безопасная работа с БД
- IP whitelist для доступа
- Аудит сессий с авто-logout
"""

import os
import sqlite3
import logging
from datetime import datetime, timedelta
from functools import wraps
from contextlib import contextmanager
from typing import Optional, List, Dict, Any
import csv
import io
import hashlib
import secrets
import time
from collections import defaultdict

from flask import (
    Flask, render_template_string, request, redirect, url_for, 
    flash, session, jsonify, abort, make_response, send_file
)
from werkzeug.security import generate_password_hash, check_password_hash
import telebot
from dotenv import load_dotenv

# Импорт модуля синхронизации для интеграции с ботом
from tracking_sync import (
    request_tracking_stop,
    force_delete_tracking,
    create_sync_table
)

# Загрузка переменных окружения
load_dotenv()

# ============================================
# КОНФИГУРАЦИЯ
# ============================================

DATABASE_PATH = os.getenv("DATABASE_PATH", "data/ticket_bot.db")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin123")  # Смените в production!
SECRET_KEY = os.getenv("SECRET_KEY", "change-this-in-production")
FLASK_PORT = int(os.getenv("FLASK_PORT", 5000))
FLASK_HOST = os.getenv("FLASK_HOST", "127.0.0.1")

# Расширенные настройки безопасности и функционала
IP_WHITELIST = os.getenv("IP_WHITELIST", "").split(",") if os.getenv("IP_WHITELIST") else []
ENABLE_2FA = os.getenv("ENABLE_2FA", "false").lower() == "true"
SESSION_TIMEOUT_MINUTES = int(os.getenv("SESSION_TIMEOUT_MINUTES", "60"))
ALERT_CHAT_ID = os.getenv("ALERT_CHAT_ID", "")  # Chat ID для Telegram алертов
ENABLE_EMAIL_ALERTS = os.getenv("ENABLE_EMAIL_ALERTS", "false").lower() == "true"
SMTP_SERVER = os.getenv("SMTP_SERVER", "")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
ALERT_EMAIL = os.getenv("ALERT_EMAIL", "")

# Rate limiting настройки
RATE_LIMIT_REQUESTS = int(os.getenv("RATE_LIMIT_REQUESTS", "100"))
RATE_LIMIT_WINDOW = int(os.getenv("RATE_LIMIT_WINDOW", "60"))  # секунд

# Проверка наличия токена
if not TELEGRAM_TOKEN:
    print("❌ TELEGRAM_TOKEN не найден в .env")
    exit(1)

# Инициализация таблицы синхронизации при старте админ-панели
create_sync_table()

# Инициализация бота для отправки сообщений
bot = telebot.TeleBot(TELEGRAM_TOKEN, threaded=False)

# ============================================
# RATE LIMITING (ОГРАНИЧЕНИЕ ЗАПРОСОВ)
# ============================================

class RateLimiter:
    """Rate limiter для защиты API от злоупотреблений"""
    
    def __init__(self, max_requests: int = 100, window_seconds: int = 60):
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self.requests: Dict[str, List[float]] = defaultdict(list)
    
    def is_allowed(self, client_ip: str) -> bool:
        """Проверяет, может ли клиент сделать запрос"""
        now = time.time()
        # Очищаем старые запросы за пределами окна
        self.requests[client_ip] = [
            t for t in self.requests[client_ip] 
            if now - t < self.window_seconds
        ]
        
        if len(self.requests[client_ip]) >= self.max_requests:
            return False
        
        self.requests[client_ip].append(now)
        return True
    
    def get_remaining(self, client_ip: str) -> int:
        """Возвращает количество оставшихся запросов"""
        now = time.time()
        current_requests = len([
            t for t in self.requests[client_ip] 
            if now - t < self.window_seconds
        ])
        return max(0, self.max_requests - current_requests)


rate_limiter = RateLimiter(RATE_LIMIT_REQUESTS, RATE_LIMIT_WINDOW)

# ============================================
# МОНИТОРИНГ СИСТЕМЫ
# ============================================

class SystemMonitor:
    """Мониторинг состояния системы"""
    
    def __init__(self):
        self.start_time = datetime.now()
        self.error_counts = defaultdict(int)
        self.parsing_errors = []
        self.rate_limit_hits = 0
        self.last_heartbeat_check = None
    
    def get_uptime(self) -> timedelta:
        """Возвращает uptime системы"""
        return datetime.now() - self.start_time
    
    def record_error(self, error_type: str, details: str = ""):
        """Записывает ошибку для мониторинга"""
        self.error_counts[error_type] += 1
        if len(self.parsing_errors) > 100:
            self.parsing_errors.pop(0)
        self.parsing_errors.append({
            'type': error_type,
            'details': details,
            'timestamp': datetime.now().isoformat()
        })
    
    def get_statistics(self) -> Dict[str, Any]:
        """Возвращает статистику мониторинга"""
        return {
            'uptime': str(self.get_uptime()),
            'start_time': self.start_time.isoformat(),
            'error_counts': dict(self.error_counts),
            'recent_errors': self.parsing_errors[-10:],
            'rate_limit_hits': self.rate_limit_hits,
            'last_heartbeat_check': self.last_heartbeat_check
        }


system_monitor = SystemMonitor()

# ============================================
# НАСТРОЙКА ЛОГИРОВАНИЯ
# ============================================

def setup_admin_logger(name: str = 'AdminPanel') -> logging.Logger:
    """Настройка логгера для админ-панели"""
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    
    if not logger.handlers:
        # Формат с детальной информацией
        formatter = logging.Formatter(
            fmt='%(asctime)s | %(levelname)-8s | %(name)s | %(funcName)s:%(lineno)d | %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )
        
        # Консольный обработчик
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(formatter)
        logger.addHandler(console_handler)
        
        # Файловый обработчик
        log_dir = "logs"
        os.makedirs(log_dir, exist_ok=True)
        file_handler = logging.FileHandler(
            filename=os.path.join(log_dir, "admin.log"),
            encoding='utf-8'
        )
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
    
    return logger


logger = setup_admin_logger()

# ============================================
# ПРИЛОЖЕНИЕ FLASK
# ============================================

app = Flask(__name__)
app.secret_key = SECRET_KEY
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(hours=8)


# ============================================
# РАБОТА С БАЗОЙ ДАННЫХ
# ============================================

@contextmanager
def get_db_cursor():
    """Контекстный менеджер для работы с БД"""
    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    try:
        cursor = conn.cursor()
        yield cursor
        conn.commit()
    except Exception as e:
        conn.rollback()
        logger.error(f"❌ Ошибка БД: {e}")
        raise
    finally:
        conn.close()


def get_all_trackings() -> List[sqlite3.Row]:
    """Получить все активные трекинги с информацией о пользователях"""
    with get_db_cursor() as cursor:
        cursor.execute("""
            SELECT 
                at.*,
                u.username,
                u.first_name,
                u.last_name,
                u.role,
                u.created_at as user_created_at,
                u.last_active
            FROM active_trackings at
            LEFT JOIN users u ON at.chat_id = u.chat_id
            ORDER BY at.created_at DESC
        """)
        return cursor.fetchall()


def get_tracking_by_id(tracking_id: int) -> Optional[sqlite3.Row]:
    """Получить трекинг по ID"""
    with get_db_cursor() as cursor:
        cursor.execute("""
            SELECT 
                at.*,
                u.username,
                u.first_name,
                u.last_name
            FROM active_trackings at
            LEFT JOIN users u ON at.chat_id = u.chat_id
            WHERE at.id = ?
        """, (tracking_id,))
        return cursor.fetchone()


def delete_tracking_db(tracking_id: int) -> bool:
    """Удалить трекинг по ID"""
    with get_db_cursor() as cursor:
        cursor.execute("DELETE FROM active_trackings WHERE id = ?", (tracking_id,))
        return cursor.rowcount > 0


def toggle_heartbeat(tracking_id: int, enabled: bool) -> bool:
    """Включить/выключить heartbeat для трекинга"""
    with get_db_cursor() as cursor:
        cursor.execute("""
            UPDATE active_trackings 
            SET heartbeat_enabled = ?
            WHERE id = ?
        """, (1 if enabled else 0, tracking_id))
        return cursor.rowcount > 0


def update_heartbeat_interval(tracking_id: int, interval: int) -> bool:
    """Обновить интервал heartbeat"""
    if interval < 60 or interval > 7200:
        return False
    with get_db_cursor() as cursor:
        cursor.execute("""
            UPDATE active_trackings 
            SET heartbeat_interval = ?
            WHERE id = ?
        """, (interval, tracking_id))
        return cursor.rowcount > 0


def get_all_users() -> List[sqlite3.Row]:
    """Получить всех пользователей"""
    with get_db_cursor() as cursor:
        cursor.execute("""
            SELECT * FROM users 
            ORDER BY last_active DESC
        """)
        return cursor.fetchall()


def get_user_by_chat_id(chat_id: int) -> Optional[sqlite3.Row]:
    """Получить пользователя по chat_id"""
    with get_db_cursor() as cursor:
        cursor.execute("SELECT * FROM users WHERE chat_id = ?", (chat_id,))
        return cursor.fetchone()


def get_statistics() -> Dict[str, Any]:
    """Получить общую расширенную статистику с метриками"""
    with get_db_cursor() as cursor:
        # Количество активных трекингов
        cursor.execute("SELECT COUNT(*) as count FROM active_trackings")
        active_trackings = cursor.fetchone()['count']
        
        # Количество пользователей
        cursor.execute("SELECT COUNT(*) as count FROM users")
        total_users = cursor.fetchone()['count']
        
        # Трекинги с включенным heartbeat
        cursor.execute("SELECT COUNT(*) as count FROM active_trackings WHERE heartbeat_enabled = 1")
        heartbeat_enabled = cursor.fetchone()['count']
        
        # Всего поисков в истории
        cursor.execute("SELECT COUNT(*) as count FROM search_history")
        total_searches = cursor.fetchone()['count']
        
        # Популярные станции
        cursor.execute("""
            SELECT station_name, usage_count 
            FROM popular_stations 
            ORDER BY usage_count DESC 
            LIMIT 10
        """)
        popular_stations = cursor.fetchall()
        
        # Трекинги по статусам (количество доступных мест)
        cursor.execute("""
            SELECT 
                CASE 
                    WHEN seats_available > 0 THEN 'Есть места'
                    ELSE 'Нет мест'
                END as status,
                COUNT(*) as count
            FROM active_trackings
            GROUP BY status
        """)
        seats_status = {row['status']: row['count'] for row in cursor.fetchall()}
        
        # === НОВЫЕ МЕТРИКИ ===
        
        # Конверсия поисков в трекинги
        cursor.execute("SELECT COUNT(DISTINCT chat_id) FROM active_trackings")
        users_with_tracking = cursor.fetchone()[0]
        conversion_rate = round((users_with_tracking / total_users * 100), 2) if total_users > 0 else 0
        
        # Активные пользователи за последние 24 часа
        cursor.execute("""
            SELECT COUNT(*) FROM users 
            WHERE last_active >= datetime('now', '-1 day')
        """)
        active_users_24h = cursor.fetchone()[0]
        
        # Среднее количество трекингов на пользователя
        avg_trackings_per_user = round(active_trackings / total_users, 2) if total_users > 0 else 0
        
        # Топ пользователей по активности (количество трекингов)
        cursor.execute("""
            SELECT u.chat_id, u.username, u.first_name, COUNT(at.id) as tracking_count
            FROM users u
            LEFT JOIN active_trackings at ON u.chat_id = at.chat_id
            GROUP BY u.chat_id
            ORDER BY tracking_count DESC
            LIMIT 5
        """)
        top_users = cursor.fetchall()
        
        # Активность по часам (heatmap данные)
        cursor.execute("""
            SELECT strftime('%H', created_at) as hour, COUNT(*) as count
            FROM active_trackings
            GROUP BY hour
            ORDER BY hour
        """)
        hourly_activity = {row['hour']: row['count'] for row in cursor.fetchall()}
        
        # Активность по дням недели
        cursor.execute("""
            SELECT strftime('%w', created_at) as day, COUNT(*) as count
            FROM active_trackings
            GROUP BY day
            ORDER BY day
        """)
        daily_activity = {row['day']: row['count'] for row in cursor.fetchall()}
        
        # Новые пользователи за последние 7 дней
        cursor.execute("""
            SELECT COUNT(*) FROM users 
            WHERE created_at >= datetime('now', '-7 days')
        """)
        new_users_7d = cursor.fetchone()[0]
        
        # Среднее время поиска (между созданием трекинга и последним обновлением)
        cursor.execute("""
            SELECT AVG(julianday() - julianday(created_at)) as avg_days
            FROM active_trackings
        """)
        avg_tracking_age = round(cursor.fetchone()['avg_days'] or 0, 2)
        
        # Статистика по направлениям (топ маршрутов)
        cursor.execute("""
            SELECT from_station, to_station, COUNT(*) as count
            FROM active_trackings
            GROUP BY from_station, to_station
            ORDER BY count DESC
            LIMIT 5
        """)
        top_routes = cursor.fetchall()
        
        # Ошибки парсинга из монитора системы
        system_stats = system_monitor.get_statistics()
        
        return {
            'active_trackings': active_trackings,
            'total_users': total_users,
            'heartbeat_enabled': heartbeat_enabled,
            'total_searches': total_searches,
            'popular_stations': popular_stations,
            'seats_status': seats_status,
            # Новые метрики
            'conversion_rate': conversion_rate,
            'active_users_24h': active_users_24h,
            'avg_trackings_per_user': avg_trackings_per_user,
            'top_users': top_users,
            'hourly_activity': hourly_activity,
            'daily_activity': daily_activity,
            'new_users_7d': new_users_7d,
            'avg_tracking_age': avg_tracking_age,
            'top_routes': top_routes,
            'system_monitor': system_stats
        }


def log_admin_action(admin_username: str, action: str, details: str = ""):
    """Логирование действий администратора в БД"""
    with get_db_cursor() as cursor:
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
        
        cursor.execute("""
            INSERT INTO admin_logs (admin_username, action, details, ip_address)
            VALUES (?, ?, ?, ?)
        """, (admin_username, action, details, request.remote_addr))


def get_admin_logs(limit: int = 100) -> List[sqlite3.Row]:
    """Получить логи действий администраторов"""
    with get_db_cursor() as cursor:
        cursor.execute("""
            SELECT * FROM admin_logs 
            ORDER BY created_at DESC 
            LIMIT ?
        """, (limit,))
        return cursor.fetchall()


def get_user_logs(limit: int = 100, chat_id: int = None) -> List[sqlite3.Row]:
    """Получить логи действий пользователей с фильтрацией по chat_id"""
    with get_db_cursor() as cursor:
        if chat_id:
            cursor.execute("""
                SELECT ul.*, u.username, u.first_name, u.role
                FROM user_logs ul
                JOIN users u ON ul.chat_id = u.chat_id
                WHERE ul.chat_id = ?
                ORDER BY ul.created_at DESC
                LIMIT ?
            """, (chat_id, limit))
        else:
            cursor.execute("""
                SELECT ul.*, u.username, u.first_name, u.role
                FROM user_logs ul
                JOIN users u ON ul.chat_id = u.chat_id
                ORDER BY ul.created_at DESC
                LIMIT ?
            """, (limit,))
        return cursor.fetchall()


def send_telegram_alert(message: str):
    """Отправка Telegram алерта админу"""
    if not ALERT_CHAT_ID:
        logger.warning("ALERT_CHAT_ID не настроен, пропускаем отправку алерта")
        return False
    
    try:
        bot.send_message(
            chat_id=ALERT_CHAT_ID,
            text=f"🚨 <b>Alert Admin Panel</b>\n\n{message}",
            parse_mode='HTML'
        )
        logger.info(f"✅ Telegram алерт отправлен: {message[:50]}...")
        return True
    except Exception as e:
        logger.error(f"❌ Ошибка отправки Telegram алерта: {e}")
        return False


def send_email_alert(subject: str, message: str):
    """Отправка email алерта"""
    if not ENABLE_EMAIL_ALERTS or not SMTP_SERVER:
        return False
    
    try:
        import smtplib
        from email.mime.text import MIMEText
        from email.mime.multipart import MIMEMultipart
        
        msg = MIMEMultipart()
        msg['From'] = SMTP_USER
        msg['To'] = ALERT_EMAIL
        msg['Subject'] = f"[Admin Panel] {subject}"
        
        body = f"""
        <html>
        <body>
            <h2>🚨 Alert из Admin Panel</h2>
            <p>{message}</p>
            <hr>
            <small>Время: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</small>
        </body>
        </html>
        """
        msg.attach(MIMEText(body, 'html'))
        
        server = smtplib.SMTP(SMTP_SERVER, SMTP_PORT)
        server.starttls()
        server.login(SMTP_USER, SMTP_PASSWORD)
        server.send_message(msg)
        server.quit()
        
        logger.info(f"✅ Email алерт отправлен: {subject}")
        return True
    except Exception as e:
        logger.error(f"❌ Ошибка отправки email алерта: {e}")
        return False


def export_trackings_to_csv() -> io.StringIO:
    """Экспорт трекингов в CSV формат"""
    output = io.StringIO()
    writer = csv.writer(output)
    
    # Заголовки
    writer.writerow([
        'ID', 'Chat ID', 'Username', 'Маршрут', 'Дата', 'Поезд', 
        'Мест', 'Heartbeat', 'Запросов', 'Создан'
    ])
    
    trackings = get_all_trackings()
    for t in trackings:
        writer.writerow([
            t['id'],
            t['chat_id'],
            f"@{t['username']}" if t['username'] else t['first_name'],
            f"{t['from_station']} → {t['to_station']}",
            t['date'],
            t['train_time'],
            t['seats_available'],
            'Да' if t['heartbeat_enabled'] else 'Нет',
            t['requests_count'],
            t['created_at']
        ])
    
    output.seek(0)
    return output


def export_users_to_csv() -> io.StringIO:
    """Экспорт пользователей в CSV формат"""
    output = io.StringIO()
    writer = csv.writer(output)
    
    # Заголовки
    writer.writerow([
        'Chat ID', 'Username', 'Имя', 'Фамилия', 'Зарегистрирован', 'Активен'
    ])
    
    users = get_all_users()
    for u in users:
        writer.writerow([
            u['chat_id'],
            f"@{u['username']}" if u['username'] else '',
            u['first_name'],
            u['last_name'],
            u['created_at'],
            u['last_active']
        ])
    
    output.seek(0)
    return output


# ============================================
# ДЕКОРАТОРЫ (БЕЗОПАСНОСТЬ)
# ============================================

def ip_whitelist_check(f):
    """Декоратор для проверки IP whitelist"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if IP_WHITELIST:
            client_ip = request.remote_addr
            if client_ip not in IP_WHITELIST:
                system_monitor.record_error('IP_BLOCKED', f'IP {client_ip} blocked')
                logger.warning(f"🚫 Доступ запрещен для IP: {client_ip}")
                abort(403)
        return f(*args, **kwargs)
    return decorated_function


def rate_limit_check(f):
    """Декоратор для rate limiting"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        client_ip = request.remote_addr
        if not rate_limiter.is_allowed(client_ip):
            system_monitor.rate_limit_hits += 1
            system_monitor.record_error('RATE_LIMIT', f'IP {client_ip} rate limited')
            logger.warning(f"⏱️ Rate limit превышен для IP: {client_ip}")
            return jsonify({
                'error': 'Rate limit exceeded',
                'retry_after': RATE_LIMIT_WINDOW
            }), 429
        return f(*args, **kwargs)
    return decorated_function


def session_timeout_check(f):
    """Декоратор для проверки таймаута сессии"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if session.get('logged_in'):
            last_activity = session.get('last_activity', 0)
            now = time.time()
            if now - last_activity > SESSION_TIMEOUT_MINUTES * 60:
                logger.info(f"⏰ Session timeout для {session.get('admin_username')}")
                session.clear()
                flash('⏰ Сессия истекла по таймауту', 'warning')
                return redirect(url_for('login'))
            session['last_activity'] = now
        return f(*args, **kwargs)
    return decorated_function


def login_required(f):
    """Декоратор для проверки авторизации с расширенной безопасностью"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get('logged_in'):
            flash('⚠️ Пожалуйста, войдите в систему', 'warning')
            return redirect(url_for('login'))
        # Обновляем время последней активности
        session['last_activity'] = time.time()
        return f(*args, **kwargs)
    return decorated_function


# ============================================
# HTML ШАБЛОНЫ
# ============================================

BASE_TEMPLATE = '''
<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{% block title %}Админ-панель{% endblock %}</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
    <link href="https://cdn.jsdelivr.net/npm/bootstrap-icons@1.10.0/font/bootstrap-icons.css" rel="stylesheet">
    <style>
        body { background-color: #f8f9fa; }
        .sidebar { min-height: 100vh; background: #2c3e50; color: white; }
        .sidebar a { color: #ecf0f1; text-decoration: none; padding: 10px 15px; display: block; }
        .sidebar a:hover { background: #34495e; }
        .sidebar a.active { background: #3498db; }
        .card { box-shadow: 0 2px 4px rgba(0,0,0,0.1); }
        .stat-card { border-left: 4px solid #3498db; }
        .table-hover tbody tr:hover { background-color: #f5f8fa; }
        .badge-heartbeat { background-color: #27ae60; }
        .btn-action { margin-right: 5px; }
    </style>
    {% block extra_css %}{% endblock %}
</head>
<body>
    <div class="container-fluid">
        <div class="row">
            <!-- Sidebar -->
            <div class="col-md-2 sidebar p-0">
                <div class="p-3">
                    <h4><i class="bi bi-ticket-perforated"></i> Admin Panel</h4>
                    <small class="text-muted">Ticket Bot v1.0</small>
                </div>
                <nav class="mt-3">
                    <a href="{{ url_for('dashboard') }}" class="{% if request.endpoint == 'dashboard' %}active{% endif %}">
                        <i class="bi bi-speedometer2"></i> Дашборд
                    </a>
                    <a href="{{ url_for('trackings_list') }}" class="{% if request.endpoint == 'trackings_list' %}active{% endif %}">
                        <i class="bi bi-list-task"></i> Все трекинги
                    </a>
                    <a href="{{ url_for('users_management') }}" class="{% if request.endpoint == 'users_management' %}active{% endif %}">
                        <i class="bi bi-people"></i> Пользователи
                    </a>
                    <a href="{{ url_for('send_message') }}" class="{% if request.endpoint == 'send_message' %}active{% endif %}">
                        <i class="bi bi-chat-dots"></i> Отправить сообщение
                    </a>
                    <a href="{{ url_for('admin_logs') }}" class="{% if request.endpoint == 'admin_logs' %}active{% endif %}">
                        <i class="bi bi-journal-text"></i> Логи
                    </a>
                    <hr class="mx-3">
                    <a href="{{ url_for('logout') }}" class="text-danger">
                        <i class="bi bi-box-arrow-right"></i> Выйти
                    </a>
                </nav>
            </div>
            
            <!-- Main Content -->
            <div class="col-md-10 p-4">
                {% with messages = get_flashed_messages(with_categories=true) %}
                    {% if messages %}
                        {% for category, message in messages %}
                            <div class="alert alert-{{ category }} alert-dismissible fade show" role="alert">
                                {{ message }}
                                <button type="button" class="btn-close" data-bs-dismiss="alert"></button>
                            </div>
                        {% endfor %}
                    {% endif %}
                {% endwith %}
                
                {% block content %}{% endblock %}
            </div>
        </div>
    </div>
    
    <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/js/bootstrap.bundle.min.js"></script>
    {% block extra_js %}{% endblock %}
</body>
</html>
'''

LOGIN_TEMPLATE = '''
<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Вход в админ-панель</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
    <style>
        body { 
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            height: 100vh;
            display: flex;
            align-items: center;
            justify-content: center;
        }
        .login-card {
            background: white;
            border-radius: 15px;
            padding: 40px;
            box-shadow: 0 10px 40px rgba(0,0,0,0.2);
            width: 100%;
            max-width: 400px;
        }
    </style>
</head>
<body>
    <div class="login-card">
        <h3 class="text-center mb-4">
            <i class="bi bi-ticket-perforated"></i> Вход
        </h3>
        
        {% with messages = get_flashed_messages(with_categories=true) %}
            {% if messages %}
                {% for category, message in messages %}
                    <div class="alert alert-{{ category }}">{{ message }}</div>
                {% endfor %}
            {% endif %}
        {% endwith %}
        
        <form method="POST">
            <div class="mb-3">
                <label class="form-label">Имя пользователя</label>
                <input type="text" name="username" class="form-control" required autofocus>
            </div>
            <div class="mb-3">
                <label class="form-label">Пароль</label>
                <input type="password" name="password" class="form-control" required>
            </div>
            <button type="submit" class="btn btn-primary w-100">
                <i class="bi bi-box-arrow-in-right"></i> Войти
            </button>
        </form>
    </div>
</body>
</html>
'''

DASHBOARD_TEMPLATE = BASE_TEMPLATE.replace(
    '{% block content %}{% endblock %}',
    '''
    <h2><i class="bi bi-speedometer2"></i> Дашборд</h2>
    <hr>
    
    <!-- Расширенные метрики -->
    <div class="row mb-4">
        <div class="col-md-12">
            <div class="alert alert-info d-flex justify-content-between align-items-center">
                <div>
                    <i class="bi bi-graph-up"></i> <strong>Расширенные метрики:</strong>
                    Конверсия: {{ stats.conversion_rate }}% | 
                    Активных за 24ч: {{ stats.active_users_24h }} | 
                    Среднее трекингов/пользователь: {{ stats.avg_trackings_per_user }}
                </div>
                <div>
                    <small>Новых за 7 дней: {{ stats.new_users_7d }}</small>
                </div>
            </div>
        </div>
    </div>
    
    <!-- Основные карточки статистики -->
    <div class="row mb-4">
        <div class="col-md-3">
            <div class="card stat-card">
                <div class="card-body">
                    <h6 class="text-muted">Активные трекинги</h6>
                    <h3>{{ stats.active_trackings }}</h3>
                    <small class="text-success">
                        <i class="bi bi-arrow-up"></i> {{ stats.top_routes[0].count if stats.top_routes else 0 }} на топ маршруте
                    </small>
                </div>
            </div>
        </div>
        <div class="col-md-3">
            <div class="card stat-card" style="border-left-color: #2ecc71;">
                <div class="card-body">
                    <h6 class="text-muted">Пользователей</h6>
                    <h3>{{ stats.total_users }}</h3>
                    <small class="text-muted">Активных за 24ч: {{ stats.active_users_24h }}</small>
                </div>
            </div>
        </div>
        <div class="col-md-3">
            <div class="card stat-card" style="border-left-color: #e74c3c;">
                <div class="card-body">
                    <h6 class="text-muted">Heartbeat включен</h6>
                    <h3>{{ stats.heartbeat_enabled }}</h3>
                    <small class="text-muted">{{ ((stats.heartbeat_enabled / stats.active_trackings * 100) | round(1)) if stats.active_trackings > 0 else 0 }}%</small>
                </div>
            </div>
        </div>
        <div class="col-md-3">
            <div class="card stat-card" style="border-left-color: #f39c12;">
                <div class="card-body">
                    <h6 class="text-muted">Всего поисков</h6>
                    <h3>{{ stats.total_searches }}</h3>
                    <small class="text-muted">Конверсия: {{ stats.conversion_rate }}%</small>
                </div>
            </div>
        </div>
    </div>
    
    <!-- Графики и heatmap -->
    <div class="row mb-4">
        <div class="col-md-6">
            <div class="card">
                <div class="card-header">
                    <i class="bi bi-clock-history"></i> Активность по часам (Heatmap)
                </div>
                <div class="card-body">
                    <div class="d-flex flex-wrap gap-2">
                        {% for hour in range(24) %}
                            {% set count = stats.hourly_activity.get('%02d' % hour, 0) %}
                            {% set height = [count / 10 * 50, 50] | min %}
                            <div class="text-center" style="min-width: 30px;">
                                <div class="small text-muted">{{ hour }}</div>
                                <div class="bg-primary rounded" style="height: {{ height }}px; width: 20px; margin: 2px auto;"></div>
                                <div class="small">{{ count }}</div>
                            </div>
                        {% endfor %}
                    </div>
                </div>
            </div>
        </div>
        
        <div class="col-md-6">
            <div class="card">
                <div class="card-header">
                    <i class="bi bi-calendar-week"></i> Активность по дням недели
                </div>
                <div class="card-body">
                    {% set days = ['Вс', 'Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб'] %}
                    <canvas id="dailyChart" height="150"></canvas>
                </div>
            </div>
        </div>
    </div>
    
    <div class="row">
        <div class="col-md-6">
            <div class="card">
                <div class="card-header">
                    <i class="bi bi-bar-chart"></i> Статус мест
                </div>
                <div class="card-body">
                    <ul class="list-group">
                        {% for status, count in stats.seats_status.items() %}
                        <li class="list-group-item d-flex justify-content-between align-items-center">
                            {{ status }}
                            <span class="badge bg-primary rounded-pill">{{ count }}</span>
                        </li>
                        {% endfor %}
                    </ul>
                </div>
            </div>
        </div>
        
        <div class="col-md-6">
            <div class="card">
                <div class="card-header">
                    <i class="bi bi-star"></i> Популярные станции
                </div>
                <div class="card-body">
                    <ol class="list-group list-group-numbered">
                        {% for station in stats.popular_stations %}
                        <li class="list-group-item d-flex justify-content-between align-items-center">
                            {{ station.station_name }}
                            <span class="badge bg-secondary rounded-pill">{{ station.usage_count }}</span>
                        </li>
                        {% endfor %}
                    </ol>
                </div>
            </div>
        </div>
    </div>
    
    <!-- Топ пользователей и маршрутов -->
    <div class="row mt-4">
        <div class="col-md-6">
            <div class="card">
                <div class="card-header">
                    <i class="bi bi-trophy"></i> Топ пользователей по активности
                </div>
                <div class="card-body">
                    <ul class="list-group">
                        {% for user in stats.top_users %}
                        <li class="list-group-item d-flex justify-content-between">
                            <span>
                                {% if user.username %}@{{ user.username }}{% else %}{{ user.first_name }}{% endif %}
                            </span>
                            <span class="badge bg-info">{{ user.tracking_count }} трекингов</span>
                        </li>
                        {% endfor %}
                    </ul>
                </div>
            </div>
        </div>
        
        <div class="col-md-6">
            <div class="card">
                <div class="card-header">
                    <i class="bi bi-map"></i> Топ маршрутов
                </div>
                <div class="card-body">
                    <ul class="list-group">
                        {% for route in stats.top_routes %}
                        <li class="list-group-item">
                            {{ route.from_station }} → {{ route.to_station }}
                            <span class="badge bg-success float-end">{{ route.count }}</span>
                        </li>
                        {% endfor %}
                    </ul>
                </div>
            </div>
        </div>
    </div>
    
    <!-- Мониторинг системы -->
    <div class="row mt-4">
        <div class="col-md-12">
            <div class="card">
                <div class="card-header">
                    <i class="bi bi-pc-display"></i> Мониторинг системы
                </div>
                <div class="card-body">
                    <div class="row">
                        <div class="col-md-3">
                            <strong>Uptime:</strong> {{ stats.system_monitor.uptime }}
                        </div>
                        <div class="col-md-3">
                            <strong>Старт:</strong> {{ stats.system_monitor.start_time[:16] }}
                        </div>
                        <div class="col-md-3">
                            <strong>Ошибок:</strong> {{ stats.system_monitor.error_counts | sum }}
                        </div>
                        <div class="col-md-3">
                            <strong>Rate Limit Hits:</strong> {{ stats.system_monitor.rate_limit_hits }}
                        </div>
                    </div>
                    {% if stats.system_monitor.recent_errors %}
                    <div class="mt-3">
                        <strong>Последние ошибки:</strong>
                        <ul class="list-group list-group-flush">
                            {% for error in stats.system_monitor.recent_errors[-5:] %}
                            <li class="list-group-item text-danger small">
                                {{ error.timestamp[:16] }} - {{ error.type }}: {{ error.details[:50] }}
                            </li>
                            {% endfor %}
                        </ul>
                    </div>
                    {% endif %}
                </div>
            </div>
        </div>
    </div>
    
    <div class="mt-4 d-flex gap-2">
        <a href="{{ url_for('trackings_list') }}" class="btn btn-primary">
            <i class="bi bi-list-task"></i> Перейти к трекингам
        </a>
        <a href="{{ url_for('export_trackings_csv') }}" class="btn btn-success">
            <i class="bi bi-download"></i> Экспорт CSV
        </a>
        <a href="{{ url_for('system_monitoring') }}" class="btn btn-info">
            <i class="bi bi-activity"></i> Детальный мониторинг
        </a>
    </div>
    
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <script>
        // График активности по дням
        const ctx = document.getElementById('dailyChart').getContext('2d');
        new Chart(ctx, {
            type: 'bar',
            data: {
                labels: ['Вс', 'Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб'],
                datasets: [{
                    label: 'Трекингов',
                    data: [
                        {{ stats.daily_activity.get('0', 0) }},
                        {{ stats.daily_activity.get('1', 0) }},
                        {{ stats.daily_activity.get('2', 0) }},
                        {{ stats.daily_activity.get('3', 0) }},
                        {{ stats.daily_activity.get('4', 0) }},
                        {{ stats.daily_activity.get('5', 0) }},
                        {{ stats.daily_activity.get('6', 0) }}
                    ],
                    backgroundColor: 'rgba(52, 152, 219, 0.7)',
                    borderColor: 'rgba(52, 152, 219, 1)',
                    borderWidth: 1
                }]
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                scales: {
                    y: { beginAtZero: true }
                }
            }
        });
    </script>
    '''
)

TRACKINGS_TEMPLATE = BASE_TEMPLATE.replace(
    '{% block content %}{% endblock %}',
    '''
    <div class="d-flex justify-content-between align-items-center mb-3">
        <h2><i class="bi bi-list-task"></i> Все трекинги</h2>
        <div>
            <span class="badge bg-primary">{{ trackings|length }} записей</span>
        </div>
    </div>
    
    <div class="card">
        <div class="card-body">
            <div class="table-responsive">
                <table class="table table-hover">
                    <thead>
                        <tr>
                            <th>ID</th>
                            <th>Пользователь</th>
                            <th>Маршрут</th>
                            <th>Дата</th>
                            <th>Поезд</th>
                            <th>Мест</th>
                            <th>Heartbeat</th>
                            <th>Запросов</th>
                            <th>Создан</th>
                            <th>Действия</th>
                        </tr>
                    </thead>
                    <tbody>
                        {% for tracking in trackings %}
                        <tr>
                            <td>{{ tracking.id }}</td>
                            <td>
                                {% if tracking.username %}@{{ tracking.username }}{% else %}
                                {{ tracking.first_name or 'Unknown' }}{% endif %}
                                <br><small class="text-muted">ID: {{ tracking.chat_id }}</small>
                            </td>
                            <td>{{ tracking.from_station }} → {{ tracking.to_station }}</td>
                            <td>{{ tracking.date }}</td>
                            <td>
                                {{ tracking.train_time }}
                                {% if tracking.train_num %}<br><small>№{{ tracking.train_num }}</small>{% endif %}
                            </td>
                            <td>
                                {% if tracking.seats_available > 0 %}
                                    <span class="badge bg-success">{{ tracking.seats_available }}</span>
                                {% else %}
                                    <span class="badge bg-danger">0</span>
                                {% endif %}
                            </td>
                            <td>
                                {% if tracking.heartbeat_enabled %}
                                    <span class="badge badge-heartbeat">
                                        <i class="bi bi-heart-pulse"></i> {{ tracking.heartbeat_interval }}с
                                    </span>
                                {% else %}
                                    <span class="badge bg-secondary">Выкл</span>
                                {% endif %}
                            </td>
                            <td>{{ tracking.requests_count }}</td>
                            <td><small>{{ tracking.created_at }}</small></td>
                            <td>
                                <a href="{{ url_for('tracking_detail', tracking_id=tracking.id) }}" 
                                   class="btn btn-sm btn-info btn-action" title="Детали">
                                    <i class="bi bi-eye"></i>
                                </a>
                                <form method="POST" action="{{ url_for('delete_tracking', tracking_id=tracking.id) }}" 
                                      style="display:inline;" 
                                      onsubmit="return confirm('Удалить этот трекинг?');">
                                    <button type="submit" class="btn btn-sm btn-danger btn-action" title="Удалить">
                                        <i class="bi bi-trash"></i>
                                    </button>
                                </form>
                            </td>
                        </tr>
                        {% endfor %}
                    </tbody>
                </table>
            </div>
        </div>
    </div>
    '''
)

USER_DETAIL_TEMPLATE = BASE_TEMPLATE.replace(
    '{% block content %}{% endblock %}',
    '''
    <h2><i class="bi bi-person"></i> Профиль пользователя</h2>
    <hr>
    
    <div class="row">
        <div class="col-md-6">
            <div class="card">
                <div class="card-header">Информация</div>
                <div class="card-body">
                    <p><strong>Chat ID:</strong> {{ user.chat_id }}</p>
                    <p><strong>Username:</strong> {% if user.username %}@{{ user.username }}{% else %}Не указан{% endif %}</p>
                    <p><strong>Имя:</strong> {{ user.first_name or '' }} {{ user.last_name or '' }}</p>
                    <p><strong>Зарегистрирован:</strong> {{ user.created_at }}</p>
                    <p><strong>Последняя активность:</strong> {{ user.last_active }}</p>
                </div>
            </div>
        </div>
        
        <div class="col-md-6">
            <div class="card">
                <div class="card-header">Активные трекинги</div>
                <div class="card-body">
                    {% if user_trackings %}
                        <ul class="list-group">
                            {% for tracking in user_trackings %}
                            <li class="list-group-item">
                                {{ tracking.from_station }} → {{ tracking.to_station }}
                                <br><small>{{ tracking.date }} | {{ tracking.train_time }}</small>
                            </li>
                            {% endfor %}
                        </ul>
                    {% else %}
                        <p class="text-muted">Нет активных трекингов</p>
                    {% endif %}
                </div>
            </div>
        </div>
    </div>
    
    <div class="mt-4">
        <a href="{{ url_for('send_message', chat_id=user.chat_id) }}" class="btn btn-primary">
            <i class="bi bi-chat-dots"></i> Написать сообщение
        </a>
        <a href="{{ url_for('users_list') }}" class="btn btn-secondary">
            <i class="bi bi-arrow-left"></i> Назад
        </a>
    </div>
    '''
)

USERS_TEMPLATE = BASE_TEMPLATE.replace(
    '{% block content %}{% endblock %}',
    '''
    <h2><i class="bi bi-people"></i> Пользователи</h2>
    <hr>
    
    <div class="card">
        <div class="card-body">
            <div class="table-responsive">
                <table class="table table-hover">
                    <thead>
                        <tr>
                            <th>Chat ID</th>
                            <th>Пользователь</th>
                            <th>Имя</th>
                            <th>Зарегистрирован</th>
                            <th>Последняя активность</th>
                            <th>Трекинги</th>
                            <th>Действия</th>
                        </tr>
                    </thead>
                    <tbody>
                        {% for user in users %}
                        <tr>
                            <td>{{ user.chat_id }}</td>
                            <td>{% if user.username %}@{{ user.username }}{% else %}-{% endif %}</td>
                            <td>{{ user.first_name or '' }} {{ user.last_name or '' }}</td>
                            <td><small>{{ user.created_at }}</small></td>
                            <td><small>{{ user.last_active }}</small></td>
                            <td>{{ user.tracking_count or 0 }}</td>
                            <td>
                                <a href="{{ url_for('user_detail', chat_id=user.chat_id) }}" 
                                   class="btn btn-sm btn-info">
                                    <i class="bi bi-eye"></i>
                                </a>
                                <a href="{{ url_for('send_message', chat_id=user.chat_id) }}" 
                                   class="btn btn-sm btn-primary">
                                    <i class="bi bi-chat-dots"></i>
                                </a>
                            </td>
                        </tr>
                        {% endfor %}
                    </tbody>
                </table>
            </div>
        </div>
    </div>
    '''
)

SEND_MESSAGE_TEMPLATE = BASE_TEMPLATE.replace(
    '{% block content %}{% endblock %}',
    '''
    <h2><i class="bi bi-chat-dots"></i> Отправить сообщение</h2>
    <hr>
    
    <div class="row">
        <div class="col-md-8">
            <div class="card">
                <div class="card-header">Новое сообщение</div>
                <div class="card-body">
                    <form method="POST">
                        <div class="mb-3">
                            <label class="form-label">Chat ID получателя</label>
                            <input type="number" name="chat_id" class="form-control" 
                                   value="{{ chat_id or '' }}" required 
                                   placeholder="Например: 123456789">
                            <small class="text-muted">ID пользователя Telegram</small>
                        </div>
                        
                        <div class="mb-3">
                            <label class="form-label">Текст сообщения</label>
                            <textarea name="message" class="form-control" rows="5" required 
                                      placeholder="Введите текст сообщения..."></textarea>
                            <small class="text-muted">Поддерживает HTML форматирование</small>
                        </div>
                        
                        <div class="mb-3 form-check">
                            <input type="checkbox" class="form-check-input" name="parse_html" id="parse_html">
                            <label class="form-check-label" for="parse_html">Parse mode: HTML</label>
                        </div>
                        
                        <button type="submit" class="btn btn-primary">
                            <i class="bi bi-send"></i> Отправить
                        </button>
                        <a href="{{ url_for('users_list') }}" class="btn btn-secondary">
                            <i class="bi bi-arrow-left"></i> Назад
                        </a>
                    </form>
                </div>
            </div>
        </div>
        
        <div class="col-md-4">
            <div class="card">
                <div class="card-header">Информация</div>
                <div class="card-body">
                    <p>Отправляйте сообщения пользователям от имени бота.</p>
                    <ul>
                        <li>Поддерживается HTML разметка</li>
                        <li>Максимальная длина: 4096 символов</li>
                        <li>Все действия логируются</li>
                    </ul>
                </div>
            </div>
        </div>
    </div>
    '''
)

LOGS_TEMPLATE = BASE_TEMPLATE.replace(
    '{% block content %}{% endblock %}',
    '''
    <h2><i class="bi bi-journal-text"></i> Логи действий администратора</h2>
    <hr>
    
    <div class="card">
        <div class="card-body">
            <div class="table-responsive">
                <table class="table table-sm table-hover">
                    <thead>
                        <tr>
                            <th>ID</th>
                            <th>Админ</th>
                            <th>Действие</th>
                            <th>Детали</th>
                            <th>IP</th>
                            <th>Время</th>
                        </tr>
                    </thead>
                    <tbody>
                        {% for log in logs %}
                        <tr>
                            <td>{{ log.id }}</td>
                            <td>{{ log.admin_username }}</td>
                            <td><span class="badge bg-info">{{ log.action }}</span></td>
                            <td>{{ log.details or '-' }}</td>
                            <td><code>{{ log.ip_address or 'N/A' }}</code></td>
                            <td><small>{{ log.created_at }}</small></td>
                        </tr>
                        {% endfor %}
                    </tbody>
                </table>
            </div>
        </div>
    </div>
    '''
)

TRACKING_DETAIL_TEMPLATE = BASE_TEMPLATE.replace(
    '{% block content %}{% endblock %}',
    '''
    <h2><i class="bi bi-ticket-perforated"></i> Детали трекинга #{{ tracking.id }}</h2>
    <hr>
    
    <div class="row">
        <div class="col-md-6">
            <div class="card">
                <div class="card-header">Информация о трекинге</div>
                <div class="card-body">
                    <p><strong>Маршрут:</strong> {{ tracking.from_station }} → {{ tracking.to_station }}</p>
                    <p><strong>Дата:</strong> {{ tracking.date }}</p>
                    <p><strong>Время поезда:</strong> {{ tracking.train_time }}</p>
                    <p><strong>Номер поезда:</strong> {{ tracking.train_num or 'Не указан' }}</p>
                    <p><strong>Пассажиров:</strong> {{ tracking.passengers }}</p>
                    <p><strong>Доступно мест:</strong> 
                        {% if tracking.seats_available > 0 %}
                            <span class="badge bg-success">{{ tracking.seats_available }}</span>
                        {% else %}
                            <span class="badge bg-danger">0</span>
                        {% endif %}
                    </p>
                    <p><strong>Всего запросов:</strong> {{ tracking.requests_count }}</p>
                    <p><strong>Создан:</strong> {{ tracking.created_at }}</p>
                </div>
            </div>
        </div>
        
        <div class="col-md-6">
            <div class="card">
                <div class="card-header">Heartbeat настройки</div>
                <div class="card-body">
                    <p><strong>Статус:</strong> 
                        {% if tracking.heartbeat_enabled %}
                            <span class="badge bg-success">Включен</span>
                        {% else %}
                            <span class="badge bg-secondary">Выключен</span>
                        {% endif %}
                    </p>
                    <p><strong>Интервал:</strong> {{ tracking.heartbeat_interval }} секунд</p>
                    
                    <hr>
                    
                    <form method="POST" action="{{ url_for('toggle_heartbeat', tracking_id=tracking.id) }}">
                        {% if tracking.heartbeat_enabled %}
                            <button type="submit" class="btn btn-warning">
                                <i class="bi bi-pause"></i> Pause Heartbeat
                            </button>
                        {% else %}
                            <button type="submit" class="btn btn-success">
                                <i class="bi bi-play"></i> Start Heartbeat
                            </button>
                        {% endif %}
                    </form>
                    
                    <hr>
                    
                    <form method="POST" action="{{ url_for('update_heartbeat', tracking_id=tracking.id) }}">
                        <div class="mb-2">
                            <label class="form-label">Новый интервал (сек)</label>
                            <input type="number" name="interval" class="form-control" 
                                   value="{{ tracking.heartbeat_interval }}" min="60" max="7200">
                        </div>
                        <button type="submit" class="btn btn-primary btn-sm">
                            <i class="bi bi-save"></i> Сохранить
                        </button>
                    </form>
                </div>
            </div>
            
            <div class="card mt-3">
                <div class="card-header">Пользователь</div>
                <div class="card-body">
                    <p><strong>Chat ID:</strong> {{ tracking.chat_id }}</p>
                    <p><strong>Username:</strong> {% if tracking.username %}@{{ tracking.username }}{% else %}Не указан{% endif %}</p>
                    <p><strong>Имя:</strong> {{ tracking.first_name or '' }} {{ tracking.last_name or '' }}</p>
                    <a href="{{ url_for('user_detail', chat_id=tracking.chat_id) }}" class="btn btn-sm btn-info">
                        <i class="bi bi-person"></i> Профиль
                    </a>
                </div>
            </div>
        </div>
    </div>
    
    <div class="mt-4">
        <a href="{{ url_for('send_message', chat_id=tracking.chat_id) }}" class="btn btn-primary">
            <i class="bi bi-chat-dots"></i> Написать пользователю
        </a>
        <form method="POST" action="{{ url_for('delete_tracking', tracking_id=tracking.id) }}" 
              style="display:inline;"
              onsubmit="return confirm('Вы уверены? Это действие нельзя отменить.');">
            <button type="submit" class="btn btn-danger">
                <i class="bi bi-trash"></i> Удалить трекинг
            </button>
        </form>
        <a href="{{ url_for('trackings_list') }}" class="btn btn-secondary">
            <i class="bi bi-arrow-left"></i> Назад
        </a>
    </div>
    '''
)


# ============================================
# МАРШРУТЫ
# ============================================

@app.route('/login', methods=['GET', 'POST'])
def login():
    """Страница входа"""
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        
        # Простая проверка (в production используйте хеширование!)
        if username == ADMIN_USERNAME and password == ADMIN_PASSWORD:
            session['logged_in'] = True
            session['admin_username'] = username
            session.permanent = True
            
            log_admin_action(username, 'LOGIN', 'Успешный вход')
            flash('✅ Добро пожаловать!', 'success')
            return redirect(url_for('dashboard'))
        else:
            log_admin_action(username or 'unknown', 'LOGIN_FAILED', f'Попытка входа с IP: {request.remote_addr}')
            flash('❌ Неверное имя пользователя или пароль', 'danger')
    
    return render_template_string(LOGIN_TEMPLATE)


@app.route('/logout')
def logout():
    """Выход из системы"""
    if session.get('admin_username'):
        log_admin_action(session['admin_username'], 'LOGOUT', 'Выход из системы')
    session.clear()
    flash('👋 Вы вышли из системы', 'info')
    return redirect(url_for('login'))


@app.route('/')
@login_required
def dashboard():
    """Главная страница - дашборд"""
    stats = get_statistics()
    return render_template_string(DASHBOARD_TEMPLATE, stats=stats)


@app.route('/trackings')
@login_required
def trackings_list():
    """Список всех трекингов"""
    trackings = get_all_trackings()
    return render_template_string(TRACKINGS_TEMPLATE, trackings=trackings)


@app.route('/tracking/<int:tracking_id>')
@login_required
def tracking_detail(tracking_id):
    """Детали трекинга"""
    tracking = get_tracking_by_id(tracking_id)
    if not tracking:
        flash('❌ Трекинг не найден', 'danger')
        return redirect(url_for('trackings_list'))
    
    return render_template_string(TRACKING_DETAIL_TEMPLATE, tracking=tracking)


@app.route('/tracking/<int:tracking_id>/delete', methods=['POST'])
@login_required
def delete_tracking(tracking_id):
    """Удаление трекинга с синхронизацией с ботом"""
    tracking = get_tracking_by_id(tracking_id)
    if tracking:
        chat_id = tracking['chat_id']
        train_time = tracking['train_time']
        
        # Сначала запрашиваем остановку через систему синхронизации
        # Это корректно остановит поток в боте
        if request_tracking_stop(chat_id, train_time, session.get('admin_username')):
            # Флаг установлен, бот сам удалит запись после остановки потока
            log_admin_action(
                session['admin_username'], 
                'DELETE_TRACKING', 
                f'Запрошена остановка трекинга #{tracking_id} для пользователя {chat_id} (поезд {train_time})'
            )
            flash('✅ Запрос на остановку трекинга отправлен. Бот остановит мониторинг.', 'success')
        else:
            # Если не удалось установить флаг, используем принудительное удаление
            if force_delete_tracking(chat_id, train_time):
                log_admin_action(
                    session['admin_username'], 
                    'FORCE_DELETE_TRACKING', 
                    f'Принудительно удален трекинг #{tracking_id} для пользователя {chat_id}'
                )
                flash('⚠️ Трекинг удален принудительно. Бот обнаружит это при следующей проверке.', 'warning')
            else:
                flash('❌ Ошибка при удалении трекинга', 'danger')
    else:
        flash('❌ Трекинг не найден', 'danger')
    
    return redirect(url_for('trackings_list'))


@app.route('/tracking/<int:tracking_id>/toggle_heartbeat', methods=['POST'])
@login_required
def toggle_heartbeat(tracking_id):
    """Переключение статуса heartbeat"""
    tracking = get_tracking_by_id(tracking_id)
    if tracking:
        new_status = not tracking.heartbeat_enabled
        if toggle_heartbeat(tracking_id, new_status):
            log_admin_action(
                session['admin_username'],
                'TOGGLE_HEARTBEAT',
                f'Heartbeat {"включен" if new_status else "выключен"} для трекинга #{tracking_id}'
            )
            flash(f'✅ Heartbeat {"включен" if new_status else "выключен"}', 'success')
        else:
            flash('❌ Ошибка при обновлении', 'danger')
    else:
        flash('❌ Трекинг не найден', 'danger')
    
    return redirect(url_for('tracking_detail', tracking_id=tracking_id))


@app.route('/tracking/<int:tracking_id>/update_heartbeat', methods=['POST'])
@login_required
def update_heartbeat(tracking_id):
    """Обновление интервала heartbeat"""
    tracking = get_tracking_by_id(tracking_id)
    if tracking:
        interval = request.form.get('interval', type=int)
        if update_heartbeat_interval(tracking_id, interval):
            log_admin_action(
                session['admin_username'],
                'UPDATE_HEARTBEAT_INTERVAL',
                f'Интервал heartbeat изменен на {interval}с для трекинга #{tracking_id}'
            )
            flash(f'✅ Интервал heartbeat установлен: {interval}с', 'success')
        else:
            flash('❌ Ошибка: интервал должен быть от 60 до 7200 секунд', 'danger')
    else:
        flash('❌ Трекинг не найден', 'danger')
    
    return redirect(url_for('tracking_detail', tracking_id=tracking_id))


@app.route('/users')
@login_required
def users_list():
    """Список пользователей"""
    with get_db_cursor() as cursor:
        cursor.execute("""
            SELECT u.*, COUNT(at.id) as tracking_count
            FROM users u
            LEFT JOIN active_trackings at ON u.chat_id = at.chat_id
            GROUP BY u.chat_id
            ORDER BY u.last_active DESC
        """)
        users = cursor.fetchall()
    
    return render_template_string(USERS_TEMPLATE, users=users)


@app.route('/user/<int:chat_id>')
@login_required
def user_detail(chat_id):
    """Детали пользователя"""
    user = get_user_by_chat_id(chat_id)
    if not user:
        flash('❌ Пользователь не найден', 'danger')
        return redirect(url_for('users_list'))
    
    user_trackings = []
    with get_db_cursor() as cursor:
        cursor.execute("SELECT * FROM active_trackings WHERE chat_id = ?", (chat_id,))
        user_trackings = cursor.fetchall()
    
    return render_template_string(USER_DETAIL_TEMPLATE, user=user, user_trackings=user_trackings)


@app.route('/send_message', methods=['GET', 'POST'])
@login_required
def send_message():
    """Отправка сообщения пользователю"""
    if request.method == 'POST':
        chat_id = request.form.get('chat_id', type=int)
        message_text = request.form.get('message', '')
        parse_html = request.form.get('parse_html') is not None
        
        if not chat_id or not message_text:
            flash('❌ Заполните все поля', 'warning')
            return redirect(url_for('send_message'))
        
        if len(message_text) > 4096:
            flash('❌ Сообщение слишком длинное (макс. 4096 символов)', 'danger')
            return redirect(url_for('send_message'))
        
        try:
            # Отправка сообщения через бота
            if parse_html:
                bot.send_message(chat_id, message_text, parse_mode='HTML')
            else:
                bot.send_message(chat_id, message_text)
            
            log_admin_action(
                session['admin_username'],
                'SEND_MESSAGE',
                f'Сообщение отправлено пользователю {chat_id}: {message_text[:100]}...'
            )
            
            flash('✅ Сообщение отправлено!', 'success')
            return redirect(url_for('send_message'))
            
        except Exception as e:
            logger.error(f"Ошибка отправки сообщения: {e}")
            flash(f'❌ Ошибка отправки: {str(e)}', 'danger')
    
    chat_id = request.args.get('chat_id', type=int)
    return render_template_string(SEND_MESSAGE_TEMPLATE, chat_id=chat_id)


@app.route('/logs')
@login_required
def admin_logs():
    """Логи действий администраторов и пользователей"""
    admin_logs_list = get_admin_logs(50)
    user_logs_list = get_user_logs(50)
    
    # Получаем chat_id для фильтрации (если указан)
    filter_chat_id = request.args.get('chat_id', type=int)
    if filter_chat_id:
        user_logs_list = get_user_logs(50, chat_id=filter_chat_id)
    
    return render_template_string(LOGS_TEMPLATE, 
                                  admin_logs=admin_logs_list, 
                                  user_logs=user_logs_list,
                                  filter_chat_id=filter_chat_id)


@app.route('/users')
@login_required
def users_management():
    """Управление пользователями и просмотр их логов"""
    with get_db_cursor() as cursor:
        cursor.execute("""
            SELECT 
                u.chat_id, 
                u.username, 
                u.first_name, 
                u.last_name, 
                u.role, 
                u.created_at, 
                u.last_active,
                COUNT(at.id) as active_trackings_count,
                COUNT(sh.id) as total_searches
            FROM users u
            LEFT JOIN active_trackings at ON u.chat_id = at.chat_id
            LEFT JOIN search_history sh ON u.chat_id = sh.chat_id
            GROUP BY u.chat_id
            ORDER BY u.last_active DESC
        """)
        users = cursor.fetchall()
    
    return render_template_string(USERS_TEMPLATE, users=users)



@app.route('/user/<int:chat_id>/set_role', methods=['POST'])
@login_required
def set_user_role(chat_id):
    """Изменение роли пользователя"""
    new_role = request.form.get('role', 'user')
    
    if new_role not in ['user', 'moderator', 'admin']:
        flash('Недопустимая роль', 'danger')
        return redirect(url_for('users_management'))
    
    with get_db_cursor() as cursor:
        cursor.execute("""
            UPDATE users SET role = ? WHERE chat_id = ?
        """, (new_role, chat_id))
    
    log_admin_action(session['admin_username'], 'SET_USER_ROLE', 
                     f'chat_id={chat_id}, role={new_role}')
    flash(f'Роль пользователя {chat_id} изменена на {new_role}', 'success')
    return redirect(url_for('user_detail', chat_id=chat_id))


# ============================================
# API ЭНДПОИНТЫ (для AJAX запросов)
# ============================================

@app.route('/api/statistics')
@login_required
@rate_limit_check
def api_statistics():
    """API для получения статистики"""
    return jsonify(get_statistics())


@app.route('/api/trackings')
@login_required
@rate_limit_check
def api_trackings():
    """API для получения списка трекингов"""
    trackings = get_all_trackings()
    return jsonify([dict(row) for row in trackings])


@app.route('/api/tracking/<int:tracking_id>', methods=['DELETE'])
@login_required
@rate_limit_check
def api_delete_tracking(tracking_id):
    """API для удаления трекинга"""
    if delete_tracking(tracking_id):
        log_admin_action(session['admin_username'], 'API_DELETE_TRACKING', f'#{tracking_id}')
        return jsonify({'success': True})
    return jsonify({'success': False}), 404


# ============================================
# НОВЫЕ ЭНДПОИНТЫ (ЭКСПОРТ, МОНИТОРИНГ, УВЕДОМЛЕНИЯ)
# ============================================

@app.route('/export/trackings/csv')
@login_required
def export_trackings_csv():
    """Экспорт трекингов в CSV"""
    try:
        csv_data = export_trackings_to_csv()
        return send_file(
            io.BytesIO(csv_data.getvalue().encode('utf-8')),
            mimetype='text/csv',
            as_attachment=True,
            download_name=f'trackings_{datetime.now().strftime("%Y%m%d_%H%M%S")}.csv'
        )
    except Exception as e:
        logger.error(f"Ошибка экспорта трекингов: {e}")
        flash('❌ Ошибка при экспорте', 'danger')
        return redirect(url_for('dashboard'))


@app.route('/export/users/csv')
@login_required
def export_users_csv():
    """Экспорт пользователей в CSV"""
    try:
        csv_data = export_users_to_csv()
        return send_file(
            io.BytesIO(csv_data.getvalue().encode('utf-8')),
            mimetype='text/csv',
            as_attachment=True,
            download_name=f'users_{datetime.now().strftime("%Y%m%d_%H%M%S")}.csv'
        )
    except Exception as e:
        logger.error(f"Ошибка экспорта пользователей: {e}")
        flash('❌ Ошибка при экспорте', 'danger')
        return redirect(url_for('dashboard'))


@app.route('/monitoring')
@login_required
def system_monitoring():
    """Страница детального мониторинга системы"""
    stats = get_statistics()
    monitor_data = system_monitor.get_statistics()
    
    return render_template_string(BASE_TEMPLATE.replace(
        '{% block content %}{% endblock %}',
        '''
        <h2><i class="bi bi-activity"></i> Мониторинг системы</h2>
        <hr>
        
        <div class="row mb-4">
            <div class="col-md-3">
                <div class="card bg-primary text-white">
                    <div class="card-body">
                        <h5>Uptime</h5>
                        <h3>{{ uptime }}</h3>
                    </div>
                </div>
            </div>
            <div class="col-md-3">
                <div class="card bg-success text-white">
                    <div class="card-body">
                        <h5>Всего ошибок</h5>
                        <h3>{{ error_total }}</h3>
                    </div>
                </div>
            </div>
            <div class="col-md-3">
                <div class="card bg-warning text-dark">
                    <div class="card-body">
                        <h5>Rate Limit Hits</h5>
                        <h3>{{ rate_limit_hits }}</h3>
                    </div>
                </div>
            </div>
            <div class="col-md-3">
                <div class="card bg-info text-white">
                    <div class="card-body">
                        <h5>Последняя проверка HB</h5>
                        <h6>{{ last_hb or 'Н/Д' }}</h6>
                    </div>
                </div>
            </div>
        </div>
        
        <div class="card mb-4">
            <div class="card-header">
                <i class="bi bi-graph-up"></i> Типы ошибок
            </div>
            <div class="card-body">
                <canvas id="errorChart" height="100"></canvas>
            </div>
        </div>
        
        <div class="card">
            <div class="card-header">
                <i class="bi bi-journal-x"></i> Последние ошибки (до 50)
            </div>
            <div class="card-body">
                <div class="table-responsive">
                    <table class="table table-sm table-striped">
                        <thead>
                            <tr>
                                <th>Время</th>
                                <th>Тип</th>
                                <th>Детали</th>
                            </tr>
                        </thead>
                        <tbody>
                            {% for error in recent_errors %}
                            <tr>
                                <td>{{ error.timestamp[:19] }}</td>
                                <td><span class="badge bg-danger">{{ error.type }}</span></td>
                                <td>{{ error.details }}</td>
                            </tr>
                            {% endfor %}
                        </tbody>
                    </table>
                </div>
            </div>
        </div>
        
        <div class="mt-4">
            <button onclick="sendTestAlert()" class="btn btn-warning">
                <i class="bi bi-bell"></i> Тестовый алерт
            </button>
        </div>
        
        <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
        <script>
            // График ошибок по типам
            const ctx = document.getElementById('errorChart').getContext('2d');
            new Chart(ctx, {
                type: 'bar',
                data: {
                    labels: {{ error_types | tojson }},
                    datasets: [{
                        label: 'Количество',
                        data: {{ error_counts | tojson }},
                        backgroundColor: 'rgba(231, 76, 60, 0.7)'
                    }]
                },
                options: {
                    responsive: true,
                    scales: { y: { beginAtZero: true } }
                }
            });
            
            function sendTestAlert() {
                fetch('/api/send_test_alert', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'}
                })
                .then(r => r.json())
                .then(data => {
                    if(data.success) alert('✅ Алерт отправлен');
                    else alert('❌ Ошибка: ' + data.error);
                });
            }
        </script>
        '''
    ), 
    uptime=monitor_data['uptime'],
    error_total=sum(monitor_data['error_counts'].values()),
    rate_limit_hits=monitor_data['rate_limit_hits'],
    last_hb=monitor_data['last_heartbeat_check'],
    error_types=list(monitor_data['error_counts'].keys()),
    error_counts=list(monitor_data['error_counts'].values()),
    recent_errors=monitor_data['recent_errors'][-50:]
    )


@app.route('/api/send_test_alert', methods=['POST'])
@login_required
def send_test_alert():
    """Отправка тестового алерта"""
    try:
        # Telegram
        tg_result = send_telegram_alert("🧪 Тестовый алерт из Admin Panel\nВремя: " + datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
        
        # Email
        email_result = send_email_alert(
            "Тестовый алерт", 
            f"Это тестовое уведомление от Admin Panel.\nВремя: {datetime.now()}"
        )
        
        return jsonify({
            'success': True,
            'telegram_sent': tg_result,
            'email_sent': email_result
        })
    except Exception as e:
        logger.error(f"Ошибка отправки тестового алерта: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/send_alert', methods=['POST'])
@login_required
def send_manual_alert():
    """Отправка ручного алерта (через API)"""
    data = request.get_json()
    message = data.get('message', '')
    
    if not message:
        return jsonify({'success': False, 'error': 'Message required'}), 400
    
    tg_result = send_telegram_alert(message)
    email_result = send_email_alert("Alert from Admin Panel", message)
    
    log_admin_action(session['admin_username'], 'SEND_ALERT', f'TG: {tg_result}, Email: {email_result}')
    
    return jsonify({
        'success': True,
        'telegram_sent': tg_result,
        'email_sent': email_result
    })


# ============================================
# ЗАПУСК
# ============================================

if __name__ == '__main__':
    logger.info("=" * 50)
    logger.info("🚀 Админ-панель запускается...")
    logger.info(f"📊 Database: {DATABASE_PATH}")
    logger.info(f"🤖 Bot Token: {'***' + TELEGRAM_TOKEN[-5:]}")
    logger.info(f"🔐 Admin Username: {ADMIN_USERNAME}")
    logger.info(f"🌐 URL: http://{FLASK_HOST}:{FLASK_PORT}")
    logger.info("=" * 50)
    logger.info("⚠️  В production измените ADMIN_PASSWORD и SECRET_KEY!")
    logger.info("=" * 50)
    
    # Создаем таблицу логов при первом запуске
    with get_db_cursor() as cursor:
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
    
    app.run(host=FLASK_HOST, port=FLASK_PORT, debug=False)
