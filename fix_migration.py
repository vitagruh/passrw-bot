import sqlite3
import os
import uuid
from datetime import datetime

DB_PATH = 'tickets.db'

def get_column_names(cursor, table_name):
    """Получает список имен колонок в таблице"""
    cursor.execute(f"PRAGMA table_info({table_name});")
    return [col[1] for col in cursor.fetchall()]

def migrate():
    if not os.path.exists(DB_PATH):
        print(f"❌ Файл базы данных {DB_PATH} не найден!")
        return

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    try:
        print("🔄 Начало безопасной миграции...\n")

        # --- 1. Обновление request_counter_history ---
        print("📋 Проверка таблицы request_counter_history...")
        history_cols = get_column_names(cursor, 'request_counter_history')
        
        cols_to_add = []
        if 'stopped_at' not in history_cols:
            cols_to_add.append(('stopped_at', 'TIMESTAMP'))
        if 'final_seats_available' not in history_cols:
            cols_to_add.append(('final_seats_available', 'INTEGER'))
            
        if cols_to_add:
            for col_name, col_type in cols_to_add:
                print(f"   ➕ Добавление колонки {col_name}...")
                cursor.execute(f"ALTER TABLE request_counter_history ADD COLUMN {col_name} {col_type};")
            print("   ✅ Таблица request_counter_history обновлена.")
        else:
            print("   ✅ Таблица request_counter_history уже актуальна.")

        # --- 2. Обновление sync_flags ---
        print("\n📋 Проверка таблицы sync_flags...")
        sync_cols = get_column_names(cursor, 'sync_flags')
        
        if 'tracking_id' not in sync_cols:
            print("   ➕ Добавление колонки tracking_id...")
            cursor.execute("ALTER TABLE sync_flags ADD COLUMN tracking_id INTEGER;")
            # Можно попробовать заполнить существующие записи, если нужно, но пока оставим NULL
            # Для новых записей это поле будет обязательным в логике кода
            print("   ✅ Таблица sync_flags обновлена.")
        else:
            print("   ✅ Таблица sync_flags уже актуальна.")
            
        # Индекс для tracking_id в sync_flags для скорости
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_sync_flags_tracking ON sync_flags(tracking_id);")

        conn.commit()
        print("\n✅ Миграция успешно завершена!")
        
        # Финальная проверка
        print("\n--- Итоговая структура ключевых таблиц ---")
        for table in ['request_counter_history', 'sync_flags']:
            cols = get_column_names(cursor, table)
            print(f"{table}: {', '.join(cols)}")

    except Exception as e:
        conn.rollback()
        print(f"\n❌ Ошибка миграции: {e}")
        print("   Изменения откатаны. Проверьте логи.")
    finally:
        conn.close()

if __name__ == "__main__":
    migrate()
