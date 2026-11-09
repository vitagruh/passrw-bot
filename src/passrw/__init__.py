"""PassRW — мониторинг билетов БЖД с Telegram-ботом и веб-админкой."""

import platform
import sys

# Python 3.15 получает версию Windows через WMI; при зависшей службе WMI
# platform.system() в aiohttp.helpers блокирует импорт на ~80 секунд.
# Отключаем WMI до импорта aiohttp — platform перейдёт на быстрый резервный путь.
if sys.platform == "win32" and hasattr(platform, "_wmi"):
    platform._wmi = None

__version__ = "2.0.0"
