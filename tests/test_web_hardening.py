"""P0/P1: заголовки, отзыв сессий, ошибки, пагинация и flash админки."""

from __future__ import annotations

import re
from datetime import date, timedelta

import httpx
import pytest_asyncio
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from passrw.db import repo
from passrw.services.manager import TrackingManager
from passrw.web.app import create_web_app


def build_app(settings, database):
    manager = TrackingManager(bot=None, database=database, settings=settings, client=None)
    return create_web_app(settings, database, manager)


def new_client(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")


def csrf_from(html: str) -> str:
    match = re.search(r'name="_csrf_token" value="([^"]+)"', html)
    assert match, "CSRF-токен не найден на странице"
    return match.group(1)


async def login(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/login", data={"username": "admin", "password": "testpass123"}
    )
    assert response.status_code == 303


@pytest_asyncio.fixture
async def app(settings, database):
    return build_app(settings, database)


@pytest_asyncio.fixture
async def client(app):
    async with new_client(app) as http:
        yield http


async def test_redirect_and_error_responses_carry_security_headers(client):
    redirect = await client.get("/")

    assert redirect.status_code == 303
    assert redirect.headers["cache-control"] == "no-store"
    assert redirect.headers["x-frame-options"] == "DENY"
    assert "script-src 'none'" in redirect.headers["content-security-policy"]
    assert "strict-transport-security" not in redirect.headers


async def test_forwarded_for_is_ignored_without_trusted_proxies(settings, database):
    settings.ip_whitelist = "10.9.9.9"
    app = build_app(settings, database)

    async with new_client(app) as http:
        response = await http.get("/", headers={"x-forwarded-for": "10.9.9.9"})

    assert response.status_code == 403
    assert response.headers["cache-control"] == "no-store"


async def test_forwarded_for_works_from_explicit_trusted_proxy(settings, database):
    settings.ip_whitelist = "10.9.9.9"
    settings.trusted_proxies = "127.0.0.1"
    app = build_app(settings, database)
    # uvicorn подключает этот middleware при proxy_headers=True.
    wrapped = ProxyHeadersMiddleware(app, trusted_hosts=settings.trusted_proxy_list)
    transport = httpx.ASGITransport(app=wrapped)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
        response = await http.get("/", headers={"x-forwarded-for": "10.9.9.9"})

    assert response.status_code == 303
    assert response.headers["location"] == "/login"


async def test_non_ascii_login_is_rejected_without_500(client):
    response = await client.post(
        "/login", data={"username": "админ", "password": "testpass123"}
    )

    assert response.status_code == 401


async def test_non_ascii_csrf_token_is_rejected_without_500(client):
    await login(client)

    response = await client.post("/logout", data={"_csrf_token": "токен"})

    assert response.status_code in (400, 403)
    assert response.headers["cache-control"] == "no-store"


async def test_hsts_only_when_cookie_is_secure(settings, database):
    settings.session_cookie_secure = True
    app = build_app(settings, database)

    async with new_client(app) as http:
        response = await http.get("/login")

    assert response.headers["strict-transport-security"] == "max-age=31536000"


async def test_invalid_ids_return_html_or_json_404(client):
    await login(client)

    html = await client.get("/trackings/not-a-number")
    assert html.status_code == 404
    assert "Запись не найдена" in html.text
    assert html.headers["cache-control"] == "no-store"

    huge = await client.get(f"/trackings/{2**63}")
    assert huge.status_code == 404

    as_json = await client.get(
        "/trackings/not-a-number", headers={"accept": "application/json"}
    )
    assert as_json.status_code == 404
    assert as_json.json()["detail"] == "Запись не найдена"


async def test_validation_error_is_rendered_as_422(client):
    await login(client)

    response = await client.get("/trackings?page=abc")

    assert response.status_code == 422
    assert "Некорректные параметры запроса" in response.text


async def test_logout_revokes_other_sessions(settings, database):
    app = build_app(settings, database)
    async with new_client(app) as first, new_client(app) as second:
        await login(first)
        second.cookies.set("passrw_admin", first.cookies.get("passrw_admin"))

        assert (await second.get("/")).status_code == 200

        dashboard = await first.get("/")
        logout = await first.post(
            "/logout", data={"_csrf_token": csrf_from(dashboard.text)}
        )
        assert logout.status_code == 303

        revoked = await second.get("/")

    assert revoked.status_code == 303
    assert revoked.headers["location"] == "/login?expired=1"


async def test_password_change_revokes_existing_sessions(settings, database):
    app = build_app(settings, database)
    async with new_client(app) as first:
        await login(first)
        assert (await first.get("/")).status_code == 200
        cookie = first.cookies.get("passrw_admin")

    # Смена пароля + перезапуск приложения (новый отпечаток в той же БД).
    settings.admin_password = "new-password"
    restarted = build_app(settings, database)
    async with new_client(restarted) as second:
        second.cookies.set("passrw_admin", cookie)
        response = await second.get("/")

    assert response.status_code == 303
    assert response.headers["location"] == "/login?expired=1"


async def test_flash_message_is_shown_once(settings, database):
    async with database.session() as session:
        await repo.upsert_user(session, 44)
        tracking = await repo.create_tracking(
            session,
            chat_id=44,
            from_station="Минск",
            to_station="Брест",
            travel_date=date.today() + timedelta(days=5),
            passengers=1,
            train_number="001А",
            depart_time="10:00",
            notify_threshold=1,
            carriage_filter=None,
            max_price=None,
            stop_on_success=True,
        )
        tracking_id = tracking.id

    app = build_app(settings, database)
    async with new_client(app) as client:
        await login(client)
        page = await client.get(f"/trackings/{tracking_id}")
        response = await client.post(
            f"/trackings/{tracking_id}/filters",
            data={"_csrf_token": csrf_from(page.text), "heartbeat_interval": "1800"},
            follow_redirects=True,
        )
        assert "Фильтры сохранены" in response.text

        again = await client.get(f"/trackings/{tracking_id}")
        assert "Фильтры сохранены" not in again.text


async def test_trackings_page_clamps_out_of_range_page(settings, database):
    async with database.session() as session:
        await repo.upsert_user(session, 45)
        for index in range(30):
            await repo.create_tracking(
                session,
                chat_id=45,
                from_station="Минск",
                to_station="Брест",
                travel_date=date.today() + timedelta(days=5),
                passengers=1,
                train_number=f"00{index}А",
                depart_time=f"{index % 24:02d}:00",
                notify_threshold=1,
                carriage_filter=None,
                max_price=None,
                stop_on_success=True,
            )

    app = build_app(settings, database)
    async with new_client(app) as client:
        await login(client)
        response = await client.get("/trackings?page=99")

    assert response.status_code == 200
    assert "стр. 2 из 2" in response.text


async def test_metrics_token_is_required_when_configured(settings, database):
    settings.metrics_token = "secret-token"
    app = build_app(settings, database)

    async with new_client(app) as client:
        denied = await client.get("/metrics")
        allowed = await client.get(
            "/metrics", headers={"authorization": "Bearer secret-token"}
        )

    assert denied.status_code == 403
    assert allowed.status_code == 200


async def test_non_ascii_row_id_returns_404_not_500(client):
    await login(client)

    response = await client.get("/trackings/%C2%B2")

    assert response.status_code == 404
    assert "Запись не найдена" in response.text


async def test_huge_page_is_clamped_on_trackings_and_users(client):
    await login(client)

    trackings = await client.get(f"/trackings?page={10**19}")
    users = await client.get(f"/users?page={10**19}")

    assert trackings.status_code == 200
    assert users.status_code == 200


async def test_heartbeat_interval_is_bounded(settings, database):
    async with database.session() as session:
        await repo.upsert_user(session, 46)
        tracking = await repo.create_tracking(
            session,
            chat_id=46,
            from_station="Минск",
            to_station="Брест",
            travel_date=date.today() + timedelta(days=5),
            passengers=1,
            train_number="001А",
            depart_time="10:00",
            notify_threshold=1,
            carriage_filter=None,
            max_price=None,
            stop_on_success=True,
        )
        tracking_id = tracking.id

    app = build_app(settings, database)
    async with new_client(app) as client:
        await login(client)
        page = await client.get(f"/trackings/{tracking_id}")
        response = await client.post(
            f"/trackings/{tracking_id}/filters",
            data={
                "_csrf_token": csrf_from(page.text),
                "heartbeat_interval": str(10**19),
            },
            follow_redirects=True,
        )
        assert response.status_code == 200

        async with database.session() as session:
            saved = await repo.get_tracking(session, tracking_id)
            assert saved is not None
            assert saved.heartbeat_interval == 604800
