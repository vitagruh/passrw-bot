"""Разбор настроек из окружения."""

from __future__ import annotations

from tests.conftest import make_settings


def test_blank_alert_chat_id_is_none(tmp_path):
    assert make_settings(tmp_path, alert_chat_id="").alert_chat_id is None


def test_numeric_alert_chat_id_is_parsed(tmp_path):
    assert make_settings(tmp_path, alert_chat_id="-1001234567890").alert_chat_id == -1001234567890
