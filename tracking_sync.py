"""
Модуль синхронизации между админ-панелью и ботом.

Единый источник истины — SQLite. Дополнительно используются файлы-флаги
в /tmp для быстрой межпроцессной реакции (IPC).

Ключевая сущность — tracking_id. Остановка всегда адресуется конкретному
трекингу, а не пользователю, чтобы не задевать остальные поезда.
"""

import json
import logging
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import db

logger = logging.getLogger('TrackingSync')
logger.setLevel(logging.INFO)

SYNC_DIR = Path(os.getenv("SYNC_DIR", "/tmp/ticket_bot_sync"))
DATABASE_PATH = db.DATABASE_PATH


@contextmanager
def get_db_cursor():
    with db.get_db_cursor(DATABASE_PATH) as cursor:
        yield cursor


def create_sync_table():
    """Совместимость со старым API: создаёт полную схему БД."""
    db.init_schema()


def _flag_paths(chat_id: int, train_time: Optional[str], tracking_id: Optional[int]) -> List[Path]:
    paths = []
    if tracking_id is not None:
        paths.append(SYNC_DIR / f"stop_{tracking_id}.flag")
    if train_time:
        safe_time = str(train_time).replace(':', '-').replace('/', '-')
        paths.append(SYNC_DIR / f"stop_{chat_id}_{safe_time}.flag")
    return paths


def _write_flag(path: Path, payload: Dict[str, Any]) -> None:
    try:
        SYNC_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False))
    except Exception as e:
        logger.warning(f"Не удалось создать файл-флаг {path}: {e}")


def _remove_flags(chat_id: int, train_time: Optional[str], tracking_id: Optional[int]) -> None:
    patterns = []
    if tracking_id is not None:
        patterns.append(f"stop_{tracking_id}*.flag")
    if train_time:
        safe_time = str(train_time).replace(':', '-').replace('/', '-')
        patterns.append(f"stop_{chat_id}_{safe_time}*.flag")
    for pattern in patterns:
        for flag in SYNC_DIR.glob(pattern):
            try:
                flag.unlink()
            except Exception as e:
                logger.warning(f"Не удалось удалить флаг {flag}: {e}")


def _find_tracking(cursor: sqlite3.Cursor, chat_id: int,
                   train_time: Optional[str], tracking_id: Optional[int]):
    if tracking_id is not None:
        cursor.execute("SELECT * FROM active_trackings WHERE id = ?", (tracking_id,))
    elif train_time:
        cursor.execute(
            "SELECT * FROM active_trackings WHERE chat_id = ? AND train_time = ? ORDER BY id DESC LIMIT 1",
            (chat_id, train_time),
        )
    else:
        return None
    return cursor.fetchone()


def request_tracking_stop(chat_id: int, train_time: str = None,
                          admin_username: str = None, tracking_id: int = None) -> bool:
    """Запрашивает мягкую остановку конкретного трекинга."""
    if tracking_id is None and not train_time:
        logger.error("request_tracking_stop: требуется tracking_id или train_time")
        return False
    try:
        with get_db_cursor() as cursor:
            tracking = _find_tracking(cursor, chat_id, train_time, tracking_id)
            if not tracking:
                logger.warning(
                    f"Трекинг не найден: tracking_id={tracking_id}, chat_id={chat_id}, train_time={train_time}"
                )
                return False

            actual_id = tracking['id']
            actual_chat_id = tracking['chat_id']
            actual_time = tracking['train_time']

            cursor.execute(
                "UPDATE active_trackings SET is_stopped = 1, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (actual_id,),
            )
            cursor.execute("""
                INSERT INTO sync_flags
                    (chat_id, train_time, tracking_id, action, admin_username, created_at)
                VALUES (?, ?, ?, 'STOP', ?, CURRENT_TIMESTAMP)
            """, (actual_chat_id, actual_time, actual_id, admin_username))

            payload = {
                'chat_id': actual_chat_id,
                'train_time': actual_time,
                'tracking_id': actual_id,
                'action': 'STOP',
                'admin': admin_username,
                'timestamp': datetime.now().isoformat(),
            }
            for path in _flag_paths(actual_chat_id, actual_time, actual_id):
                _write_flag(path, payload)

            logger.info(
                f"🛑 Запрошена остановка трекинга: id={actual_id}, "
                f"chat_id={actual_chat_id}, train_time={actual_time}, admin={admin_username}"
            )
            return True
    except Exception as e:
        logger.error(f"Ошибка при запросе остановки: {e}", exc_info=True)
        return False


