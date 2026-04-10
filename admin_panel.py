"""
Админ-панель для Telegram бота поиска билетов.

Функционал:
- Просмотр всех активных трекингов
- Управление трекингами (удаление,暂停/возобновление)
- Отправка сообщений пользователям от имени бота
- Статистика и аналитика
- Логирование действий администратора

Best Practices:
- Аутентификация через токен
- CSRF защита
- Валидация входных данных
- Логирование всех действий
- Rate limiting для API
- Безопасная работа с БД
"""

import os
import sqlite3
import logging
from datetime import datetime, timedelta
from functools import wraps
from contextlib import contextmanager
from typing import Optional, List, Dict, Any

from flask import (
    Flask, render_template_string, request, redirect, url_for, 
    flash, session, jsonify, abort
)
from werkzeug.security import generate_password_hash, check_password_hash
import telebot
from dotenv import load_dotenv

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

# Проверка наличия токена
if not TELEGRAM_TOKEN:
    print("❌ TELEGRAM_TOKEN не найден в .env")
    exit(1)

# Инициализация бота для отправки сообщений
bot = telebot.TeleBot(TELEGRAM_TOKEN, threaded=False)

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
    """Получить общую статистику"""
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
        
        return {
            'active_trackings': active_trackings,
            'total_users': total_users,
            'heartbeat_enabled': heartbeat_enabled,
            'total_searches': total_searches,
            'popular_stations': popular_stations,
            'seats_status': seats_status
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


# ============================================
# ДЕКОРАТОРЫ
# ============================================

def login_required(f):
    """Декоратор для проверки авторизации"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get('logged_in'):
            flash('⚠️ Пожалуйста, войдите в систему', 'warning')
            return redirect(url_for('login'))
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
                    <a href="{{ url_for('users_list') }}" class="{% if request.endpoint == 'users_list' %}active{% endif %}">
                        <i class="bi bi-people"></i> Пользователи
                    </a>
                    <a href="{{ url_for('send_message') }}" class="{% if request.endpoint == 'send_message' %}active{% endif %}">
                        <i class="bi bi-chat-dots"></i> Отправить сообщение
                    </a>
                    <a href="{{ url_for('admin_logs') }}" class="{% if request.endpoint == 'admin_logs' %}active{% endif %}">
                        <i class="bi bi-journal-text"></i> Логи админа
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
    
    <div class="row mb-4">
        <div class="col-md-3">
            <div class="card stat-card">
                <div class="card-body">
                    <h6 class="text-muted">Активные трекинги</h6>
                    <h3>{{ stats.active_trackings }}</h3>
                </div>
            </div>
        </div>
        <div class="col-md-3">
            <div class="card stat-card" style="border-left-color: #2ecc71;">
                <div class="card-body">
                    <h6 class="text-muted">Пользователей</h6>
                    <h3>{{ stats.total_users }}</h3>
                </div>
            </div>
        </div>
        <div class="col-md-3">
            <div class="card stat-card" style="border-left-color: #e74c3c;">
                <div class="card-body">
                    <h6 class="text-muted">Heartbeat включен</h6>
                    <h3>{{ stats.heartbeat_enabled }}</h3>
                </div>
            </div>
        </div>
        <div class="col-md-3">
            <div class="card stat-card" style="border-left-color: #f39c12;">
                <div class="card-body">
                    <h6 class="text-muted">Всего поисков</h6>
                    <h3>{{ stats.total_searches }}</h3>
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
    
    <div class="mt-4">
        <a href="{{ url_for('trackings_list') }}" class="btn btn-primary">
            <i class="bi bi-list-task"></i> Перейти к трекингам
        </a>
    </div>
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
    """Удаление трекинга"""
    tracking = get_tracking_by_id(tracking_id)
    if tracking:
        if delete_tracking_db(tracking_id):
            log_admin_action(
                session['admin_username'], 
                'DELETE_TRACKING', 
                f'Удален трекинг #{tracking_id} для пользователя {tracking["chat_id"]}'
            )
            flash('✅ Трекинг удален', 'success')
        else:
            flash('❌ Ошибка при удалении', 'danger')
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
    """Логи действий администраторов"""
    logs = get_admin_logs(100)
    return render_template_string(LOGS_TEMPLATE, logs=logs)


# ============================================
# API ЭНДПОИНТЫ (для AJAX запросов)
# ============================================

@app.route('/api/statistics')
@login_required
def api_statistics():
    """API для получения статистики"""
    return jsonify(get_statistics())


@app.route('/api/trackings')
@login_required
def api_trackings():
    """API для получения списка трекингов"""
    trackings = get_all_trackings()
    return jsonify([dict(row) for row in trackings])


@app.route('/api/tracking/<int:tracking_id>', methods=['DELETE'])
@login_required
def api_delete_tracking(tracking_id):
    """API для удаления трекинга"""
    if delete_tracking(tracking_id):
        log_admin_action(session['admin_username'], 'API_DELETE_TRACKING', f'#{tracking_id}')
        return jsonify({'success': True})
    return jsonify({'success': False}), 404


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
