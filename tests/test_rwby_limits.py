"""Границы клиента pass.rw.by: размер ответа и словарь локов."""

from __future__ import annotations

from datetime import date

import httpx
import pytest

from passrw.services import rwby as rwby_module
from passrw.services.parser import LayoutChanged
from passrw.services.rwby import RwbyClient, SiteUnavailable


def make_client(settings, handler) -> RwbyClient:
    return RwbyClient(settings, transport=httpx.MockTransport(handler))


async def test_oversized_response_is_rejected(settings, monkeypatch):
    monkeypatch.setattr(rwby_module, "MAX_RESPONSE_BYTES", 32)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>" + "x" * 200 + "</html>")

    client = make_client(settings, handler)
    try:
        with pytest.raises(SiteUnavailable):
            await client.search("Минск", "Брест", date(2026, 12, 1))
    finally:
        await client.close()


async def test_locks_are_bounded_when_cache_is_disabled(settings, monkeypatch):
    settings.http_cache_ttl = 0
    monkeypatch.setattr(rwby_module, "MAX_LOCKS", 5)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html></html>")

    client = make_client(settings, handler)
    try:
        for index in range(20):
            with pytest.raises(LayoutChanged):
                await client.search(f"S{index}", "B", date(2026, 12, 1))
    finally:
        await client.close()

    assert len(client._locks) <= 6
