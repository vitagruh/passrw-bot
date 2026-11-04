"""Интеграционные тесты Flask-админки (логин, CSRF, маршруты, API)."""

import re

import admin_panel as ap
import db


def make_client():
    ap.app.config.update(TESTING=True)
    return ap.app.test_client()


def login(client):
    response = client.post(
        "/login", data={"username": "admin", "password": "testpass123"}
    )
    assert response.status_code == 302
    dashboard = client.get("/")
    match = re.search(r'var token = "([0-9a-f]+)"', dashboard.get_data(as_text=True))
    assert match, "CSRF-токен не найден на странице"
    return match.group(1)


def test_login_required_redirects(clean_db):
    client = make_client()
    response = client.get("/")
    assert response.status_code == 302
    assert "/login" in response.headers["Location"]


def test_login_rejects_wrong_password(clean_db):
    ap.login_attempts.clear()
    client = make_client()
    response = client.post(
        "/login", data={"username": "admin", "password": "wrong"}
    )
    assert response.status_code == 200
    assert "Неверное имя пользователя или пароль" in response.get_data(as_text=True)
    ap.login_attempts.clear()


def test_login_lockout_after_failures(clean_db):
    ap.login_attempts.clear()
    client = make_client()
    for _ in range(ap.LOGIN_MAX_ATTEMPTS):
        client.post("/login", data={"username": "admin", "password": "wrong"})

    response = client.post(
        "/login", data={"username": "admin", "password": "testpass123"}
    )
    assert response.status_code == 429
    ap.login_attempts.clear()


def test_main_pages_render(clean_db, tracking):
    client = make_client()
    login(client)
    tracking_id = tracking["id"]

    for url in (
        "/",
        "/trackings",
        "/users",
        "/monitoring",
        "/api/statistics",
        f"/tracking/{tracking_id}",
        f"/user/{tracking['chat_id']}",
        "/export/trackings/csv",
        "/export/users/csv",
    ):
        response = client.get(url)
        assert response.status_code == 200, f"{url} -> {response.status_code}"


def test_csrf_token_required_for_post(clean_db, tracking):
    client = make_client()
    login(client)
    response = client.post(f"/tracking/{tracking['id']}/toggle_heartbeat", data={})
    assert response.status_code == 400


def test_toggle_heartbeat(clean_db, tracking):
    client = make_client()
    token = login(client)

    response = client.post(
        f"/tracking/{tracking['id']}/toggle_heartbeat",
        data={"_csrf_token": token},
    )
    assert response.status_code == 302

    with db.get_db_cursor() as cursor:
        row = cursor.execute(
            "SELECT heartbeat_enabled FROM active_trackings WHERE id = ?",
            (tracking["id"],),
        ).fetchone()
    assert row["heartbeat_enabled"] == 1


def test_update_heartbeat_interval(clean_db, tracking):
    client = make_client()
    token = login(client)

    response = client.post(
        f"/tracking/{tracking['id']}/update_heartbeat",
        data={"_csrf_token": token, "interval": "900"},
    )
    assert response.status_code == 302

    with db.get_db_cursor() as cursor:
        row = cursor.execute(
            "SELECT heartbeat_interval FROM active_trackings WHERE id = ?",
            (tracking["id"],),
        ).fetchone()
    assert row["heartbeat_interval"] == 900


def test_api_delete_requests_graceful_stop(clean_db, tracking):
    client = make_client()
    token = login(client)

    response = client.delete(
        f"/api/tracking/{tracking['id']}", headers={"X-CSRF-Token": token}
    )
    assert response.status_code == 200
    assert response.get_json() == {"success": True}

    with db.get_db_cursor() as cursor:
        row = cursor.execute(
            "SELECT is_stopped FROM active_trackings WHERE id = ?", (tracking["id"],)
        ).fetchone()
        flag = cursor.execute(
            "SELECT action, tracking_id, processed FROM sync_flags WHERE tracking_id = ?",
            (tracking["id"],),
        ).fetchone()
    assert row["is_stopped"] == 1
    assert flag["action"] == "STOP"
    assert flag["processed"] == 0


def test_api_delete_missing_tracking(clean_db):
    client = make_client()
    token = login(client)
    response = client.delete(
        "/api/tracking/999999", headers={"X-CSRF-Token": token}
    )
    assert response.status_code == 404


def test_missing_tracking_redirects(clean_db):
    client = make_client()
    login(client)
    response = client.get("/tracking/999999")
    assert response.status_code == 302


def test_healthz(clean_db, tracking):
    client = make_client()
    response = client.get("/healthz")
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["status"] == "ok"
    assert payload["db"] == "ok"
    assert payload["trackings"] == 1
    assert payload["users"] >= 1


def test_metrics(clean_db, tracking):
    client = make_client()
    response = client.get("/metrics")
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "passrw_trackings_active 1" in body
    assert "passrw_db_ok 1" in body
    assert "passrw_users_total" in body

