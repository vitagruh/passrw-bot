"""
Скрипт миграции базы данных для внедрения уникальной идентификации трекингов.

Цели миграции:
1. Добавить поле unique_token (UUID) в active_trackings
2. Добавить поле is_stopped для graceful shutdown
3. Добавить поле last_request_count для снэпшотов
4. Создать таблицу request_counter_history для хранения истории
5. Обновить индексы для работы с tracking_id

Best Practices:
- Атомарность операций (транзакции)
- Сохранение существующих данных
- Генерация unique_token для старых записей
- Обратная совместимость на период перехода
"""

import sqlite3
import uuid
import os
from datetime import datetime
from pathlib import Path

DATABASE_PATH = os.getenv("DATABASE_PATH", "data/ticket_bot.db")


def get_db_connection():
    """Создает подключение к базе данных"""
    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def migrate_active_trackings():
    """
    Миграция таблицы active_trackings:
    - Добавление unique_token
    - Добавление is_stopped
    - Добавление last_request_count
    - Добавление updated_at
    """
    print("🔄 Миграция таблицы active_trackings...")
    
    conn = get_db_connection()
    cursor = conn.cursor()
    
    try:
        # Проверяем是否存在 новые колонки
        cursor.execute("PRAGMA table_info(active_trackings)")
        columns = {row['name'] for row in cursor.fetchall()}
        
        # Добавляем unique_token если нет
        if 'unique_token' not in columns:
            print("  ➕ Добавление колонки unique_token...")
            cursor.execute("""
                ALTER TABLE active_trackings 
                ADD COLUMN unique_token TEXT
            """)
            
            # Генерируем unique_token для всех существующих записей
            cursor.execute("SELECT id, chat_id, train_time, created_at FROM active_trackings")
            rows = cursor.fetchall()
            for row in rows:
                # Генерируем UUID на основе id, chat_id, train_time и timestamp
                token_data = f"{row['id']}-{row['chat_id']}-{row['train_time']}-{row['created_at']}"
                unique_token = str(uuid.uuid5(uuid.NAMESPACE_DNS, token_data))
                cursor.execute("""
                    UPDATE active_trackings 
                    SET unique_token = ? 
                    WHERE id = ?
                """, (unique_token, row['id']))
                print(f"    ✓ Сгенерирован unique_token для трека ID={row['id']}")
        
        # Добавляем is_stopped если нет
        if 'is_stopped' not in columns:
            print("  ➕ Добавление колонки is_stopped...")
            cursor.execute("""
                ALTER TABLE active_trackings 
                ADD COLUMN is_stopped BOOLEAN DEFAULT 0
            """)
            print("    ✓ Колонка is_stopped добавлена (default=0)")
        
        # Добавляем last_request_count если нет
        if 'last_request_count' not in columns:
            print("  ➕ Добавление колонки last_request_count...")
            cursor.execute("""
                ALTER TABLE active_trackings 
                ADD COLUMN last_request_count INTEGER DEFAULT 0
            """)
            # Инициализируем текущим requests_count
            cursor.execute("""
                UPDATE active_trackings 
                SET last_request_count = requests_count
            """)
            print("    ✓ Колонка last_request_count добавлена и инициализирована")
        
        # Добавляем updated_at если нет
        if 'updated_at' not in columns:
            print("  ➕ Добавление колонки updated_at...")
            # SQLite не позволяет добавлять колонки с non-constant default
            # Поэтому добавляем без default, затем обновляем записи
            cursor.execute("""
                ALTER TABLE active_trackings 
                ADD COLUMN updated_at TIMESTAMP
            """)
            
            # Обновляем существующие записи значением created_at
            cursor.execute("""
                UPDATE active_trackings 
                SET updated_at = created_at
                WHERE updated_at IS NULL
            """)
            print("    ✓ Колонка updated_at добавлена и инициализирована")
        
        # Создаем индекс на unique_token
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_trackings_unique_token 
            ON active_trackings(unique_token)
        """)
        
        # Создаем индекс на is_stopped для быстрой проверки активных
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_trackings_is_stopped 
            ON active_trackings(is_stopped, chat_id)
        """)
        
        conn.commit()
        print("✅ Таблица active_trackings успешно мигрирована")
        
    except Exception as e:
        conn.rollback()
        print(f"❌ Ошибка миграции active_trackings: {e}")
        raise
    finally:
        conn.close()


