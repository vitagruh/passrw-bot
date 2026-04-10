# 📊 Миграция Базы Данных - Краткое Руководство

## ✅ Выполненные Изменения

### 1. Структура Базы Данных

#### Новые поля в таблице `active_trackings`:
- **`is_stopped`** (BOOLEAN) - флаг graceful shutdown для корректной остановки потоков
- **`last_request_count`** (INTEGER) - сохранение последнего значения счетчика запросов
- **`unique_token`** (TEXT) - уникальный токен для каждого трекинга (гарантирует абсолютную уникальность)

#### Новая таблица `request_counter_history`:
```sql
CREATE TABLE request_counter_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tracking_id INTEGER NOT NULL,
    chat_id INTEGER NOT NULL,
    requests_count INTEGER DEFAULT 0,
    recorded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    reason TEXT  -- 'periodic_save', 'tracking_stop', 'migration'
)
```

### 2. Уникальная Идентификация Трекингов

**Проблема решена:** Раньше при удалении трекинга удалялись все дубликаты с одинаковыми параметрами.

**Решение:**
- Каждый трекинг имеет уникальный `id` (AUTOINCREMENT)
- Генерируется `unique_token` при создании: `{uuid}_{timestamp}`
- Все операции используют `tracking_id` для точной идентификации

### 3. Сохранение Счетчиков Запросов

**Проблема решена:** При добавлении трека с такими же параметрами счетчик обнулялся.

**Решение:**
- Счетчик `requests_count` сохраняется в БД при каждом обновлении
- Дополнительно копируется в `last_request_count`
- При остановке трекинга данные сохраняются в `request_counter_history`

## 🔧 Обновленный Функционал

### ticket_bot.py

#### save_tracking_to_db()
```python
# Генерирует уникальный токен
unique_token = f"{uuid.uuid4().hex[:16]}_{int(time.time())}"
# Возвращает tracking_id для последующего использования
return cursor.lastrowid
```

#### update_tracking_status()
```python
# Поддержка tracking_id для точного обновления
update_tracking_status(chat_id, train_time, seats, train_num, requests_count, 
                       tracking_id=tracking_id)
```

#### remove_tracking_from_db()
```python
# Удаляет только конкретный трек по ID
remove_tracking_from_db(chat_id, train_time, tracking_id=tracking_id)
```

#### on_stop_tracking_choice()
```python
# Извлечение tracking_id из callback_data
tracking_id = int(call.data.replace("stop_tracking_", ""))
# Точное удаление конкретного трекинга
remove_tracking_from_db(chat_id, train_time, tracking_id=tracking_id)
```

### tracking_sync.py

#### confirm_tracking_stopped()
```python
# Принимает tracking_id для точного удаления
confirm_tracking_stopped(chat_id, selected_time, tracking_id=tracking_id)
```

## 🚀 Как Использовать

### Для Пользователей Бота

1. **Запуск трекинга:** Без изменений, бот автоматически генерирует unique_token
2. **Остановка трекинга:** 
   - Команда `/stop` показывает список с кнопками
   - Каждая кнопка содержит уникальный ID трекинга
   - Удаляется только выбранный трек, даже если есть дубликаты

### Для Администраторов

1. **Админ-панель:** Использует `tracking_id` для всех операций
2. **Удаление через админку:**
   ```python
   request_tracking_stop(chat_id, train_time, admin_username, tracking_id)
   ```
3. **Просмотр дубликатов:**
   ```sql
   SELECT * FROM duplicate_trackings;
   ```

## 📈 Мониторинг

### Проверка дубликатов:
```sql
SELECT * FROM duplicate_trackings;
```

### История счетчиков:
```sql
SELECT tracking_id, chat_id, requests_count, recorded_at, reason
FROM request_counter_history
ORDER BY recorded_at DESC
LIMIT 10;
```

### Активные трекинги с токенами:
```sql
SELECT id, chat_id, train_time, unique_token, requests_count, is_stopped
FROM active_trackings
WHERE is_stopped = 0;
```

## ⚠️ Важные Замечания

1. **Обратная совместимость:** Код поддерживает старые записи без tracking_id через fallback-механизмы
2. **Graceful Shutdown:** Потоки корректно завершаются при получении флага остановки
3. **Транзакционность:** Все операции с БД выполняются в транзакциях (ACID)
4. **Логирование:** Все действия синхронизации логируются для отладки

## 🎯 Принципы Транзакций (ACID)

- **Atomicity (Атомарность):** Все операции выполняются полностью или не выполняются вообще
- **Consistency (Согласованность):** Данные всегда валидны благодаря foreign keys и проверкам
- **Isolation (Изоляция):** Транзакции изолированы через контекстные менеджеры
- **Durability (Долговечность):** Данные сохраняются немедленно после commit

## 📝 Следующие Шаги

1. ✅ Миграция выполнена
2. 🔄 Перезапустите `ticket_bot.py`
3. 🔄 Перезапустите `admin_panel.py`
4. 🧪 Протестируйте создание/удаление трекингов
5. 🧪 Проверьте что удаляется только один конкретный трек
6. 🧪 Убедитесь что счетчики сохраняются

---
**Дата миграции:** 2024
**Статус:** ✅ Успешно
