"""Метрики Prometheus, общие для бота и веб-процесса."""

from prometheus_client import Counter, Gauge

CHECKS_TOTAL = Counter("passrw_checks_total", "Выполненные проверки расписания")
CHECK_FAILURES = Counter(
    "passrw_check_failures_total", "Ошибки проверок", ["reason"]
)
HTTP_REQUESTS = Counter(
    "passrw_http_requests_total", "HTTP-запросы к pass.rw.by", ["status"]
)
NOTIFICATIONS_TOTAL = Counter(
    "passrw_notifications_total", "Отправленные уведомления о билетах"
)
ACTIVE_TRACKINGS = Gauge(
    "passrw_active_trackings", "Активные трекинги в менеджере"
)
PARSER_LAYOUT_ERRORS = Counter(
    "passrw_parser_layout_errors_total", "Случаи нераспознанной вёрстки"
)
