"""Конфиг uvicorn: X-Forwarded-For принимается только от доверенных прокси."""

from __future__ import annotations

from passrw.__main__ import build_uvicorn_config
from tests.conftest import make_settings


async def _app(scope, receive, send):  # pragma: no cover — ASGI-заглушка
    pass


def test_proxy_headers_disabled_without_trusted_proxies(tmp_path):
    settings = make_settings(tmp_path)

    config = build_uvicorn_config(_app, settings)

    assert config.proxy_headers is False
    assert config.forwarded_allow_ips == "127.0.0.1"


def test_proxy_headers_enabled_for_trusted_proxies(tmp_path):
    settings = make_settings(tmp_path, trusted_proxies="10.0.0.1, 192.168.0.2")

    config = build_uvicorn_config(_app, settings)

    assert config.proxy_headers is True
    assert config.forwarded_allow_ips == "10.0.0.1, 192.168.0.2"