def confirm_tracking_stopped(chat_id: int, train_time: str = None,
                             tracking_id: int = None, reason: str = 'user_stop') -> bool:
    """Фиксирует историю и удаляет трекинг после завершения потока."""
    try:
        with get_db_cursor() as cursor:
            tracking = _find_tracking(cursor, chat_id, train_time, tracking_id)
            if not tracking:
                # Уже удалён — это не ошибка, просто снимаем флаги.
                _remove_flags(chat_id, train_time, tracking_id)
                return True

            actual_id = tracking['id']
            actual_chat_id = tracking['chat_id']
            actual_time = tracking['train_time']

            cursor.execute("""
                INSERT INTO request_counter_history
                    (tracking_id, chat_id, requests_count, reason)
                VALUES (?, ?, ?, ?)
            """, (actual_id, actual_chat_id, tracking['requests_count'] or 0, reason))

            if tracking_id is not None:
                cursor.execute("""
                    UPDATE sync_flags
                    SET processed = 1, processed_at = CURRENT_TIMESTAMP
                    WHERE tracking_id = ? AND action IN ('STOP', 'FORCE_STOP') AND processed = 0
                """, (actual_id,))
            else:
                cursor.execute("""
                    UPDATE sync_flags
                    SET processed = 1, processed_at = CURRENT_TIMESTAMP
                    WHERE chat_id = ? AND train_time = ? AND action IN ('STOP', 'FORCE_STOP')
                          AND processed = 0
                """, (actual_chat_id, actual_time))

            _remove_flags(actual_chat_id, actual_time, actual_id)

            cursor.execute("DELETE FROM active_trackings WHERE id = ?", (actual_id,))
            logger.info(
                f"✅ Трекинг {actual_chat_id}:{actual_time} (id={actual_id}) остановлен "
                f"и удалён из БД, reason={reason}"
            )
            return True
    except Exception as e:
        logger.error(f"Ошибка при подтверждении остановки: {e}", exc_info=True)
        return False


def check_stop_request(chat_id: int, train_time: str = None, tracking_id: int = None) -> bool:
    """Проверяет запрос остановки: сначала быстрый файл-флаг, затем БД."""
    try:
        if tracking_id is not None and (SYNC_DIR / f"stop_{tracking_id}.flag").exists():
            return True
        if train_time:
            safe_time = str(train_time).replace(':', '-').replace('/', '-')
            if list(SYNC_DIR.glob(f"stop_{chat_id}_{safe_time}*.flag")):
                return True

        with get_db_cursor() as cursor:
            if tracking_id is not None:
                cursor.execute("""
                    SELECT id FROM sync_flags
                    WHERE tracking_id = ? AND action IN ('STOP', 'FORCE_STOP') AND processed = 0
                    ORDER BY created_at DESC LIMIT 1
                """, (tracking_id,))
            elif train_time:
                cursor.execute("""
                    SELECT id FROM sync_flags
                    WHERE chat_id = ? AND train_time = ? AND action IN ('STOP', 'FORCE_STOP')
                          AND processed = 0
                    ORDER BY created_at DESC LIMIT 1
                """, (chat_id, train_time))
            else:
                return False
            return cursor.fetchone() is not None
    except Exception as e:
        logger.error(f"Ошибка при проверке флага остановки: {e}", exc_info=True)
        return False


