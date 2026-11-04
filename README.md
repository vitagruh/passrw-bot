# Telegram-бот отслеживания билетов БЖД

Бот следит за наличием мест на [pass.rw.by](https://pass.rw.by) и присылает уведомления, когда появляются подходящие билеты. В комплекте — веб-админка на Flask и общий слой SQLite.

## Возможности

- Пошаговый поиск: станция отправления → назначение → дата (inline-календарь) → пассажиры.
- Подсказки станций по мере ввода и защита от опечаток.
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
- Автоочистка логов/истории и восстановление трекингов после перезапуска.

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
py -3.12 -m venv .venv
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
py -3.12 -m venv .venv
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
| `CHECK_INTERVAL` | Интервал проверки, сек | `60` |
| `MAX_TRACKINGS_PER_USER` | Лимит трекингов | `5` |
| `NOTIFY_COOLDOWN` | Пауза между уведомлениями, сек | `300` |
| `STOP_ON_SUCCESS` | Останавливать трекинг после находки | `true` |
| `MAINTENANCE_INTERVAL` | Период очистки БД, сек | `21600` |
| `RETENTION_DAYS` | Срок хранения логов/истории, дни | `30` |
| `LOCAL_TIMEZONE` | Часовой пояс вывода | `Europe/Minsk` |
| `DATABASE_PATH` | Путь к SQLite | `data/ticket_bot.db` |
| `ADMIN_USERNAME` | Логин админки | `admin` |
| `ADMIN_PASSWORD` / `ADMIN_PASSWORD_HASH` | Пароль или его хеш | вход запрещён |
| `SECRET_KEY` | Ключ сессий Flask | временный |
| `SESSION_TIMEOUT_MINUTES` | Авто-logout | `60` |
| `LOGIN_MAX_ATTEMPTS` / `LOGIN_LOCKOUT_SECONDS` | Защита входа | `5` / `900` |
| `IP_WHITELIST` | Разрешённые IP через запятую | без ограничений |
| `METRICS_TOKEN` | Токен для `/metrics` | без токена |

## Тесты и CI

```bash
pip install -r requirements-dev.txt
python -m pytest -q
python -m pyflakes ticket_bot.py admin_panel.py tracking_sync.py db.py tests
```

Покрыты: схема и миграции БД, время/часовой пояс, мягкая остановка и sync-флаги, валидация и парсер, мультитрекинг, настройки/тихие часы/тренды, маршруты админки и CSRF. CI — `.github/workflows/ci.yml` (Python 3.11–3.13).

## Docker

```bash
cp .env.example .env   # заполните секреты
docker compose up -d --build
```

`docker-compose.yml` поднимает бота и админку, общий volume для `data/` и `logs/`. Админка проброшена только на `127.0.0.1:5000` — наружу публикуйте через reverse proxy с HTTPS.

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
├── .env.example
├── data/                  # SQLite (создаётся автоматически)
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