def create_history_table():
    """
    Создание таблицы request_counter_history для хранения истории трекингов.
    
    Структура:
    - tracking_id: ссылка на активный трекинг (до удаления)
    - chat_id: ID пользователя (дублируется для быстрого поиска)
    - final_requests_count: финальное количество запросов
    - reason: причина остановки (user_stop, admin_stop, timeout, success)
    - stopped_at: время остановки
    """
    print("\n🔄 Создание таблицы request_counter_history...")
    
    conn = get_db_connection()
    cursor = conn.cursor()
    
    try:
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS request_counter_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tracking_id INTEGER NOT NULL,
                chat_id INTEGER NOT NULL,
                from_station TEXT NOT NULL,
                to_station TEXT NOT NULL,
                train_time TEXT NOT NULL,
                train_num TEXT,
                final_requests_count INTEGER DEFAULT 0,
                last_request_count INTEGER DEFAULT 0,
                seats_found INTEGER DEFAULT 0,
                reason TEXT NOT NULL CHECK(reason IN (
                    'user_stop', 
                    'admin_stop', 
                    'timeout', 
                    'success', 
                    'error',
                    'force_stop'
                )),
                unique_token TEXT,
                created_at TIMESTAMP,
                stopped_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (tracking_id) REFERENCES active_trackings(id) ON DELETE SET NULL
            )
        """)
        
        # Индексы для быстрого поиска
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_history_chat 
            ON request_counter_history(chat_id, stopped_at)
        """)
        
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_history_reason 
            ON request_counter_history(reason, stopped_at)
        """)
        
        conn.commit()
        print("✅ Таблица request_counter_history создана")
        
    except Exception as e:
        conn.rollback()
        print(f"❌ Ошибка создания таблицы истории: {e}")
        raise
    finally:
        conn.close()


def update_sync_flags_table():
    """
    Обновление таблицы sync_flags для поддержки tracking_id.
    """
    print("\n🔄 Обновление таблицы sync_flags...")
    
    conn = get_db_connection()
    cursor = conn.cursor()
    
    try:
        # Проверяем существование таблицы
        cursor.execute("""
            SELECT name FROM sqlite_master 
            WHERE type='table' AND name='sync_flags'
        """)
        
        if not cursor.fetchone():
            print("  ⚠️ Таблица sync_flags не найдена, создаем...")
            cursor.execute("""
                CREATE TABLE sync_flags (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL,
                    train_time TEXT NOT NULL,
                    action TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    processed BOOLEAN DEFAULT 0,
                    processed_at TIMESTAMP
                )
            """)
        
        # Проверяем наличие колонки tracking_id
        cursor.execute("PRAGMA table_info(sync_flags)")
        columns = {row['name'] for row in cursor.fetchall()}
        
        if 'tracking_id' not in columns:
            print("  ➕ Добавление колонки tracking_id...")
            cursor.execute("""
                ALTER TABLE sync_flags 
                ADD COLUMN tracking_id INTEGER
            """)
            
            # Создаем индекс для быстрого поиска по tracking_id
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_sync_tracking_id 
                ON sync_flags(tracking_id, processed)
            """)
            print("    ✓ Колонка tracking_id добавлена")
        
        # Добавляем admin_username для аудита
        if 'admin_username' not in columns:
            print("  ➕ Добавление колонки admin_username...")
            cursor.execute("""
                ALTER TABLE sync_flags 
                ADD COLUMN admin_username TEXT
            """)
            print("    ✓ Колонка admin_username добавлена")
        
        conn.commit()
        print("✅ Таблица sync_flags обновлена")
        
    except Exception as e:
        conn.rollback()
        print(f"❌ Ошибка обновления sync_flags: {e}")
        raise
    finally:
        conn.close()


def verify_migration():
    """Проверка успешности миграции"""
    print("\n🔍 Проверка миграции...")
    
    conn = get_db_connection()
    cursor = conn.cursor()
    
    try:
        # Проверяем active_trackings
        cursor.execute("PRAGMA table_info(active_trackings)")
        columns = {row['name'] for row in cursor.fetchall()}
        required_columns = {'unique_token', 'is_stopped', 'last_request_count', 'updated_at'}
        missing = required_columns - columns
        
        if missing:
            print(f"❌ Отсутствуют колонки в active_trackings: {missing}")
            return False
        else:
            print("  ✅ active_trackings: все колонки на месте")
        
        # Проверяем request_counter_history
        cursor.execute("""
            SELECT name FROM sqlite_master 
            WHERE type='table' AND name='request_counter_history'
        """)
        if not cursor.fetchone():
            print("  ❌ Таблица request_counter_history не создана")
            return False
        else:
            print("  ✅ request_counter_history: таблица создана")
        
        # Проверяем sync_flags
        cursor.execute("PRAGMA table_info(sync_flags)")
        columns = {row['name'] for row in cursor.fetchall()}
        if 'tracking_id' not in columns:
            print("  ❌ Отсутствует tracking_id в sync_flags")
            return False
        else:
            print("  ✅ sync_flags: tracking_id добавлен")
        
        # Считаем количество записей с unique_token
        cursor.execute("""
            SELECT COUNT(*) FROM active_trackings 
            WHERE unique_token IS NOT NULL
        """)
        count = cursor.fetchone()[0]
        print(f"  ℹ️ Записей с unique_token: {count}")
        
        print("\n✅ Миграция успешно завершена!")
        return True
        
    except Exception as e:
        print(f"❌ Ошибка проверки: {e}")
        return False
    finally:
        conn.close()


def main():
    """Основная функция миграции"""
    print("=" * 60)
    print("🚀 Миграция базы данных для системы трекинга билетов")
    print("=" * 60)
    print(f"📁 Путь к БД: {DATABASE_PATH}")
    print()
    
    # Проверяем существование БД
    if not os.path.exists(DATABASE_PATH):
        print(f"❌ База данных не найдена: {DATABASE_PATH}")
        print("   Сначала запустите ticket_bot.py для создания БД")
        return False
    
    # Выполняем миграцию
    try:
        migrate_active_trackings()
        create_history_table()
        update_sync_flags_table()
        
        # Верификация
        success = verify_migration()
        
        if success:
            print("\n" + "=" * 60)
            print("✅ Все этапы миграции успешно выполнены!")
            print("=" * 60)
            print("\n📋 Что было сделано:")
            print("  • Добавлен unique_token для уникальной идентификации")
            print("  • Добавлен флаг is_stopped для graceful shutdown")
            print("  • Создана таблица истории request_counter_history")
            print("  • Обновлена таблица sync_flags")
            print("\n⚠️ Важно:")
            print("  • Теперь используйте tracking_id для всех операций")
            print("  • Удаление выполняется через флаг is_stopped")
            print("  • История сохраняется в request_counter_history")
            return True
        else:
            return False
            
    except Exception as e:
        print(f"\n❌ Критическая ошибка миграции: {e}")
        print("   Проверьте логи и попробуйте снова")
        return False


if __name__ == "__main__":
    success = main()
    exit(0 if success else 1)
