import logging
import sys
from pathlib import Path
from logging.handlers import RotatingFileHandler

def setup_logger(name: str = "train_bot", log_dir: str = "logs") -> logging.Logger:
    """
    Настраивает основательное логирование по best practices:
    - Консоль (INFO+)
    - Файл all.log (INFO+, ротация)
    - Файл error.log (ERROR+, ротация)
    """
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)  # Базовый уровень, фильтруется хендлерами

    # Создаем директорию для логов
    log_path = Path(log_dir)
    log_path.mkdir(exist_ok=True)

    # Форматы
    detailed_format = logging.Formatter(
        '%(asctime)s | %(levelname)-8s | %(name)s | %(funcName)s:%(lineno)d | %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    console_format = logging.Formatter(
        '%(asctime)s | %(levelname)-8s | %(message)s',
        datefmt='%H:%M:%S'
    )

    # 1. Консольный хендлер (INFO и выше)
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(console_format)

    # 2. Файловый хендлер (все логи INFO+) с ротацией
    # maxBytes=10MB, backupCount=5
    file_handler = RotatingFileHandler(
        log_path / "all.log",
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding='utf-8'
    )
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(detailed_format)

    # 3. Файловый хендлер только для ошибок (ERROR+) с ротацией
    # maxBytes=5MB, backupCount=3
    error_handler = RotatingFileHandler(
        log_path / "error.log",
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
        encoding='utf-8'
    )
    error_handler.setLevel(logging.ERROR)
    error_handler.setFormatter(detailed_format)

    # Добавляем хендлеры
    logger.addHandler(console_handler)
    logger.addHandler(file_handler)
    logger.addHandler(error_handler)

    # Запрещаем всплытие логов в корневой логгер (чтобы не дублировались)
    logger.propagate = False

    return logger

# Глобальный экземпляр логгера
logger = setup_logger()
