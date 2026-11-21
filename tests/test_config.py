"""Разбор настроек из окружения."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from tests.conftest import make_settings


def test_blank_alert_chat_id_is_none(tmp_path):
    assert make_settings(tmp_path, alert_chat_id="").alert_chat_id is None


def test_numeric_alert_chat_id_is_parsed(tmp_path):
    assert make_settings(tmp_path, alert_chat_id="-1001234567890").alert_chat_id == -1001234567890


def test_sale_window_defaults_to_60_days(tmp_path):
    assert make_settings(tmp_path).sale_window_days == 60


@pytest.mark.parametrize("value", [0, 366])
def test_sale_window_is_validated(tmp_path, value):
    with pytest.raises(ValidationError):
        make_settings(tmp_path, sale_window_days=value)


def test_trusted_proxies_are_parsed(tmp_path):
    settings = make_settings(tmp_path, trusted_proxies=" 10.0.0.1, ,192.168.0.2 ")
    disabled = make_settings(tmp_path, trusted_proxies="")

    assert settings.trusted_proxy_list == ["10.0.0.1", "192.168.0.2"]
    assert settings.proxy_headers_enabled is True
    assert disabled.proxy_headers_enabled is False

