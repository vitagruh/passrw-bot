"""
Модуль синхронизации между админ-панелью и ботом.

Обеспечивает надежную синхронизацию состояний трекингов между:
- Админ-панелью (Flask, отдельный процесс)
- Ticket-bot (Telebot, основной процесс)

Архитектурные решения:
1. SQLite база данных как единый источник истины (single source of truth)
2. Файлы-флаги в директории /tmp для межпроцессной коммуникации (IPC)
3. Проверка актуальности состояния в каждом цикле трекинга
4. Атомарные операции с использованием транзакций

Best Practices:
- ACID транзакции для консистентности данных
- Graceful shutdown для потоков
- Idempotency операций удаления
- Логирование всех действий синхронизации
"""

import os
import json
import time
import logging
import sqlite3
from pathlib import Path
from typing import Optional, Dict, List, Any
from contextlib import contextmanager
from datetime import datetime

# Настройка логгера
logger = logging.getLogger('TrackingSync')
logger.setLevel(logging.INFO)

# Пути
SYNC_DIR = Path('/tmp/ticket_bot_sync')
DATABASE_PATH = os.getenv("DATABASE_PATH", "data/ticket_bot.db")

# Инициализация директории синхронизации
SYNC_DIR.mkdir(parents=True, exist_ok=True)


@contextmanager
def get_db_cursor():
    """Контекстный менеджер для работы с базой данных"""
    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
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


