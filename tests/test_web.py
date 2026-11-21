"""Интеграционные тесты веб-админки через ASGI-транспорт."""

from __future__ import annotations

import re
from datetime import date, timedelta

import httpx
import pytest_asyncio

from passrw.db import repo
from passrw.services.manager import TrackingManager
from passrw.web.app import create_web_app
from tests.conftest import make_settings


def build_app(settings, database):
    manager = TrackingManager(bot=None, database=database, settings=settings, client=None)
    return create_web_app(settings, database, manager)


@pytest_asyncio.fixture
async def app(settings, database):
    return build_app(settings, database)


@pytest_asyncio.fixture
async def client(app):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
        yield http


async def login(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/login", data={"username": "admin", "password": "testpass123"}
    )
    assert response.status_code == 303


def csrf_from(html: str) -> str:
    match = re.search(r'name="_csrf_token" value="([^"]+)"', html)
    assert match, "CSRF-токен не найден на странице"
    return match.group(1)


async def test_healthz_and_metrics_are_public(client):
    health = await client.get("/healthz")
    assert health.status_code == 200
    body = health.json()
    assert body["database"] == "ok"
    assert "manager" in body
    assert "version" not in body

    metrics = await client.get("/metrics")
    assert metrics.status_code == 200


async def test_protected_page_redirects_to_login(client):
    response = await client.get("/")

    assert response.status_code == 303
    assert response.headers["location"] == "/login"


async def test_login_flow_csrf_and_logout(client):
    page = await client.get("/login")
    assert page.status_code == 200

    failed = await client.post(
        "/login", data={"username": "admin", "password": "wrong"}
    )
    assert failed.status_code == 401

    await login(client)
    assert client.cookies.get("passrw_admin")

    dashboard = await client.get("/")
    assert dashboard.status_code == 200
    assert "Панель" in dashboard.text

    without_csrf = await client.post("/logout")
    assert without_csrf.status_code == 400

    token = csrf_from(dashboard.text)
    logout = await client.post("/logout", data={"_csrf_token": token})
    assert logout.status_code == 303

    again = await client.get("/")
    assert again.status_code == 303
    assert again.headers["location"] == "/login"


async def test_admin_page_requires_configured_password(settings, database):
    settings.admin_password = ""
    settings.admin_password_hash = ""

    transport = httpx.ASGITransport(app=build_app(settings, database))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/")

    assert response.status_code == 503
    assert "ADMIN_PASSWORD" in response.text


async def test_ip_allowlist_blocks_admin_but_not_healthz(settings, database):
    settings.ip_whitelist = "10.0.0.1"

    transport = httpx.ASGITransport(app=build_app(settings, database))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        blocked = await client.get("/")
        health = await client.get("/healthz")

    assert blocked.status_code == 403
    assert health.status_code == 200


async def test_csv_export_neutralizes_formula_injection(settings, database, client):
    async with database.session() as session:
        await repo.upsert_user(session, 42, username="=evil")
        await repo.create_tracking(
            session,
            chat_id=42,
            from_station="=1+1",
            to_station="Минск",
            travel_date=date.today() + timedelta(days=3),
            passengers=1,
            train_number="712Б",
            depart_time="12:34",
            notify_threshold=1,
            carriage_filter=None,
            max_price=None,
            stop_on_success=True,
        )

    await login(client)
    response = await client.get("/export/trackings.csv?active=all")

    assert response.status_code == 200
    assert "text/csv" in response.headers["content-type"]
    assert response.content.startswith("\ufeff".encode())
    assert "'=1+1" in response.text
    assert "'=evil" in response.text


async def test_tracking_page_and_admin_stop(settings, database, client):
    async with database.session() as session:
        await repo.upsert_user(session, 43, username="dave")
        tracking = await repo.create_tracking(
            session,
            chat_id=43,
            from_station="Минск",
            to_station="Брест",
            travel_date=date.today() + timedelta(days=4),
            passengers=1,
            train_number="001А",
            depart_time="10:00",
            notify_threshold=1,
            carriage_filter=None,
            max_price=None,
            stop_on_success=True,
        )
        tracking_id = tracking.id

    await login(client)
    detail = await client.get(f"/trackings/{tracking_id}")
    assert detail.status_code == 200
    assert "Минск" in detail.text

    token = csrf_from(detail.text)
    stopped = await client.post(
        f"/trackings/{tracking_id}/stop", data={"_csrf_token": token}
    )
    assert stopped.status_code == 303

    async with database.session() as session:
        tracking = await repo.get_tracking(session, tracking_id)
        assert tracking is not None
        assert tracking.is_active is False
        assert tracking.stop_reason == "admin_stop"


async def test_unknown_tracking_returns_404(database, client):
    await login(client)

    response = await client.get("/trackings/999999")

    assert response.status_code == 404


def test_settings_factory_isolated(tmp_path):
    custom = make_settings(tmp_path, session_timeout_minutes=15)

    assert custom.session_timeout_minutes == 15
    assert custom.database_path == tmp_path / "test.db"
    assert custom.admin_auth_configured
