"""
Скрипт миграции базы данных для исправления проблем с уникальностью трекингов
и сохранением счетчиков запросов.

Проблемы которые решает:
1. Удаление всех дубликатов вместо одного конкретного трека
2. Сброс счетчика запросов при добавлении трека с такими же параметрами
3. Отсутствие уникального идентификатора для каждого трекинга

Решения:
1. Добавление уникального индекса на id (уже есть AUTOINCREMENT)
2. Переработка логики удаления/добавления через tracking_id
3. Сохранение истории счетчиков в отдельной таблице
4. Добавление поля is_stopped для graceful shutdown
"""

import sqlite3
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

def migrate_database():
    """Выполняет миграцию базы данных"""
    print(f"🔧 Начало миграции базы данных: {DATABASE_PATH}")
    
    conn = get_db_connection()
    cursor = conn.cursor()
    
    try:
        # === ШАГ 0: Инициализация базовой структуры если её нет ===
        print("\n📋 Проверка базовой структуры...")
        
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='active_trackings'")
        if not cursor.fetchone():
            print("   ⚠️ Таблица active_trackings не найдена. Создаем базовую структуру...")
            
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
            
            # Таблица активных трекингов (базовая структура)
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
            
            # Индексы
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_trackings_chat ON active_trackings(chat_id)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_history_chat ON search_history(chat_id)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_stations_name ON popular_stations(station_name)")
            
            print("   ✅ Базовая структура создана")
        else:
            print("   ✅ Таблица active_trackings существует")
        
        # === ШАГ 1: Проверка текущей структуры ===
        print("\n📊 Анализ текущей структуры...")
        
        cursor.execute("PRAGMA table_info(active_trackings)")
        columns = {col['name']: col for col in cursor.fetchall()}
        print(f"   Найдено колонок в active_trackings: {len(columns)}")
        for col_name, col_info in columns.items():
            print(f"   - {col_name}: {col_info['type']}")
        
        # === ШАГ 2: Добавление новых полей если их нет ===
        print("\n🔨 Добавление новых полей...")
        
        # Поле is_stopped для graceful shutdown
        if 'is_stopped' not in columns:
            print("   ➕ Добавляем поле is_stopped...")
            cursor.execute("""
                ALTER TABLE active_trackings 
                ADD COLUMN is_stopped BOOLEAN DEFAULT 0
            """)
            print("   ✅ Поле is_stopped добавлено")
        else:
            print("   ⏭️ Поле is_stopped уже существует")
        
        # Поле last_request_count для сохранения последнего счетчика
        if 'last_request_count' not in columns:
            print("   ➕ Добавляем поле last_request_count...")
            cursor.execute("""
                ALTER TABLE active_trackings 
                ADD COLUMN last_request_count INTEGER DEFAULT 0
            """)
            print("   ✅ Поле last_request_count добавлено")
        else:
            print("   ⏭️ Поле last_request_count уже существует")
        
        # Поле unique_token для абсолютной уникальности каждого трекинга
        if 'unique_token' not in columns:
            print("   ➕ Добавляем поле unique_token...")
            cursor.execute("""
                ALTER TABLE active_trackings 
                ADD COLUMN unique_token TEXT
            """)
            # Генерируем уникальные токены для существующих записей
            cursor.execute("SELECT id FROM active_trackings WHERE unique_token IS NULL")
            rows = cursor.fetchall()
            for row in rows:
                token = f"{row['id']}_{datetime.now().timestamp()}_{os.urandom(4).hex()}"
                cursor.execute("""
                    UPDATE active_trackings 
                    SET unique_token = ? 
                    WHERE id = ?
                """, (token, row['id']))
            print(f"   ✅ Поле unique_token добавлено, сгенерировано {len(rows)} токенов")
        else:
            print("   ⏭️ Поле unique_token уже существует")
        
        # === ШАГ 3: Создание таблицы истории счетчиков ===
        print("\n🔨 Создание таблицы истории счетчиков...")
        
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS request_counter_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tracking_id INTEGER NOT NULL,
                chat_id INTEGER NOT NULL,
                requests_count INTEGER DEFAULT 0,
                recorded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                reason TEXT,  -- 'periodic_save', 'tracking_stop', 'migration'
                FOREIGN KEY (tracking_id) REFERENCES active_trackings(id) ON DELETE CASCADE
            )
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_counter_history_tracking 
            ON request_counter_history(tracking_id)
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_counter_history_chat 
            ON request_counter_history(chat_id)
        """)
        print("   ✅ Таблица request_counter_history создана")
        
        # === ШАГ 4: Сохранение текущих счетчиков в историю ===
        print("\n💾 Сохранение текущих счетчиков в историю...")
        
        cursor.execute("""
            SELECT id, chat_id, requests_count 
            FROM active_trackings 
            WHERE requests_count > 0
        """)
        rows = cursor.fetchall()
        
        for row in rows:
            cursor.execute("""
                INSERT INTO request_counter_history 
                (tracking_id, chat_id, requests_count, reason)
                VALUES (?, ?, ?, 'migration')
            """, (row['id'], row['chat_id'], row['requests_count']))
        
        print(f"   ✅ Сохранено {len(rows)} счетчиков в историю")
        
        # === ШАГ 5: Обновление sync_flags таблицы ===
        print("\n🔨 Обновление таблицы sync_flags...")
        
        cursor.execute("PRAGMA table_info(sync_flags)")
        sync_columns = {col['name'] for col in cursor.fetchall()}
        
        if 'unique_token' not in sync_columns:
            print("   ➕ Добавляем поле unique_token в sync_flags...")
            cursor.execute("""
                ALTER TABLE sync_flags 
                ADD COLUMN unique_token TEXT
            """)
            print("   ✅ Поле unique_token добавлено в sync_flags")
        else:
            print("   ⏭️ Поле unique_token уже существует в sync_flags")
        
        # === ШАГ 6: Создание представлений для отладки ===
        print("\n🔨 Создание представлений...")
        
        # Представление для просмотра дубликатов
        cursor.execute("""
            CREATE VIEW IF NOT EXISTS duplicate_trackings AS
            SELECT 
                chat_id, 
                train_time, 
                from_station, 
                to_station, 
                date,
                COUNT(*) as duplicate_count,
                GROUP_CONCAT(id) as tracking_ids
            FROM active_trackings
            WHERE is_stopped = 0
            GROUP BY chat_id, train_time, from_station, to_station, date
            HAVING COUNT(*) > 1
        """)
        print("   ✅ Представление duplicate_trackings создано")
        
        # === ШАГ 7: Проверка результатов ===
        print("\n📊 Проверка результатов миграции...")
        
        cursor.execute("SELECT COUNT(*) as count FROM active_trackings")
        total_trackings = cursor.fetchone()['count']
        
        cursor.execute("SELECT COUNT(*) as count FROM active_trackings WHERE is_stopped = 0")
        active_trackings = cursor.fetchone()['count']
        
        cursor.execute("SELECT COUNT(*) as count FROM request_counter_history")
        history_count = cursor.fetchone()['count']
        
        cursor.execute("SELECT COUNT(*) as count FROM duplicate_trackings")
        duplicates = cursor.fetchone()['count']
        
        print(f"\n✅ Миграция завершена успешно!")
        print(f"   📈 Всего трекингов: {total_trackings}")
        print(f"   🟢 Активных трекингов: {active_trackings}")
        print(f"   📚 Записей в истории счетчиков: {history_count}")
        print(f"   ⚠️ Найдено дубликатов: {duplicates}")
        
        if duplicates > 0:
            print("\n⚠️ ВНИМАНИЕ: Обнаружены дубликаты трекингов!")
            print("   Рекомендуется вручную проверить и удалить лишние треки через админ-панель.")
            print("   Теперь каждый трек можно точно идентифицировать по полю 'id' или 'unique_token'.")
        
        conn.commit()
        print("\n✅ Изменения сохранены в базе данных")
        
    except Exception as e:
        conn.rollback()
        print(f"\n❌ Ошибка миграции: {e}")
        raise
    finally:
        conn.close()
        print("\n🔒 Подключение к базе данных закрыто")


def verify_migration():
    """Проверяет корректность миграции"""
    print("\n🔍 Проверка миграции...")
    
    conn = get_db_connection()
    cursor = conn.cursor()
    
    try:
        # Проверка наличия всех полей
        cursor.execute("PRAGMA table_info(active_trackings)")
        columns = {col['name'] for col in cursor.fetchall()}
        
        required_columns = {
            'id', 'chat_id', 'train_time', 'requests_count',
            'is_stopped', 'last_request_count', 'unique_token'
        }
        
        missing = required_columns - columns
        if missing:
            print(f"❌ Отсутствуют поля: {missing}")
            return False
        
        print("   ✅ Все необходимые поля присутствуют")
        
        # Проверка таблицы истории
        cursor.execute("SELECT COUNT(*) FROM request_counter_history")
        count = cursor.fetchone()[0]
        print(f"   ✅ Таблица истории содержит {count} записей")
        
        # Проверка уникальности токенов
        cursor.execute("""
            SELECT unique_token, COUNT(*) as cnt 
            FROM active_trackings 
            WHERE unique_token IS NOT NULL
            GROUP BY unique_token 
            HAVING cnt > 1
        """)
        duplicates = cursor.fetchall()
        
        if duplicates:
            print(f"   ⚠️ Найдено {len(duplicates)} дубликатов unique_token")
            return False
        
        print("   ✅ Все unique_token уникальны")
        print("\n✅ Проверка пройдена успешно!")
        return True
        
    except Exception as e:
        print(f"❌ Ошибка проверки: {e}")
        return False
    finally:
        conn.close()


if __name__ == "__main__":
    print("=" * 60)
    print("🚀 МИГРАЦИЯ БАЗЫ ДАННЫХ TICKET BOT")
    print("=" * 60)
    
    # Проверка существования БД
    if not os.path.exists(DATABASE_PATH):
        print(f"❌ База данных не найдена: {DATABASE_PATH}")
        print("   Сначала запустите бота для создания базы данных.")
        exit(1)
    
    # Выполнение миграции
    migrate_database()
    
    # Проверка результатов
    if verify_migration():
        print("\n" + "=" * 60)
        print("✅ МИГРАЦИЯ ЗАВЕРШЕНА УСПЕШНО!")
        print("=" * 60)
        print("\n📝 Следующие шаги:")
        print("   1. Перезапустите ticket_bot.py")
        print("   2. Перезапустите admin_panel.py")
        print("   3. Проверьте работу через админ-панель")
        print("\n💡 Теперь каждый трекинг имеет уникальный ID и token,")
        print("   что позволяет удалять конкретные треки без влияния на другие.")
    else:
        print("\n" + "=" * 60)
        print("⚠️ МИГРАЦИЯ ЗАВЕРШЕНА С ПРЕДУПРЕЖДЕНИЯМИ")
        print("=" * 60)
        exit(1)
