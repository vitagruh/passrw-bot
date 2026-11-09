"""Тесты хешей паролей, CSRF-хелперов и защиты CSV."""

from __future__ import annotations

import hashlib

from argon2 import PasswordHasher

from passrw.web.security import csv_safe, verify_password_hash


def test_argon2_hash_verification():
    encoded = PasswordHasher().hash("s3cret")

    assert verify_password_hash(encoded, "s3cret")
    assert not verify_password_hash(encoded, "wrong")


def test_legacy_pbkdf2_hash_verification():
    salt = "legacy-salt"
    digest = hashlib.pbkdf2_hmac("sha256", b"oldpass", salt.encode(), 1000).hex()
    encoded = f"pbkdf2:sha256:1000${salt}${digest}"

    assert verify_password_hash(encoded, "oldpass")
    assert not verify_password_hash(encoded, "nope")
    assert not verify_password_hash("unknown-format", "oldpass")


def test_csv_formula_injection_is_neutralized():
    for dangerous in ("=1+1", "+7", "-2", "@cmd", "\tpayload", "\rpayload"):
        assert csv_safe(dangerous) == "'" + dangerous

    assert csv_safe("Минск") == "Минск"
    assert csv_safe(None) == ""
