"""Аутентификация админки, CSRF и защитные хелперы."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from collections import defaultdict
from typing import Any

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError

from passrw.config import Settings

_ph = PasswordHasher()


def safe_compare_str(first: str | None, second: str | None) -> bool:
    """Сравнение строк без утечки по времени и без TypeError на non-ASCII."""
    if not isinstance(first, str) or not isinstance(second, str):
        return False
    try:
        return secrets.compare_digest(first.encode("utf-8"), second.encode("utf-8"))
    except (TypeError, ValueError):
        return False


def admin_credentials_fingerprint(settings: Settings) -> str:
    """Отпечаток учётных данных: смена пароля/хеша отзывает сессии админа."""
    raw = settings.admin_password_hash or settings.admin_password
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def verify_password_hash(encoded: str, password: str) -> bool:
    """Проверяет argon2, а также pbkdf2/scrypt-хеши из старой админки."""
    try:
        if encoded.startswith("$argon2"):
            return _ph.verify(encoded, password)
        if encoded.startswith("pbkdf2:"):
            method, salt, digest = encoded.split("$", 2)
            _, algorithm, iterations = method.split(":")
            calculated = hashlib.pbkdf2_hmac(
                algorithm, password.encode(), salt.encode(), int(iterations)
            ).hex()
            return hmac.compare_digest(calculated, digest)
        if encoded.startswith("scrypt:"):
            method, salt, digest = encoded.split("$", 2)
            n, r, p = (int(part) for part in method.split(":")[1:])
            dklen = len(bytes.fromhex(digest))
            calculated = hashlib.scrypt(
                password.encode(), salt=salt.encode(), n=n, r=r, p=p, dklen=dklen
            ).hex()
            return hmac.compare_digest(calculated, digest)
    except (VerifyMismatchError, InvalidHashError, ValueError, TypeError):
        return False
    return False


def verify_admin(settings: Settings, username: str, password: str) -> bool:
    if not password or not safe_compare_str(username, settings.admin_username):
        return False
    if settings.admin_password_hash:
        return verify_password_hash(settings.admin_password_hash, password)
    if settings.admin_password:
        return secrets.compare_digest(password.encode(), settings.admin_password.encode())
    return False


def ensure_csrf(request) -> str:
    token = request.session.get("_csrf")
    if not token:
        token = secrets.token_hex(32)
        request.session["_csrf"] = token
    return token


def csrf_valid(request, supplied: str | None) -> bool:
    expected = request.session.get("_csrf")
    return bool(expected) and safe_compare_str(expected, supplied)


class RateLimiter:
    def __init__(self, max_requests: int, window_seconds: int) -> None:
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._hits: dict[str, list[float]] = defaultdict(list)

    def allow(self, key: str) -> bool:
        now = time.time()
        hits = [stamp for stamp in self._hits[key] if now - stamp < self.window_seconds]
        if len(hits) >= self.max_requests:
            self._hits[key] = hits
            return False
        hits.append(now)
        self._hits[key] = hits
        return True


class LoginGuard:
    """Блокировка перебора пароля по IP."""

    def __init__(self, max_attempts: int, lockout_seconds: int) -> None:
        self.max_attempts = max_attempts
        self.lockout_seconds = lockout_seconds
        self._attempts: dict[str, list[float]] = defaultdict(list)

    def locked(self, ip: str) -> bool:
        now = time.time()
        attempts = [stamp for stamp in self._attempts[ip] if now - stamp < self.lockout_seconds]
        self._attempts[ip] = attempts
        return len(attempts) >= self.max_attempts

    def fail(self, ip: str) -> None:
        self._attempts[ip].append(time.time())

    def reset(self, ip: str) -> None:
        self._attempts.pop(ip, None)


def csv_safe(value: Any) -> Any:
    """Защита от формул в CSV при открытии в Excel/LibreOffice."""
    if value is None:
        return ""
    text = str(value)
    if text.startswith(("=", "+", "-", "@", "\t", "\r")):
        return "'" + text
    return text