def is_tracking_active_in_db(chat_id: int, train_time: str = None,
                             tracking_id: int = None) -> bool:
    """Проверяет, что трекинг ещё существует (его не удалили из админки)."""
    try:
        with get_db_cursor() as cursor:
            if tracking_id is not None:
                cursor.execute("SELECT id FROM active_trackings WHERE id = ?", (tracking_id,))
            elif train_time:
                cursor.execute(
                    "SELECT id FROM active_trackings WHERE chat_id = ? AND train_time = ?",
                    (chat_id, train_time),
                )
            else:
                cursor.execute("SELECT id FROM active_trackings WHERE chat_id = ?", (chat_id,))
            return cursor.fetchone() is not None
    except Exception as e:
        logger.error(f"Ошибка при проверке трекинга в БД: {e}", exc_info=True)
        return False


def get_tracking_runtime(tracking_id: int) -> Optional[sqlite3.Row]:
    """Актуальные настройки трекинга (heartbeat, счётчики) для потока."""
    try:
        with get_db_cursor() as cursor:
            cursor.execute("SELECT * FROM active_trackings WHERE id = ?", (tracking_id,))
            return cursor.fetchone()
    except Exception as e:
        logger.error(f"Ошибка чтения настроек трекинга {tracking_id}: {e}")
        return None


def cleanup_old_sync_flags(max_age_hours: int = 24) -> int:
    """Удаляет обработанные флаги синхронизации старше N часов."""
    try:
        with get_db_cursor() as cursor:
            cursor.execute(
                "DELETE FROM sync_flags WHERE processed = 1 AND created_at < datetime('now', ?)",
                (f'-{max_age_hours} hours',),
            )
            deleted = cursor.rowcount or 0
            if deleted:
                logger.info(f"🧹 Очищено {deleted} старых флагов синхронизации")
            return deleted
    except Exception as e:
        logger.error(f"Ошибка при очистке флагов: {e}", exc_info=True)
        return 0


def get_pending_sync_actions(chat_id: int = None) -> List[Dict[str, Any]]:
    try:
        with get_db_cursor() as cursor:
            if chat_id:
                cursor.execute("""
                    SELECT chat_id, train_time, tracking_id, action, admin_username, created_at
                    FROM sync_flags
                    WHERE processed = 0 AND chat_id = ?
                    ORDER BY created_at ASC
                """, (chat_id,))
            else:
                cursor.execute("""
                    SELECT chat_id, train_time, tracking_id, action, admin_username, created_at
                    FROM sync_flags
                    WHERE processed = 0
                    ORDER BY created_at ASC
                """)
            return [dict(row) for row in cursor.fetchall()]
    except Exception as e:
        logger.error(f"Ошибка при получении pending действий: {e}", exc_info=True)
        return []


def force_delete_tracking(chat_id: int, train_time: str = None,
                          tracking_id: int = None) -> bool:
    """Экстренное удаление без мягкой остановки."""
    if tracking_id is None and not train_time:
        return False
    try:
        with get_db_cursor() as cursor:
            tracking = _find_tracking(cursor, chat_id, train_time, tracking_id)
            if not tracking:
                return False
            actual_id = tracking['id']
            actual_time = tracking['train_time']

            cursor.execute("""
                INSERT INTO sync_flags
                    (chat_id, train_time, tracking_id, action, created_at)
                VALUES (?, ?, ?, 'FORCE_STOP', CURRENT_TIMESTAMP)
            """, (chat_id, actual_time, actual_id))
            cursor.execute("DELETE FROM active_trackings WHERE id = ?", (actual_id,))

            _write_flag(
                SYNC_DIR / f"stop_{actual_id}.flag",
                {
                    'chat_id': chat_id,
                    'train_time': actual_time,
                    'tracking_id': actual_id,
                    'action': 'FORCE_STOP',
                    'timestamp': datetime.now().isoformat(),
                },
            )
            logger.warning(f"⚠️ Принудительно удалён трекинг id={actual_id} (chat_id={chat_id})")
            return True
    except Exception as e:
        logger.error(f"Ошибка при принудительном удалении: {e}", exc_info=True)
        return False


# Каталог для IPC должен существовать до первой записи.
try:
    SYNC_DIR.mkdir(parents=True, exist_ok=True)
except Exception:
    pass
