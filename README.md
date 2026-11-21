# passrw v2

Telegram-бот и веб-админка для отслеживания наличия билетов на [pass.rw.by](https://pass.rw.by).
Пересобран на async-стеке: один процесс вместо трёх, без файловых флагов и потоков на каждый трекинг.

История изменений — в [CHANGELOG.md](CHANGELOG.md).

## Возможности

**Для пользователя**
- пошаговый поиск: станции с подсказками → календарь (окно продажи из `SALE_WINDOW_DAYS`) → пассажиры → поезд;
- трекинг конкретного поезда; несколько трекингов одновременно;
- уведомление только при изменении набора мест/цены (переживает перезапуск);
- адаптивная частота проверок: чем ближе поездка, тем чаще;
- фильтры: тип вагона, максимальная цена, порог мест;
- тихие часы: проверки продолжаются, уведомления не отправляются; места, появившиеся ночью,
  бот покажет при первой проверке после окончания тихих часов;
- heartbeat-сообщения «трекинг жив»;
- история наличия мест со спарклайном и изменением цены;
- история поисков (повтор в один клик), избранные маршруты, личная статистика;
- понятные причины пустой выдачи: станция не найдена, нет прямого сообщения,
  продажа ещё не открыта, поездов нет.

**Для администратора**
- одна веб-панель с логином по `ADMIN_USERNAME` / `ADMIN_PASSWORD`(HASH); команда `/admin`
  в боте доступна только пользователю с ролью `admin`;
- дашборд: пользователи, трекинги, находки/ошибки за 24 ч, последняя успешная проверка, график замеров;
- список и карточка трекинга: фильтры, остановка, удаление, история замеров;
- управление пользователями: блокировка (снимает трекинги), роли, рассылка;
- логи с честной пагинацией: ошибки бота, события пользователей, аудит действий админов;
- экспорт CSV (с защитой от формул);
- `/healthz` для healthcheck (состояние БД и менеджера) и `/metrics` в формате Prometheus.

**Надёжность**
- ретраи и кэш HTTP-запросов к pass.rw.by, детект капчи;
- парсер с запасными селекторами и отдельным сигналом «вёрстка изменилась»;
- алерт админу после N подряд неудачных проверок;
- SQLite в WAL, все операции через SQLAlchemy 2 async;
- graceful shutdown: отмена задач, закрытие HTTP-клиента и БД.

## Архитектура

```
src/passrw/
  config.py            pydantic-settings конфигурация
  __main__.py          запуск: бот + uvicorn + обслуживание БД в одном asyncio-loop
  bot/                 aiogram 3: роутеры, FSM, клавиатуры, middleware
  services/
    rwby.py            async-клиент pass.rw.by (ретраи, кэш, семафор)
    parser.py          разбор HTML в доменные объекты
    manager.py         один asyncio-таск на трекинг
    notifications.py   тексты уведомлений
  domain/              Train/Carriage/Filters, правила ввода, тихие часы
  db/                  модели, сессии, репозитории
  web/                 FastAPI-админка, Jinja2-шаблоны, статика
scripts/import_legacy.py   миграция старой ticket_bot.db
```

Хранение: SQLite (naive UTC в БД, локальное время — через `LOCAL_TIMEZONE`).
Остановка трекинга не удаляет запись: сохраняются причина и история замеров.

## Быстрый старт

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -e ".[dev]"

cp .env.example .env
# заполните TELEGRAM_TOKEN и ADMIN_PASSWORD, при желании SECRET_KEY

python -m passrw
```

Бот запускается в polling-режиме, админка — на `http://127.0.0.1:5000`.

## Docker

```bash
cp .env.example .env
docker compose up -d --build
```

Один контейнер, non-root пользователь, healthcheck на `/healthz`.
Данные и логи монтируются из `./data` и `./logs`.

## Миграция со старой версии

```bash
python scripts/import_legacy.py --source data/ticket_bot.db --target data/passrw.db
```

Переносятся пользователи, трекинги (активные и остановленные), настройки, избранное,
история поисков, статистика, популярные станции и история наличия мест.
Старая база только читается; существующие записи в новой не перезаписываются.

## Бэкап и восстановление

Резервная копия снимается через SQLite backup API, поэтому корректно учитывает
данные WAL, ещё не перенесённые в основной файл базы. Копия проверяется через
`PRAGMA integrity_check`; существующий файл не перезаписывается без `--force`.

```powershell
python scripts\backup_db.py --source data\passrw.db --dest data\backups\passrw-2026-11-21.db
```

Для встроенного интерпретатора из этого репозитория:

```powershell
.\Python\bin\python.exe scripts\backup_db.py --source data\passrw.db --dest data\backups\passrw-2026-11-21.db
```

Восстановление (PowerShell):

```powershell
# 1. Остановите процесс: Ctrl+C для python -m passrw или `docker compose stop`.
# 2. Замените базу копией.
Copy-Item data\backups\passrw-2026-11-21.db data\passrw.db -Force
# 3. Удалите оставшиеся WAL/SHM-файлы старой базы — иначе они будут применены поверх копии.
Remove-Item data\passrw.db-wal, data\passrw.db-shm -ErrorAction SilentlyContinue
# 4. Запустите процесс снова.
python -m passrw
```

Не копируйте файл базы на работающий процесс: WAL/SHM-файлы должны быть
удалены только после полной остановки, иначе база может быть повреждена.

## Настройки

Все переменные — в `.env.example`. Ключевые:

| Переменная | Назначение |
|---|---|
| `TELEGRAM_TOKEN` | токен бота (обязателен) |
| `ADMIN_PASSWORD` / `ADMIN_PASSWORD_HASH` | пароль админки; hash — argon2, поддерживаются pbkdf2/scrypt из старой версии |
| `SECRET_KEY` | подпись сессий; пусто — генерируется при старте (сессии сбрасываются) |
| `IP_WHITELIST` | список IP через запятую |
| `TRUSTED_PROXIES` | доверенные reverse proxy через запятую; только от них принимается `X-Forwarded-For` |
| `SALE_WINDOW_DAYS` | горизонт продажи билетов для календаря и проверки даты (по умолчанию 60) |
| `ALERT_CHAT_ID` | куда присылать алерты о сбоях |
| `METRICS_TOKEN` | Bearer-токен для `/metrics` |
| `CHECK_INTERVAL`, `MIN_CHECK_INTERVAL` | базовый/минимальный интервал проверок |
| `NOTIFY_COOLDOWN` | антидубль уведомлений, сек |
| `RETENTION_DAYS` | срок хранения логов и истории |

Задать хеш пароля:

```bash
python -c "from argon2 import PasswordHasher; print(PasswordHasher().hash('пароль'))"
```

## Тесты

```bash
pytest
ruff check src tests scripts
```

Тесты покрывают парсер и причины пустой выдачи, правила, репозитории, клавиатуры,
сценарии бота (`search_id`, команды в любом состоянии) и веб-админку (вход, CSRF,
healthz, заголовки, отзыв сессий, ошибки 404/422, пагинация, flash).

## CI и качество

GitHub Actions (`.github/workflows/ci.yml`) запускается на push и pull request:

- job `quality` — `ruff check src tests scripts` и `pytest -q` на Python 3.12 и 3.13;
- job `coverage` — тесты с покрытием на Python 3.12 и порогом 58% (учитываются ветки).

Локально те же проверки:

```bash
ruff check src tests scripts
pytest -q
pytest --cov=passrw --cov-report=term-missing --cov-report=xml
```

Настройки покрытия — в `[tool.coverage.*]` в `pyproject.toml`; непокрытые строки
показывает `term-missing`. В CI отчёт `coverage.xml` сохраняется артефактом
`coverage-xml` в запуске workflow (вкладка Actions → нужный run → Artifacts).

## Безопасность

- пароли — argon2 (или совместимые хеши), логин/CSRF сравниваются без утечки по времени;
- выход из админки и смена пароля/хеша отзывают все активные сессии (эпоха в новой таблице `app_meta`);
- блокировка входа по IP после N неудачных попыток;
- CSRF для всех изменяющих запросов, SameSite=Lax, HttpOnly cookie;
- CSP (`script-src 'none'`), `X-Frame-Options: DENY`, `nosniff`, `no-referrer`,
  `Cache-Control: no-store` для админских страниц, HSTS при `SESSION_COOKIE_SECURE=true`;
- `X-Forwarded-For` учитывается только от адресов из `TRUSTED_PROXIES`, иначе IP берётся
  из реального подключения;
- HTML пользовательских данных экранируется; экспорт CSV защищён от формул;
- валидация станций, дат, идентификаторов записей и числовых фильтров на входе.

## Лицензия

Проект распространяется по лицензии MIT. Полный текст — в файле [LICENSE](LICENSE).
