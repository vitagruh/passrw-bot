"""Единая настройка логирования для бота, воркеров и веб-админки."""

from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path

from passrw.config import Settings

_CONFIGURED = False


def setup_logging(settings: Settings) -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return
    _CONFIGURED = True

    level = getattr(logging, settings.log_level, logging.INFO)
    log_dir = Path(settings.log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)

    detailed = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)s | %(funcName)s:%(lineno)d | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    console = logging.Formatter("%(asctime)s | %(levelname)-8s | %(name)s | %(message)s", "%H:%M:%S")

    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()

    stream = logging.StreamHandler(sys.stdout)
    stream.setLevel(level)
    stream.setFormatter(console)
    root.addHandler(stream)

    app_file = logging.handlers.RotatingFileHandler(
        log_dir / "bot.log", maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    app_file.setLevel(level)
    app_file.setFormatter(detailed)
    root.addHandler(app_file)

    error_file = logging.handlers.RotatingFileHandler(
        log_dir / "error.log", maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    error_file.setLevel(logging.ERROR)
    error_file.setFormatter(detailed)
    root.addHandler(error_file)

    for noisy in ("aiogram.event", "httpx", "uvicorn.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
