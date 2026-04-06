#!/usr/bin/env python3
"""
Тестовый скрипт для проверки системы логирования.
Запустите: python test_logging.py
"""

import os
import sys
import logging
from logging.handlers import RotatingFileHandler
import traceback


# ============================================
# НАСТРОЙКА ЛОГИРОВАНИЯ (BEST PRACTICES)
# ============================================

def setup_logger(name: str = 'TestLogger') -> logging.Logger:
    """
    Создает и настраивает логгер с консольным и файловым выводом.
    
    Best practices:
    - RotatingFileHandler для автоматической ротации логов
    - Структурированный формат с информацией о месте вызова
    - Разделение уровней ERROR/CRITICAL в отдельный файл
    - Поддержка уровня логирования через переменную окружения
    """
    logger = logging.getLogger(name)
    logger.setLevel(logging.NOTSET)  # Устанавливаем минимальный уровень, фильтрация будет на хендлерах
    
    # Получаем уровень логирования из переменной окружения (по умолчанию INFO)
    log_level = os.getenv('LOG_LEVEL', 'DEBUG').upper()
    numeric_level = getattr(logging, log_level, logging.DEBUG)
    
    # Формат с детальной информацией (время, уровень, модуль, функция, строка, сообщение)
    detailed_formatter = logging.Formatter(
        fmt='%(asctime)s | %(levelname)-8s | %(name)s | %(funcName)s:%(lineno)d | %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    
    # Упрощенный формат для консоли (короче и читаемее)
    console_formatter = logging.Formatter(
        fmt='%(asctime)s | %(levelname)-8s | %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    
    # Консольный обработчик (выводит всё от DEBUG и выше)
    console_handler = logging.StreamHandler()
    console_handler.setLevel(numeric_level)
    console_handler.setFormatter(console_formatter)
    logger.addHandler(console_handler)
    
    # Файловый обработчик с ротацией
    # maxBytes=10MB, backupCount=5 файлов (итого до 50MB логов)
    log_file = os.getenv('LOG_FILE', 'logs/bot.log')
    log_dir = os.path.dirname(log_file)
    if log_dir and not os.path.exists(log_dir):
        os.makedirs(log_dir, exist_ok=True)
    
    file_handler = RotatingFileHandler(
        filename=log_file,
        encoding='utf-8',
        maxBytes=10 * 1024 * 1024,  # 10 MB
        backupCount=5,
        delay=True  # Создавать файл только при первой записи
    )
    file_handler.setLevel(logging.DEBUG)  # В файл пишем всё от DEBUG
    file_handler.setFormatter(detailed_formatter)
    logger.addHandler(file_handler)
    
    # Отдельный файл для ошибок (ERROR и CRITICAL)
    error_log_file = os.getenv('ERROR_LOG_FILE', 'logs/error.log')
    error_log_dir = os.path.dirname(error_log_file)
    if error_log_dir and not os.path.exists(error_log_dir):
        os.makedirs(error_log_dir, exist_ok=True)
    
    error_handler = RotatingFileHandler(
        filename=error_log_file,
        encoding='utf-8',
        maxBytes=5 * 1024 * 1024,  # 5 MB
        backupCount=3,
        delay=True
    )
    error_handler.setLevel(logging.ERROR)
    error_handler.setFormatter(detailed_formatter)
    logger.addHandler(error_handler)
    
    # Запрещаем распространение логов вверх по иерархии
    logger.propagate = False
    
    return logger


# Создаем тестовый логгер
logger = setup_logger('TestLogger')


def main():
    print("=" * 60)
    print("ТЕСТИРОВАНИЕ СИСТЕМЫ ЛОГИРОВАНИЯ")
    print("=" * 60)
    
    # Тестируем все уровни логирования
    logger.debug("🔍 DEBUG: Отладочное сообщение (видно только при LOG_LEVEL=DEBUG)")
    logger.info("ℹ️ INFO: Информационное сообщение о событии")
    logger.warning("⚠️ WARNING: Предупреждение о потенциальной проблеме")
    logger.error("❌ ERROR: Ошибка выполнения операции")
    logger.critical("🚨 CRITICAL: Критическая ошибка, требующая немедленного внимания")
    
    # Тестируем логирование с исключением
    try:
        raise ValueError("Тестовое исключение для проверки traceback")
    except Exception:
        logger.exception("Произошло ожидаемое исключение при тестировании")
    
    print("\n" + "=" * 60)
    print("✅ Все уровни логирования протестированы!")
    print("=" * 60)
    print(f"\n📁 Проверьте файлы логов:")
    print(f"   - Основной лог: {os.getenv('LOG_FILE', 'logs/bot.log')}")
    print(f"   - Лог ошибок: {os.getenv('ERROR_LOG_FILE', 'logs/error.log')}")
    print("\n💡 Совет: установите LOG_LEVEL=DEBUG в .env для отладочных сообщений")


if __name__ == "__main__":
    main()