def create_sync_table():
    """
    Создает таблицу для хранения состояний синхронизации.
    Вызывается при старте бота и админ-панели.
    """
    with get_db_cursor() as cursor:
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS sync_flags (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                train_time TEXT NOT NULL,
                action TEXT NOT NULL,  -- 'STOP', 'UPDATE', 'RESUME'
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                processed BOOLEAN DEFAULT 0,
                processed_at TIMESTAMP,
                UNIQUE(chat_id, train_time, action, created_at)
            )
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_sync_chat 
            ON sync_flags(chat_id, processed)
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_sync_pending 
            ON sync_flags(processed, created_at)
        """)
    logger.info("✅ Таблица синхронизации создана")


def request_tracking_stop(chat_id: int, train_time: str, admin_username: str = None) -> bool:
    """
    Запрашивает остановку трекинга.
    
    Архитектурное решение:
    - Вместо прямого удаления из БД сначала устанавливаем флаг остановки
    - Бот проверяет флаги в каждом цикле и корректно завершает поток
    - Только после подтверждения от бота запись удаляется из БД
    
    :param chat_id: ID чата пользователя
    :param train_time: Время отправления поезда (ключ трекинга)
    :param admin_username: Имя администратора (для аудита)
    :return: True если запрос успешно создан
    """
    try:
        with get_db_cursor() as cursor:
            # Проверяем существует ли трекинг
            cursor.execute("""
                SELECT id FROM active_trackings 
                WHERE chat_id = ? AND train_time = ?
            """, (chat_id, train_time))
            
            if not cursor.fetchone():
                logger.warning(f"Трекинг {chat_id}:{train_time} не найден в БД")
                return False
            
            # Создаем флаг остановки
            cursor.execute("""
                INSERT INTO sync_flags (chat_id, train_time, action, created_at)
                VALUES (?, ?, 'STOP', CURRENT_TIMESTAMP)
            """, (chat_id, train_time))
            
            # Создаем файл-уведомление для мгновенной реакции бота
            flag_file = SYNC_DIR / f"stop_{chat_id}_{train_time.replace(':', '-')}.flag"
            flag_file.write_text(json.dumps({
                'chat_id': chat_id,
                'train_time': train_time,
                'action': 'STOP',
                'admin': admin_username,
                'timestamp': datetime.now().isoformat()
            }))
            
            logger.info(
                f"🛑 Запрошена остановка трекинга: chat_id={chat_id}, "
                f"train_time={train_time}, admin={admin_username}"
            )
            return True
            
    except Exception as e:
        logger.error(f"Ошибка при запросе остановки: {e}", exc_info=True)
        return False


def confirm_tracking_stopped(chat_id: int, train_time: str) -> bool:
    """
    Подтверждает остановку трекинга после завершения потока.
    
    :param chat_id: ID чата пользователя
    :param train_time: Время отправления поезда
    :return: True если подтверждение успешно записано
    """
    try:
        with get_db_cursor() as cursor:
            # Отмечаем все флаги STOP как обработанные
            cursor.execute("""
                UPDATE sync_flags 
                SET processed = 1, processed_at = CURRENT_TIMESTAMP
                WHERE chat_id = ? AND train_time = ? AND action = 'STOP' AND processed = 0
            """, (chat_id, train_time))
            
            # Удаляем файл-флаг
            flag_files = list(SYNC_DIR.glob(f"stop_{chat_id}_{train_time.replace(':', '-')}*.flag"))
            for flag_file in flag_files:
                try:
                    flag_file.unlink()
                except Exception as e:
                    logger.warning(f"Не удалось удалить флаг {flag_file}: {e}")
            
            # Теперь удаляем сам трекинг из БД
            cursor.execute("""
                DELETE FROM active_trackings 
                WHERE chat_id = ? AND train_time = ?
            """, (chat_id, train_time))
            
            logger.info(f"✅ Трекинг {chat_id}:{train_time} остановлен и удален из БД")
            return True
            
    except Exception as e:
        logger.error(f"Ошибка при подтверждении остановки: {e}", exc_info=True)
        return False


def check_stop_request(chat_id: int, train_time: str) -> bool:
    """
    Проверяет наличие запроса на остановку трекинга.
    
    Вызывается в каждом цикле tracking_worker для проверки необходимости остановки.
    
    :param chat_id: ID чата пользователя
    :param train_time: Время отправления поезда
    :return: True если есть запрос на остановку
    """
    try:
        # Проверяем файл-флаг (быстрая проверка)
        flag_files = list(SYNC_DIR.glob(f"stop_{chat_id}_{train_time.replace(':', '-')}*.flag"))
        if flag_files:
            logger.debug(f"Найден файл-флаг остановки для {chat_id}:{train_time}")
            return True
        
        # Проверяем базу данных (надежная проверка)
        with get_db_cursor() as cursor:
            cursor.execute("""
                SELECT id FROM sync_flags 
                WHERE chat_id = ? AND train_time = ? AND action = 'STOP' AND processed = 0
                ORDER BY created_at DESC
                LIMIT 1
            """, (chat_id, train_time))
            
            result = cursor.fetchone()
            if result:
                logger.debug(f"Найден запрос остановки в БД для {chat_id}:{train_time}")
                return True
        
        return False
        
    except Exception as e:
        logger.error(f"Ошибка при проверке флага остановки: {e}", exc_info=True)
        return False


def is_tracking_active_in_db(chat_id: int, train_time: str) -> bool:
    """
    Проверяет существование трекинга в базе данных.
    
    Критически важно для синхронизации: если запись удалена через админ-панель,
    поток должен завершиться даже если нет явного флага остановки.
    
    :param chat_id: ID чата пользователя
    :param train_time: Время отправления поезда
    :return: True если трекинг существует в БД
    """
    try:
        with get_db_cursor() as cursor:
            cursor.execute("""
                SELECT id FROM active_trackings 
                WHERE chat_id = ? AND train_time = ?
            """, (chat_id, train_time))
            return cursor.fetchone() is not None
    except Exception as e:
        logger.error(f"Ошибка при проверке трекинга в БД: {e}", exc_info=True)
        return False


def cleanup_old_sync_flags(max_age_hours: int = 24):
    """
    Очищает старые обработанные флаги синхронизации.
    
    Вызывается периодически для предотвращения разрастания таблицы.
    
    :param max_age_hours: Максимальный возраст флагов в часах
    """
    try:
        with get_db_cursor() as cursor:
            cursor.execute("""
                DELETE FROM sync_flags 
                WHERE processed = 1 AND created_at < datetime('now', ?)
            """, (f'-{max_age_hours} hours',))
            
            deleted_count = cursor.rowcount
            if deleted_count > 0:
                logger.info(f"🧹 Очищено {deleted_count} старых флагов синхронизации")
                
    except Exception as e:
        logger.error(f"Ошибка при очистке флагов: {e}", exc_info=True)


def get_pending_sync_actions(chat_id: int = None) -> List[Dict[str, Any]]:
    """
    Получает необработанные действия синхронизации.
    
    :param chat_id: Опционально фильтрует по ID чата
    :return: Список действий синхронизации
    """
    try:
        with get_db_cursor() as cursor:
            if chat_id:
                cursor.execute("""
                    SELECT chat_id, train_time, action, created_at, admin
                    FROM sync_flags 
                    WHERE processed = 0 AND chat_id = ?
                    ORDER BY created_at ASC
                """, (chat_id,))
            else:
                cursor.execute("""
                    SELECT chat_id, train_time, action, created_at
                    FROM sync_flags 
                    WHERE processed = 0
                    ORDER BY created_at ASC
                """)
            
            return [dict(row) for row in cursor.fetchall()]
            
    except Exception as e:
        logger.error(f"Ошибка при получении pending действий: {e}", exc_info=True)
        return []


def force_delete_tracking(chat_id: int, train_time: str) -> bool:
    """
    Принудительно удаляет трекинг из БД без graceful shutdown.
    
    Используется только в экстренных случаях когда бот недоступен.
    После вызова этой функции бот должен обнаружить отсутствие записи 
    при следующей проверке и завершить поток.
    
    :param chat_id: ID чата пользователя
    :param train_time: Время отправления поезда
    :return: True если удаление прошло успешно
    """
    try:
        with get_db_cursor() as cursor:
            # Сначала создаем флаг остановки (на случай если бот еще работает)
            cursor.execute("""
                INSERT INTO sync_flags (chat_id, train_time, action, created_at)
                VALUES (?, ?, 'FORCE_STOP', CURRENT_TIMESTAMP)
            """, (chat_id, train_time))
            
            # Затем удаляем трекинг
            cursor.execute("""
                DELETE FROM active_trackings 
                WHERE chat_id = ? AND train_time = ?
            """, (chat_id, train_time))
            
            deleted = cursor.rowcount > 0
            
            if deleted:
                # Создаем файл-флаг для мгновенного уведомления бота
                flag_file = SYNC_DIR / f"force_stop_{chat_id}_{train_time.replace(':', '-')}.flag"
                flag_file.write_text(json.dumps({
                    'chat_id': chat_id,
                    'train_time': train_time,
                    'action': 'FORCE_STOP',
                    'timestamp': datetime.now().isoformat()
                }))
                
                logger.warning(
                    f"⚠️ Принудительное удаление трекинга: "
                    f"chat_id={chat_id}, train_time={train_time}"
                )
            
            return deleted
            
    except Exception as e:
        logger.error(f"Ошибка при принудительном удалении: {e}", exc_info=True)
        return False


# Инициализация при импорте модуля
create_sync_table()
