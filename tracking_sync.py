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


def request_tracking_stop(chat_id: int, train_time: str = None, admin_username: str = None, tracking_id: int = None) -> bool:
    """
    Запрашивает остановку трекинга.
    
    Архитектурное решение:
    - Вместо прямого удаления из БД сначала устанавливаем флаг is_stopped=1
    - Бот проверяет флаг в каждом цикле и корректно завершает поток
    - Поток выполняет финальное сохранение статистики и вызывает confirm_tracking_stopped()
    - confirm_tracking_stopped() записывает историю и удаляет запись из БД
    
    :param chat_id: ID чата пользователя
    :param train_time: Время отправления поезда (ключ трекинга, опционально)
    :param admin_username: Имя администратора (для аудита)
    :param tracking_id: Уникальный ID трекинга (приоритет над train_time)
    :return: True если запрос успешно создан
    """
    try:
        with get_db_cursor() as cursor:
            # Определяем трекинг для остановки
            if tracking_id:
                # Поиск по уникальному ID
                cursor.execute("""
                    SELECT id, chat_id, train_time, requests_count, last_request_count,
                           from_station, to_station, train_num, unique_token, created_at
                    FROM active_trackings 
                    WHERE id = ?
                """, (tracking_id,))
            elif train_time:
                # Поиск по chat_id + train_time (старый метод)
                cursor.execute("""
                    SELECT id, chat_id, train_time, requests_count, last_request_count,
                           from_station, to_station, train_num, unique_token, created_at
                    FROM active_trackings 
                    WHERE chat_id = ? AND train_time = ?
                """, (chat_id, train_time))
            else:
                logger.error("request_tracking_stop: требуется tracking_id или train_time")
                return False
            
            tracking = cursor.fetchone()
            
            if not tracking:
                logger.warning(f"Трекинг не найден: tracking_id={tracking_id}, chat_id={chat_id}, train_time={train_time}")
                return False
            
            # Извлекаем данные трекинга
            actual_tracking_id = tracking['id']
            actual_chat_id = tracking['chat_id']
            actual_train_time = tracking['train_time']
            
            # Устанавливаем флаг is_stopped=1 в БД (graceful shutdown)
            cursor.execute("""
                UPDATE active_trackings 
                SET is_stopped = 1, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
            """, (actual_tracking_id,))
            
            # Создаем запись в sync_flags для уведомления бота
            cursor.execute("""
                INSERT INTO sync_flags (chat_id, train_time, action, tracking_id, admin_username, created_at)
                VALUES (?, ?, 'STOP', ?, ?, CURRENT_TIMESTAMP)
            """, (actual_chat_id, actual_train_time, actual_tracking_id, admin_username))
            
            # Создаем файл-уведомление для мгновенной реакции бота
            flag_file = SYNC_DIR / f"stop_{actual_chat_id}_{actual_train_time.replace(':', '-')}.flag"
            flag_file.write_text(json.dumps({
                'chat_id': actual_chat_id,
                'train_time': actual_train_time,
                'tracking_id': actual_tracking_id,
                'action': 'STOP',
                'admin': admin_username,
                'timestamp': datetime.now().isoformat()
            }))
            
            logger.info(
                f"🛑 Запрошена остановка трекинга: tracking_id={actual_tracking_id}, "
                f"chat_id={actual_chat_id}, train_time={actual_train_time}, admin={admin_username}"
            )
            return True
            
    except Exception as e:
        logger.error(f"Ошибка при запросе остановки: {e}", exc_info=True)
        return False


def confirm_tracking_stopped(chat_id: int, train_time: str = None, tracking_id: int = None, reason: str = 'user_stop') -> bool:
    """
    Подтверждает остановку трекинга после завершения потока.
    
    Архитектурное решение:
    - Записывает финальную статистику в request_counter_history
    - Удаляет запись из active_trackings
    - Отмечает флаги синхронизации как обработанные
    - Удаляет файлы-флаги
    
    :param chat_id: ID чата пользователя
    :param train_time: Время отправления поезда (опционально, если есть tracking_id)
    :param tracking_id: Уникальный ID трекинга (приоритет над train_time)
    :param reason: Причина остановки (user_stop, admin_stop, success, timeout, error)
    :return: True если подтверждение успешно записано
    """
    try:
        with get_db_cursor() as cursor:
            # Определяем трекинг для остановки
            if tracking_id:
                cursor.execute("""
                    SELECT id, chat_id, train_time, requests_count, last_request_count,
                           from_station, to_station, train_num, seats_available, 
                           unique_token, created_at
                    FROM active_trackings 
                    WHERE id = ?
                """, (tracking_id,))
            elif train_time:
                cursor.execute("""
                    SELECT id, chat_id, train_time, requests_count, last_request_count,
                           from_station, to_station, train_num, seats_available,
                           unique_token, created_at
                    FROM active_trackings 
                    WHERE chat_id = ? AND train_time = ?
                """, (chat_id, train_time))
            else:
                logger.error("confirm_tracking_stopped: требуется tracking_id или train_time")
                return False
            
            tracking = cursor.fetchone()
            
            if not tracking:
                logger.warning(f"Трекинг не найден для подтверждения остановки: tracking_id={tracking_id}, chat_id={chat_id}")
                return False
            
            # Извлекаем данные трекинга
            actual_tracking_id = tracking['id']
            actual_chat_id = tracking['chat_id']
            actual_train_time = tracking['train_time']
            
            # Записываем историю перед удалением (сохраняем счетчики)
            cursor.execute("""
                INSERT INTO request_counter_history 
                (tracking_id, chat_id, from_station, to_station, train_time, train_num,
                 final_requests_count, last_request_count, seats_found, reason, 
                 unique_token, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                actual_tracking_id,
                actual_chat_id,
                tracking['from_station'],
                tracking['to_station'],
                actual_train_time,
                tracking['train_num'],
                tracking['requests_count'],
                tracking['last_request_count'],
                tracking['seats_available'],
                reason,
                tracking['unique_token'],
                tracking['created_at']
            ))
            
            logger.info(
                f"📝 Сохранена история трекинга: tracking_id={actual_tracking_id}, "
                f"requests={tracking['requests_count']}, reason={reason}"
            )
            
            # Отмечаем все флаги STOP как обработанные
            cursor.execute("""
                UPDATE sync_flags 
                SET processed = 1, processed_at = CURRENT_TIMESTAMP
                WHERE chat_id = ? AND train_time = ? AND action = 'STOP' AND processed = 0
            """, (actual_chat_id, actual_train_time))
            
            # Удаляем файл-флаг
            flag_files = list(SYNC_DIR.glob(f"stop_{actual_chat_id}_{actual_train_time.replace(':', '-')}*.flag"))
            for flag_file in flag_files:
                try:
                    flag_file.unlink()
                except Exception as e:
                    logger.warning(f"Не удалось удалить флаг {flag_file}: {e}")
            
            # Теперь удаляем сам трекинг из БД
            cursor.execute("""
                DELETE FROM active_trackings 
                WHERE id = ?
            """, (actual_tracking_id,))
            
            deleted_count = cursor.rowcount
            logger.info(f"✅ Трекинг {actual_chat_id}:{actual_train_time} (ID={actual_tracking_id}) остановлен и удален из БД (удалено строк: {deleted_count})")
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
