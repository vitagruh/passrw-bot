# Интеграция Admin Panel с Ticket Bot: Синхронизация трекингов

## Проблема

После удаления трекинга через админ-панель бот продолжал отправлять запросы, так как:
1. Админ-панель удаляла запись только из базы данных
2. Активный поток (`tracking_worker`) в памяти бота не получал сигнал об остановке
3. Счетчик запросов сбрасывался при перезапуске бота
4. При наличии нескольких одинаковых трекингов удалялись все вместо одного

## Решение

Реализована надежная система синхронизации между админ-панелью и ботом на основе лучших практик:

### Архитектурные компоненты

#### 1. Модуль `tracking_sync.py` (единый источник истины)

**Функции:**
- `request_tracking_stop(chat_id, train_time, admin_username)` - запрашивает остановку трекинга
- `confirm_tracking_stopped(chat_id, train_time)` - подтверждает остановку после завершения потока
- `check_stop_request(chat_id, train_time)` - проверяет наличие запроса на остановку
- `is_tracking_active_in_db(chat_id, train_time)` - проверяет существование трекинга в БД
- `force_delete_tracking(chat_id, train_time)` - принудительное удаление (fallback)
- `cleanup_old_sync_flags(max_age_hours)` - очистка старых флагов

**Механизмы синхронизации:**
1. **SQLite таблица `sync_flags`** - хранит флаги действий между процессами
2. **Файлы-флаги в `/tmp/ticket_bot_sync/`** - для мгновенной реакции (IPC)
3. **ACID транзакции** - гарантируют консистентность данных

#### 2. Изменения в `ticket_bot.py`

**Импорты:**
```python
from tracking_sync import (
    check_stop_request,
    is_tracking_active_in_db,
    confirm_tracking_stopped,
    cleanup_old_sync_flags,
    get_pending_sync_actions
)
```

**Обновленный `tracking_worker`:**
```python
def tracking_worker(chat_id, from_station, to_station, date, selected_time):
    while chat_id in active_jobs:
        try:
            # === ПРОВЕРКА СИНХРОНИЗАЦИИ С АДМИН-ПАНЕЛЬЮ ===
            # Проверяем наличие запроса на остановку от админ-панели
            if check_stop_request(chat_id, selected_time):
                logger.info(f"🛑 Получен запрос на остановку трекинга от админ-панели")
                break
            
            # Проверяем существование трекинга в БД
            if not is_tracking_active_in_db(chat_id, selected_time):
                logger.warning(f"⚠️ Трекинг удален из БД напрямую. Завершаем поток.")
                break
            # ==================================================
            
            # ... основная логика трекинга ...
    
    # === ГРАЦИОЗНОЕ ЗАВЕРШЕНИЕ ПОТОКА ===
    confirm_tracking_stopped(chat_id, selected_time)
```

**Ключевые изменения:**
1. Проверка флагов остановки в каждом цикле
2. Проверка существования записи в БД (защита от прямого удаления)
3. Graceful shutdown с подтверждением остановки
4. Сохранение счетчика запросов при восстановлении трекингов
5. Удаление по уникальному ID трекинга (не по train_time)

**Обновленный `restore_active_trackings`:**
```python
def restore_active_trackings(bot_instance):
    # ... восстановление трекингов ...
    
    # Восстанавливаем статус трекинга с сохранением счетчика запросов из БД
    tracking_status[chat_id] = {
        'train_num': tracking['train_num'],
        'train_time': tracking['train_time'],
        'seats_available': tracking['seats_available'],
        'requests_count': tracking['requests_count'] if tracking['requests_count'] else 0,
        'id': tracking['id']  # Уникальный ID трекинга
    }
    
    # Очищаем старые флаги синхронизации при старте
    cleanup_old_sync_flags(max_age_hours=24)
```

#### 3. Изменения в `admin_panel.py`

**Импорты:**
```python
from tracking_sync import (
    request_tracking_stop,
    force_delete_tracking,
    create_sync_table
)

# Инициализация таблицы синхронизации при старте
create_sync_table()
```

**Обновленный `delete_tracking`:**
```python
@app.route('/tracking/<int:tracking_id>/delete', methods=['POST'])
@login_required
def delete_tracking(tracking_id):
    """Удаление трекинга с синхронизацией с ботом"""
    tracking = get_tracking_by_id(tracking_id)
    if tracking:
        chat_id = tracking['chat_id']
        train_time = tracking['train_time']
        
        # Сначала запрашиваем остановку через систему синхронизации
        if request_tracking_stop(chat_id, train_time, session.get('admin_username')):
            log_admin_action(
                session['admin_username'], 
                'DELETE_TRACKING', 
                f'Запрошена остановка трекинга #{tracking_id}'
            )
            flash('✅ Запрос на остановку трекинга отправлен', 'success')
        else:
            # Fallback: принудительное удаление
            if force_delete_tracking(chat_id, train_time):
                flash('⚠️ Трекинг удален принудительно', 'warning')
```

## Как это работает

### Сценарий 1: Корректная остановка через админ-панель

