# Telegram-бот отслеживания билетов БЖД

Бот следит за наличием мест на [pass.rw.by](https://pass.rw.by) и присылает уведомления, когда появляются подходящие билеты. В комплекте — веб-админка на Flask и общий слой SQLite.

## Возможности

- Пошаговый поиск: станция отправления → назначение → дата (inline-календарь) → пассажиры.
- Подсказки станций после ввода названия (inline-кнопки) и защита от опечаток.
- Мультитрекинг: до 5 поездов на пользователя, каждый со своим потоком и heartbeat.
- Умные уведомления: дедупликация, повтор только при изменении мест/цены, cooldown.
- Настройки пользователя (`/settings`):
  - тихие часы (например `22:00-08:00`);
  - тип вагона (плацкарт, купе, СВ, сидячий, люкс);
  - максимальная цена;
  - порог количества мест;
  - пассажиры по умолчанию.
- История наличия мест и цен (`📈 История` в `/status`) со спарклайном.
- Шаринг маршрута (`🔗 Поделиться`) и кнопка покупки на RW.BY.
- История поисков, избранные маршруты, личная статистика.
- Админ-панель: трекинги, пользователи, статистика, CSV-экспорт, мониторинг, heartbeat.
- Безопасность: CSRF, rate limiting входа, блокировка после неудачных попыток, session timeout, IP whitelist, хеш пароля, CSV-injection защита.
- Служебные эндпоинты `/healthz` и `/metrics` (Prometheus).
- Автоочистка записей логов в БД и истории по `RETENTION_DAYS`, ротация `bot.log` и `error.log`.
- Восстановление активных трекингов после перезапуска.

## Требования

- Python 3.11+
- Telegram Bot Token от [@BotFather](https://t.me/BotFather)

## Установка

### Windows (PowerShell)

1. Установите Python 3.11+ с [python.org](https://www.python.org/downloads/windows/) и отметьте **«Add python.exe to PATH»**.
2. Установите [Git for Windows](https://git-scm.com/download/win) (нужен для клонирования).
3. Выполните в PowerShell:

```powershell
git clone https://github.com/vitagruh/passrw-bot.git
cd passrw-bot
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
Copy-Item .env.example .env
```

Если PowerShell блокирует активацию окружения, разрешите скрипты для текущего пользователя:

```powershell
Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned
```

В `cmd.exe` активация выглядит иначе:

```bat
py -3 -m venv .venv
.venv\Scripts\activate.bat
pip install -r requirements.txt
copy .env.example .env
```

### Linux (Debian/Ubuntu)

```bash
sudo apt update
sudo apt install -y git python3 python3-venv python3-pip
git clone https://github.com/vitagruh/passrw-bot.git
cd passrw-bot
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
cp .env.example .env
chmod 600 .env
```

Для Fedora/RHEL/openSUSE замените первую команду на
`sudo dnf install -y git python3 python3-pip` (в openSUSE — `sudo zypper install -y git python3 python3-pip`).

### Linux как systemd-сервис (опционально)

Создайте `/etc/systemd/system/passrw-bot.service` (пути и пользователя подставьте свои):

```ini
[Unit]
Description=PassRW Telegram bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=passrw
WorkingDirectory=/opt/passrw-bot
EnvironmentFile=/opt/passrw-bot/.env
ExecStart=/opt/passrw-bot/.venv/bin/python /opt/passrw-bot/ticket_bot.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now passrw-bot
journalctl -u passrw-bot -f
```

Отдельный unit для админки делается так же, с `ExecStart=.../python .../admin_panel.py`.

## Настройка

Заполните `.env`:

```env
TELEGRAM_TOKEN=ваш_токен
ADMIN_PASSWORD=сложный_пароль
# Рекомендуется вместо пароля:
# ADMIN_PASSWORD_HASH=...
SECRET_KEY=случайная_hex_строка
```

Сгенерировать хеш и ключ:

```bash
python -c "from werkzeug.security import generate_password_hash; print(generate_password_hash('пароль'))"
python -c "import secrets; print(secrets.token_hex(32))"
```

## Запуск

```bash
python ticket_bot.py     # бот
python admin_panel.py    # админка: http://127.0.0.1:5000
```

В Windows команды те же — при активированном `.venv` (`.\.venv\Scripts\Activate.ps1` или `.venv\Scripts\activate.bat`).

Каталоги `data/` и `logs/` создаются автоматически. Схема БД инициализируется и мигрируется модулем `db.py`.

## Команды бота

| Команда | Описание |
|---|---|
| `/start` | Приветствие и главное меню |
| `/track` | Новый поиск и трекинг |
| `/status` | Статус трекингов, история мест, шаринг |
| `/mytracks` | Список трекингов с удалением |
| `/stop` | Остановить трекинг(и) |
| `/history` | История поисков и повтор |
| `/favorites` | Избранные маршруты |
| `/stats` | Личная статистика |
| `/settings` | Тихие часы, фильтры, пассажиры |
| `/help` | Справка |
| `/admin` | Админ-меню (роль `admin`/`moderator`) |

## Настройки `.env`

Основные переменные (полный список — в `.env.example`):

| Переменная | Назначение | По умолчанию |
|---|---|---|
| `TELEGRAM_TOKEN` | Токен бота | обязателен |
| `CHECK_INTERVAL` | Интервал проверки, сек (не менее 10) | `60` |
| `HTTP_CACHE_TTL` | Кеш запросов к pass.rw.by, сек (`0` — выключить) | `30` |
| `REQUEST_TIMEOUT` | Таймаут запроса к сайту, сек | `15` |
| `MAX_PASSENGERS` | Максимум пассажиров в поиске | `9` |
| `MAX_TRACKINGS_PER_USER` | Лимит трекингов | `5` |
| `NOTIFY_COOLDOWN` | Пауза между уведомлениями, сек | `300` |
| `STOP_ON_SUCCESS` | Останавливать трекинг после находки | `true` |
| `MAINTENANCE_INTERVAL` | Период очистки БД, сек (не менее 300) | `21600` |
| `RETENTION_DAYS` | Срок хранения записей логов/истории, дни | `30` |
| `LOCAL_TIMEZONE` | Часовой пояс вывода | `Europe/Minsk` |
| `DATABASE_PATH` | Путь к SQLite | `data/ticket_bot.db` |
| `SYNC_DIR` | Каталог флагов мягкой остановки | `data/sync` |
| `ADMIN_USERNAME` | Логин админки | `admin` |
| `ADMIN_PASSWORD` / `ADMIN_PASSWORD_HASH` | Пароль или его хеш | вход запрещён |
| `SECRET_KEY` | Ключ сессий Flask | временный, генерируется при старте |
| `FLASK_HOST` / `FLASK_PORT` | Адрес и порт админки | `127.0.0.1` / `5000` |
| `SESSION_COOKIE_SECURE` | Только HTTPS-cookie (за reverse proxy) | `false` |
| `SESSION_TIMEOUT_MINUTES` | Авто-logout | `60` |
| `LOGIN_MAX_ATTEMPTS` / `LOGIN_LOCKOUT_SECONDS` | Защита входа | `5` / `900` |
| `RATE_LIMIT_REQUESTS` / `RATE_LIMIT_WINDOW` | Лимит запросов к админке | `100` / `60` |
| `IP_WHITELIST` | Разрешённые IP через запятую | без ограничений |
| `METRICS_TOKEN` | Токен для `/metrics` | публичный |

## Тесты и CI

```bash
pip install -r requirements-dev.txt
python -m pytest -q
python -m pyflakes ticket_bot.py admin_panel.py tracking_sync.py db.py tests
```

Покрыты: схема и миграции БД, время/часовой пояс, мягкая остановка и sync-флаги, валидация и парсер, мультитрекинг, настройки/тихие часы/тренды, маршруты админки и CSRF. CI — `.github/workflows/ci.yml` (Python 3.11–3.13).

## Docker

### Требования

- Docker Engine 24+ и Docker Compose v2 (`docker compose`, не устаревший `docker-compose`).
- Свободный порт `5000` на localhost (его занимает админка).
- Реальный `TELEGRAM_TOKEN`: с фиктивным токеном контейнер бота завершится с ошибкой 401.

### Подготовка

```bash
cp .env.example .env
chmod 600 .env          # Linux/macOS; на Windows настройте ACL
```

Заполните в `.env` как минимум:

```env
TELEGRAM_TOKEN=токен_от_BotFather
ADMIN_PASSWORD_HASH=...   # или ADMIN_PASSWORD
SECRET_KEY=случайная_hex_строка
METRICS_TOKEN=...          # желательно, если /metrics доступен не только локально
```

Файл `.env` не попадает в образ (исключён в `.dockerignore`) и передаётся контейнерам через `env_file`.

### Запуск

```bash
docker compose up -d --build
docker compose ps
```

`docker-compose.yml` описывает два сервиса:

| Сервис | Команда | Порты | Назначение |
|---|---|---|---|
| `bot` | `python ticket_bot.py` | — | опрос Telegram и трекинги |
| `admin` | `python admin_panel.py` | `127.0.0.1:5000` | Flask-админка (waitress) |

Оба сервиса собираются из одного `Dockerfile` (`python:3.12-slim`) и работают от непривилегированного пользователя `appuser` (uid 10001).

### Данные и логи

| Volume | Точка монтирования | Содержимое |
|---|---|---|
| `bot_data` | `/app/data` | `ticket_bot.db`, каталог `sync/` |
| `bot_logs` | `/app/logs` | `bot.log`, `error.log`, `admin.log` |

Volume сохраняются между перезапусками и пересборками. Команда `docker compose down -v` **удаляет их вместе с базой и историей трекингов** — используйте её только осознанно.

### Проверка состояния

```bash
curl http://127.0.0.1:5000/healthz
# {"db":"ok","status":"ok","trackings":0,"uptime_seconds":...,"users":0}

curl -H "Authorization: Bearer $METRICS_TOKEN" http://127.0.0.1:5000/metrics
```

`/healthz` публичный и возвращает `503`, если БД недоступна. Если `METRICS_TOKEN` задан, запрос к `/metrics` без заголовка `Authorization: Bearer ...` получает `403`; если токен не задан, метрики публичны.

### Обслуживание

```bash
docker compose logs -f bot       # логи бота
docker compose logs -f admin     # логи админки
docker compose restart admin     # перезапуск одного сервиса
git pull && docker compose up -d --build   # обновление
docker compose stop              # остановить без удаления контейнеров
docker compose down              # остановить и удалить контейнеры (volume остаются)
```

Проверить итоговую конфигурацию без запуска:

```bash
docker compose config
```

Команда печатает переменные окружения, включая секреты из `.env`, — не публикуйте её вывод.

### Безопасность

- Админка слушает только `127.0.0.1`; наружу публикуйте через nginx/Caddy с HTTPS.
- Задайте `METRICS_TOKEN`, если метрики читает внешняя система.
- Не передавайте секреты через `docker build --build-arg` и не копируйте `.env` в образ.
- Контейнеры не используют `privileged` и root; не добавляйте лишние capabilities.

### Если что-то не работает

- **Бот перезапускается с ошибкой 401** — неверный или отозванный `TELEGRAM_TOKEN`; проверьте `docker compose logs bot`.
- **`port is already allocated`** — порт 5000 занят; освободите его или измените проброс в `docker-compose.yml`.
- **Админка долго в статусе `health: starting`** — healthcheck выполняется раз в 30 секунд, подождите до минуты.
- **`database is locked`** — не запускайте несколько контейнеров бота на одном volume; SQLite работает в WAL, но один писатель должен быть один.

## Структура

```
.
├── ticket_bot.py          # бот: хендлеры, парсер, потоки трекингов
├── admin_panel.py         # Flask-админка (waitress)
├── db.py                  # единая схема, миграции, время, очистка
├── tracking_sync.py       # мягкая остановка бота из админки (SQLite + флаги)
├── requirements.txt
├── requirements-dev.txt
├── Dockerfile
├── docker-compose.yml
├── .dockerignore
├── .github/workflows/ci.yml  # pytest + pyflakes на Python 3.11–3.13
├── .env.example
├── LICENSE
├── data/                  # SQLite и sync/ (создаётся автоматически)
├── logs/                  # логи (создаётся автоматически)
└── tests/                 # pytest
```

## Безопасность

- `.env` и `__pycache__` исключены из git; секреты храните только в окружении/secret manager.
- Задайте `SECRET_KEY`, `ADMIN_PASSWORD_HASH` и при необходимости `IP_WHITELIST`.
- Не публикуйте админку напрямую в интернет — используйте HTTPS reverse proxy.
- Если `.env` когда-либо попадал в публичный репозиторий, смените токен, пароль и `SECRET_KEY`.

## Устранение неполадок

- **Бот не отвечает** — проверьте `logs/bot.log` и `logs/error.log`, токен и доступ к сети.
- **Поезда не найдены** — проверьте названия станций и дату; сайт может менять вёрстку.
- **Не работает админка** — проверьте `ADMIN_PASSWORD`/`ADMIN_PASSWORD_HASH`, `SECRET_KEY` и `logs/admin.log`.
- **Занята БД** — SQLite работает в WAL; не запускайте несколько копий бота на одном файле.

## Лицензия

MIT — см. [LICENSE](LICENSE).
