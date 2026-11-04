FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    DATABASE_PATH=/app/data/ticket_bot.db \
    LOCAL_TIMEZONE=Europe/Minsk

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY db.py ticket_bot.py admin_panel.py tracking_sync.py ./

RUN mkdir -p /app/data /app/logs \
    && useradd --create-home --uid 10001 appuser \
    && chown -R appuser:appuser /app

USER appuser

EXPOSE 5000

# По умолчанию запускается админ-панель; бот запускается через docker-compose
CMD ["python", "admin_panel.py"]