1. **Админ-панель:**
   - Пользователь нажимает "Удалить трекинг"
   - Вызывается `request_tracking_stop(chat_id, train_time)`
   - Создается запись в таблице `sync_flags`
   - Создается файл-флаг `/tmp/ticket_bot_sync/stop_{chat_id}_{train_time}.flag`

2. **Ticket-bot:**
   - В следующем цикле `tracking_worker` вызывает `check_stop_request()`
   - Обнаруживает файл-флаг или запись в БД
   - Прерывает цикл `while`
   - Выполняется graceful shutdown:
     - Очистка in-memory данных (`active_jobs`, `tracking_status`)
     - Вызов `confirm_tracking_stopped()`
     - Удаление записи из БД по уникальному ID
     - Удаление файла-флага

3. **Результат:**
   - Поток остановлен корректно
   - Данные синхронизированы
   - Счетчик запросов сохранен в логах
   - Удален только конкретный трекинг (даже если есть дубликаты)

### Сценарий 2: Принудительное удаление (fallback)

Если бот недоступен или произошла ошибка:

1. **Админ-панель:**
   - `request_tracking_stop()` возвращает `False`
   - Вызывается `force_delete_tracking()`
   - Удаляется запись из БД
   - Создается флаг `FORCE_STOP`

2. **Ticket-bot:**
   - При следующей проверке `is_tracking_active_in_db()` возвращает `False`
   - Поток завершается
   - Записи в БД уже нет (удалено принудительно)

### Сценарий 3: Перезапуск бота

1. **При старте:**
   - `restore_active_trackings()` загружает трекинги из БД
   - Восстанавливается `requests_count` из БД
   - Выполняется `cleanup_old_sync_flags()`

2. **Результат:**
   - Счетчик запросов не сбрасывается
   - Все активные трекинги продолжают работу

### Сценарий 4: Удаление пользователем через бота

1. **Пользователь:**
   - Отправляет команду `/mytracks`
   - Видит список своих трекингов с кнопками "❌ Удалить трек №X"
   - Нажимает кнопку удаления конкретного трекинга

2. **Ticket-bot:**
   - Извлекает `tracking_id` из callback data
   - Удаляет трекинг по ID через `remove_tracking_from_db(chat_id, tracking_id=tracking_id)`
   - Очищает in-memory данные по ID
   - Отправляет подтверждение пользователю

3. **Результат:**
   - Удален только выбранный трекинг
   - Даже при наличии дубликатов с одинаковым поездом и датой

## Таблица синхронизации

```sql
CREATE TABLE sync_flags (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    train_time TEXT NOT NULL,
    action TEXT NOT NULL,  -- 'STOP', 'UPDATE', 'RESUME', 'FORCE_STOP'
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    processed BOOLEAN DEFAULT 0,
    processed_at TIMESTAMP,
    UNIQUE(chat_id, train_time, action, created_at)
);
```

## Best Practices реализованные в решении

1. **Single Source of Truth** - SQLite БД как единый источник истины
2. **ACID транзакции** - атомарность операций синхронизации
3. **Graceful Shutdown** - корректное завершение потоков
4. **Idempotency** - повторные вызовы функций безопасны
5. **Fallback механизм** - принудительное удаление если основной способ не сработал
6. **Logging & Auditing** - все действия логируются с указанием администратора
7. **Cleanup** - автоматическая очистка старых флагов
8. **Persistence** - сохранение состояния между перезапусками

## Тестирование

### Проверка работы синхронизации:

```bash
# 1. Запустить бота
python ticket_bot.py

# 2. Запустить админ-панель
python admin_panel.py

# 3. Создать трекинг через Telegram бота

# 4. Удалить трекинг через админ-панель

# 5. Проверить логи бота:
# - Должно быть сообщение "🛑 Получен запрос на остановку трекинга"
# - Должно быть сообщение "🏁 Завершение потока трекинга"
# - Поток должен остановиться в течение CHECK_INTERVAL секунд
```

### Проверка сохранения счетчика запросов:

```bash
# 1. Посмотреть requests_count в БД до перезапуска
sqlite3 data/ticket_bot.db "SELECT chat_id, train_time, requests_count FROM active_trackings;"

# 2. Перезапустить бота

# 3. Посмотреть requests_count после перезапуска
# Значение должно сохраниться
```

## Мониторинг

### Логи бота:
- `🛑 Получен запрос на остановку трекинга от админ-панели`
- `⚠️ Трекинг удален из БД напрямую. Завершаем поток.`
- `🏁 Завершение потока трекинга для {chat_id}:{train_time}`
- `✅ Трекинг {chat_id}:{train_time} остановлен и удален из БД`

### Логи админ-панели:
- `Запрошена остановка трекинга #{id} для пользователя {chat_id}`
- `Принудительно удален трекинг #{id} для пользователя {chat_id}`

## Заключение

Реализованная система синхронизации обеспечивает:
- ✅ Немедленную остановку трекингов после удаления через админ-панель
- ✅ Сохранение счетчика запросов между перезапусками
- ✅ Надежную межпроцессную коммуникацию
- ✅ Graceful shutdown потоков
- ✅ Полное логирование и аудит действий
