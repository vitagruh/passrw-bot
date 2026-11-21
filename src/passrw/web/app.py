"""Веб-админка: FastAPI-приложение поверх той же БД и менеджера трекингов."""

from __future__ import annotations

import asyncio
import csv
import io
import logging
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from sqlalchemy import text
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware

from passrw import __version__
from passrw.config import Settings
from passrw.db import Database, repo
from passrw.domain.rules import CARRIAGE_FILTERS, CARRIAGE_LABELS
from passrw.services.manager import TrackingManager
from passrw.util.time import fmt_local
from passrw.web.security import (
    LoginGuard,
    RateLimiter,
    admin_credentials_fingerprint,
    csrf_valid,
    csv_safe,
    ensure_csrf,
    safe_compare_str,
    verify_admin,
)

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
PUBLIC_PATHS = {"/login", "/healthz", "/metrics"}
TRACKING_PAGE_SIZE = 25
USERS_PAGE_SIZE = 50
LOGS_PAGE_SIZE = 100
MAX_ROW_ID = 2**63
MAX_PAGE = 100_000


def _page_count(total: int, size: int) -> int:
    return max(1, (total + size - 1) // size)


def _parse_row_id(raw: str) -> int:
    """Идентификатор записи: только положительный int в пределах SQLite."""
    text = (raw or "").strip()
    # isdigit() истинно и для не-ASCII цифр ('²'), а int() на них падает.
    if not text.isascii() or not text.isdigit():
        raise HTTPException(404, "Запись не найдена")
    value = int(text)
    if not 1 <= value < MAX_ROW_ID:
        raise HTTPException(404, "Запись не найдена")
    return value


def create_web_app(
    settings: Settings,
    database: Database,
    manager: TrackingManager,
) -> FastAPI:
    app = FastAPI(
        title="passrw admin",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        dependencies=[Depends(_csrf_guard)],
    )
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    templates.env.filters["localdt"] = lambda value, fmt="%d.%m.%Y %H:%M": fmt_local(
        value, settings.timezone, fmt
    )
    templates.env.globals["csrf_token"] = ensure_csrf
    templates.env.globals["carriage_labels"] = CARRIAGE_LABELS
    templates.env.globals["version"] = __version__

    login_guard = LoginGuard(settings.login_max_attempts, settings.login_lockout_seconds)
    login_rate = RateLimiter(settings.rate_limit_requests, settings.rate_limit_window)
    credentials_fingerprint = admin_credentials_fingerprint(settings)

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    def client_ip(request: Request) -> str:
        # request.client.host — реальный адрес подключения; X-Forwarded-For
        # учитывается только если uvicorn настроен доверять прокси (см. __main__).
        return request.client.host if request.client else "unknown"

    async def admin_epoch() -> int:
        async with database.session() as session:
            return await repo.ensure_admin_session_epoch(session, credentials_fingerprint)

    @app.middleware("http")
    async def auth_guard(request: Request, call_next):
        path = request.url.path
        ip = client_ip(request)

        if path != "/healthz" and settings.ip_allowlist and ip not in settings.ip_allowlist:
            return _security_headers(
                PlainTextResponse("Forbidden: IP is not allowlisted", status_code=403),
                settings,
            )

        if path in PUBLIC_PATHS or path.startswith("/static/"):
            response = await call_next(request)
            return _security_headers(
                response, settings, no_store=not path.startswith("/static/")
            )

        if not settings.admin_auth_configured:
            return _security_headers(
                PlainTextResponse(
                    "Admin password is not configured (ADMIN_PASSWORD / ADMIN_PASSWORD_HASH)",
                    status_code=503,
                ),
                settings,
            )

        if not request.session.get("admin"):
            return _security_headers(RedirectResponse("/login", status_code=303), settings)

        if request.session.get("admin_epoch") != await admin_epoch():
            request.session.clear()
            return _security_headers(
                RedirectResponse("/login?expired=1", status_code=303), settings
            )

        last_activity = request.session.get("last_activity", 0)
        if last_activity and time.time() - last_activity > settings.session_timeout_minutes * 60:
            request.session.clear()
            return _security_headers(
                RedirectResponse("/login?expired=1", status_code=303), settings
            )
        request.session["last_activity"] = time.time()

        response = await call_next(request)
        return _security_headers(response, settings)

    # SessionMiddleware добавляется после auth_guard, чтобы оказаться снаружи:
    # auth_guard обращается к request.session.
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.secret_key,
        session_cookie="passrw_admin",
        max_age=settings.session_timeout_minutes * 60,
        same_site="lax",
        https_only=settings.session_cookie_secure,
    )

    def pop_flash(request: Request) -> tuple[str, str]:
        data = request.session.pop("_flash", None)
        if isinstance(data, dict):
            return str(data.get("message", "")), str(data.get("error", ""))
        return "", ""

    def set_flash(request: Request, *, message: str = "", error: str = "") -> None:
        request.session["_flash"] = {"message": message, "error": error}

    def redirect_with_flash(
        request: Request, url: str, *, message: str = "", error: str = ""
    ) -> RedirectResponse:
        if message or error:
            set_flash(request, message=message, error=error)
        return RedirectResponse(url, status_code=303)

    def render(
        request: Request, name: str, *, status_code: int = 200, **context: Any
    ) -> HTMLResponse:
        context.setdefault("admin", request.session.get("admin", ""))
        context.setdefault("settings", settings)
        if "message" not in context and "error" not in context:
            message, error = pop_flash(request)
            context["message"] = message
            context["error"] = error
        return templates.TemplateResponse(request, name, context, status_code=status_code)

    async def log_admin(request: Request, action: str, details: str = "") -> None:
        try:
            async with database.session() as session:
                await repo.log_admin_action(
                    session,
                    request.session.get("admin", "unknown"),
                    action,
                    details,
                    client_ip(request),
                )
        except Exception:  # noqa: BLE001 — аудит не должен ломать действие
            pass

    def wants_json(request: Request) -> bool:
        return "application/json" in request.headers.get("accept", "").casefold()

    def html_error(request: Request, status_code: int, message: str) -> Response:
        if wants_json(request):
            response: Response = JSONResponse(
                {"status": status_code, "detail": message}, status_code=status_code
            )
        else:
            admin = ""
            try:
                admin = request.session.get("admin", "")
            except AssertionError:  # сессии нет — ошибка до SessionMiddleware
                pass
            response = templates.TemplateResponse(
                request,
                "error.html",
                {
                    "status": status_code,
                    "message": message,
                    "admin": admin,
                    "settings": settings,
                },
                status_code=status_code,
            )
        return _security_headers(response, settings)

    @app.exception_handler(StarletteHTTPException)
    async def on_http_exception(request: Request, exc: StarletteHTTPException):
        detail = exc.detail if isinstance(exc.detail, str) else "Ошибка запроса"
        return html_error(request, exc.status_code, detail)

    @app.exception_handler(RequestValidationError)
    async def on_validation_error(request: Request, exc: RequestValidationError):
        return html_error(request, 422, "Некорректные параметры запроса")

    @app.exception_handler(Exception)
    async def on_unhandled_error(request: Request, exc: Exception):
        logger.error(
            "Необработанная ошибка админки: %s %s", request.method, request.url.path,
            exc_info=exc,
        )
        return html_error(request, 500, "Внутренняя ошибка сервера")

    # ── вход ───────────────────────────────────────────────────

    @app.get("/login", response_class=HTMLResponse)
    async def login_page(request: Request, expired: int = 0):
        if request.session.get("admin"):
            return RedirectResponse("/", status_code=303)
        return render(
            request,
            "login.html",
            error="Сессия истекла, войдите заново" if expired else "",
        )

    @app.post("/login", response_class=HTMLResponse)
    async def login_submit(
        request: Request,
        username: str = Form(""),
        password: str = Form(""),
    ):
        ip = client_ip(request)
        if not login_rate.allow(f"login:{ip}"):
            return render(request, "login.html", error="Слишком много запросов. Подождите минуту.", status_code=429)
        if not settings.admin_auth_configured:
            return render(
                request,
                "login.html",
                error="Пароль администратора не настроен на сервере.",
                status_code=503,
            )
        if login_guard.locked(ip):
            return render(
                request,
                "login.html",
                error=f"Вход заблокирован на {settings.login_lockout_seconds // 60} мин.",
                status_code=429,
            )
        if not verify_admin(settings, username, password):
            login_guard.fail(ip)
            await log_admin(request, "LOGIN_FAILED", f"username={username[:64]}")
            return render(request, "login.html", error="Неверный логин или пароль", status_code=401)

        login_guard.reset(ip)
        epoch = await admin_epoch()
        request.session.clear()
        request.session["admin"] = username
        request.session["admin_epoch"] = epoch
        request.session["last_activity"] = time.time()
        ensure_csrf(request)
        await log_admin(request, "LOGIN", f"username={username[:64]}")
        return RedirectResponse("/", status_code=303)

    @app.post("/logout")
    async def logout(request: Request):
        admin = request.session.get("admin", "")
        request.session.clear()
        async with database.session() as session:
            await repo.bump_admin_session_epoch(session)
        await log_admin(request, "LOGOUT", f"username={admin}")
        return RedirectResponse("/login", status_code=303)

    # ── панель ─────────────────────────────────────────────────

    @app.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request):
        async with database.session() as session:
            counts = await repo.dashboard_counts(session)
            samples = await repo.samples_per_day(session, 7)
            errors = await repo.list_bot_errors(session, 5)
            stations = await repo.top_stations(session, 5)
        chart_max = max((value for _, value in samples), default=1) or 1
        return render(
            request,
            "dashboard.html",
            counts=counts,
            samples=samples,
            chart_max=chart_max,
            errors=errors,
            stations=stations,
            running=manager.running_count,
            mismatch=counts["active_trackings"] != manager.running_count,
            uptime=str(timedelta(seconds=manager.uptime_seconds)),
            nav="dashboard",
        )

    # ── трекинги ───────────────────────────────────────────────

    @app.get("/trackings", response_class=HTMLResponse)
    async def trackings_page(
        request: Request,
        active: str = "1",
        q: str = "",
        page: int = 1,
    ):
        # Ограничиваем page до расчёта offset: иначе ?page=10**19
        # переполняет INTEGER SQLite ещё до clamp по числу страниц.
        page = min(max(1, page), MAX_PAGE)
        active_filter: bool | None = {"1": True, "0": False, "all": None}.get(active, True)
        async with database.session() as session:
            rows, total = await repo.list_trackings(
                session,
                active=active_filter,
                query=q.strip(),
                offset=(page - 1) * TRACKING_PAGE_SIZE,
                limit=TRACKING_PAGE_SIZE,
            )
            pages = _page_count(total, TRACKING_PAGE_SIZE)
            if page > pages:
                page = pages
                rows, total = await repo.list_trackings(
                    session,
                    active=active_filter,
                    query=q.strip(),
                    offset=(page - 1) * TRACKING_PAGE_SIZE,
                    limit=TRACKING_PAGE_SIZE,
                )
        return render(
            request,
            "trackings.html",
            rows=rows,
            total=total,
            page=page,
            pages=pages,
            active=active,
            q=q,
            nav="trackings",
        )

    @app.get("/trackings/{tracking_id}", response_class=HTMLResponse)
    async def tracking_detail(request: Request, tracking_id: str):
        row_id = _parse_row_id(tracking_id)
        async with database.session() as session:
            tracking = await repo.get_tracking_with_user(session, row_id)
            if tracking is None:
                raise HTTPException(404, "Трекинг не найден")
            samples = await repo.list_samples(session, row_id, 100)
        return render(
            request,
            "tracking.html",
            t=tracking,
            samples=samples,
            running=manager.is_running(row_id),
            carriage_filters=CARRIAGE_FILTERS,
            nav="trackings",
        )

    @app.post("/trackings/{tracking_id}/stop")
    async def tracking_stop(
        request: Request,
        tracking_id: str,
        notify: str | None = Form(None),
    ):
        row_id = _parse_row_id(tracking_id)
        async with database.session() as session:
            tracking = await repo.get_tracking(session, row_id)
            if tracking is None:
                raise HTTPException(404, "Трекинг не найден")
            route = f"{tracking.from_station} → {tracking.to_station}"
        stopped = await manager.stop(
            row_id,
            reason="admin_stop",
            notify_user=notify is not None,
            message=f"⏹ Трекинг {route} остановлен администратором.",
        )
        await log_admin(request, "TRACKING_STOP", f"id={row_id} {route}")
        suffix = "остановлен" if stopped else "уже неактивен"
        return redirect_with_flash(request, f"/trackings/{row_id}", message=suffix)

    @app.post("/trackings/{tracking_id}/filters")
    async def tracking_filters(
        request: Request,
        tracking_id: str,
        notify_threshold: str = Form(""),
        carriage_filter: str = Form("any"),
        max_price: str = Form(""),
        heartbeat_enabled: str | None = Form(None),
        heartbeat_interval: str = Form("1800"),
        stop_on_success: str | None = Form(None),
    ):
        row_id = _parse_row_id(tracking_id)
        fields: dict[str, Any] = {}
        try:
            threshold_raw = notify_threshold.strip()
            fields["notify_threshold"] = None if not threshold_raw else int(threshold_raw)
            if fields["notify_threshold"] is not None and not 1 <= fields["notify_threshold"] <= 20:
                raise ValueError("порог вне диапазона 1–20")

            key = carriage_filter if carriage_filter in CARRIAGE_FILTERS else "any"
            fields["carriage_filter"] = CARRIAGE_FILTERS[key]

            price_raw = max_price.strip().replace(",", ".")
            fields["max_price"] = None if not price_raw else float(price_raw)
            if fields["max_price"] is not None and not 0 < fields["max_price"] <= 100000:
                raise ValueError("цена вне диапазона")

            fields["heartbeat_interval"] = min(
                max(300, int(heartbeat_interval or 1800)), 604800
            )
        except ValueError as exc:
            return redirect_with_flash(
                request, f"/trackings/{row_id}", error=str(exc)
            )

        fields["heartbeat_enabled"] = heartbeat_enabled is not None
        fields["stop_on_success"] = stop_on_success is not None

        async with database.session() as session:
            tracking = await repo.get_tracking(session, row_id)
            if tracking is None:
                raise HTTPException(404, "Трекинг не найден")
            is_active = tracking.is_active
            await repo.update_tracking(session, row_id, **fields)
        if is_active:
            manager.add(row_id)
        await log_admin(request, "TRACKING_FILTERS", f"id={row_id} {fields}")
        return redirect_with_flash(
            request, f"/trackings/{row_id}", message="Фильтры сохранены"
        )

    @app.post("/trackings/{tracking_id}/delete")
    async def tracking_delete(request: Request, tracking_id: str):
        row_id = _parse_row_id(tracking_id)
        async with database.session() as session:
            tracking = await repo.get_tracking(session, row_id)
            if tracking is None:
                raise HTTPException(404, "Трекинг не найден")
            if tracking.is_active:
                return redirect_with_flash(
                    request,
                    f"/trackings/{row_id}",
                    error="Сначала остановите трекинг",
                )
            await repo.delete_tracking(session, row_id)
        await log_admin(request, "TRACKING_DELETE", f"id={row_id}")
        return redirect_with_flash(request, "/trackings", message="Трекинг удалён")

    # ── пользователи ───────────────────────────────────────────

    @app.get("/users", response_class=HTMLResponse)
    async def users_page(request: Request, q: str = "", page: int = 1):
        page = min(max(1, page), MAX_PAGE)
        async with database.session() as session:
            rows, total = await repo.list_users(
                session,
                offset=(page - 1) * USERS_PAGE_SIZE,
                limit=USERS_PAGE_SIZE,
                query=q.strip(),
            )
            pages = _page_count(total, USERS_PAGE_SIZE)
            if page > pages:
                page = pages
                rows, total = await repo.list_users(
                    session,
                    offset=(page - 1) * USERS_PAGE_SIZE,
                    limit=USERS_PAGE_SIZE,
                    query=q.strip(),
                )
        return render(
            request,
            "users.html",
            rows=[{"user": user, "active": active} for user, active in rows],
            total=total,
            page=page,
            pages=pages,
            q=q,
            nav="users",
        )

    @app.post("/users/{chat_id}/block")
    async def user_block(request: Request, chat_id: str):
        row_id = _parse_row_id(chat_id)
        async with database.session() as session:
            user = await repo.get_user(session, row_id)
            if user is None:
                raise HTTPException(404, "Пользователь не найден")
            target = not user.is_blocked
            await repo.set_user_blocked(session, row_id, target)
        if target:
            await manager.stop_all_for_chat(row_id, reason="admin_block")
        action = "USER_BLOCK" if target else "USER_UNBLOCK"
        await log_admin(request, action, f"chat_id={row_id}")
        return redirect_with_flash(
            request,
            "/users",
            message="Пользователь заблокирован" if target else "Блокировка снята",
        )

    @app.post("/users/{chat_id}/role")
    async def user_role(request: Request, chat_id: str, role: str = Form("user")):
        row_id = _parse_row_id(chat_id)
        if role not in {"user", "moderator", "admin"}:
            raise HTTPException(400, "Неизвестная роль")
        async with database.session() as session:
            user = await repo.get_user(session, row_id)
            if user is None:
                raise HTTPException(404, "Пользователь не найден")
            await repo.set_user_role(session, row_id, role)
        await log_admin(request, "USER_ROLE", f"chat_id={row_id} role={role}")
        return redirect_with_flash(request, "/users", message="Роль обновлена")

    @app.post("/broadcast")
    async def broadcast(request: Request, text: str = Form("")):
        body = text.strip()
        if not body or len(body) > 3500:
            return redirect_with_flash(
                request, "/users", error="Текст должен быть от 1 до 3500 символов"
            )
        async with database.session() as session:
            chat_ids = await repo.list_chat_ids(session)
        semaphore = asyncio.Semaphore(5)

        async def deliver(chat_id: int) -> bool:
            async with semaphore:
                return await manager.send_user_message(chat_id, body)

        results = await asyncio.gather(*(deliver(chat_id) for chat_id in chat_ids))
        sent = sum(1 for ok in results if ok)
        await log_admin(request, "BROADCAST", f"sent={sent}/{len(chat_ids)}")
        return redirect_with_flash(
            request, "/users", message=f"Рассылка: доставлено {sent} из {len(chat_ids)}"
        )

    # ── логи ───────────────────────────────────────────────────

    @app.get("/logs", response_class=HTMLResponse)
    async def logs_page(request: Request, kind: str = "errors", page: int = 1):
        page = max(1, page)
        if kind not in {"errors", "users", "admin"}:
            kind = "errors"
        async with database.session() as session:
            if kind == "users":
                total = await repo.count_user_logs(session)
            elif kind == "admin":
                total = await repo.count_admin_logs(session)
            else:
                total = await repo.count_bot_errors(session)
            pages = _page_count(total, LOGS_PAGE_SIZE)
            page = min(page, pages)
            offset = (page - 1) * LOGS_PAGE_SIZE
            if kind == "users":
                items = await repo.list_user_logs(session, LOGS_PAGE_SIZE, offset)
            elif kind == "admin":
                items = await repo.list_admin_logs(session, LOGS_PAGE_SIZE, offset)
            else:
                items = await repo.list_bot_errors(session, LOGS_PAGE_SIZE, offset)
        return render(
            request,
            "logs.html",
            items=items,
            kind=kind,
            page=page,
            pages=pages,
            has_next=page < pages,
            nav="logs",
        )

    # ── экспорт и служебные эндпоинты ──────────────────────────

    @app.get("/export/trackings.csv")
    async def export_trackings(request: Request, active: str = "all"):
        active_filter: bool | None = {"1": True, "0": False, "all": None}.get(active, None)
        async with database.session() as session:
            rows, _ = await repo.list_trackings(
                session, active=active_filter, offset=0, limit=100000
            )
        buffer = io.StringIO()
        buffer.write("\ufeff")
        writer = csv.writer(buffer, lineterminator="\r\n")
        writer.writerow(
            [
                "id", "chat_id", "username", "from", "to", "travel_date", "passengers",
                "train", "depart_time", "active", "stop_reason", "seats", "min_price",
                "checks", "created_at", "stopped_at",
            ]
        )
        for item in rows:
            writer.writerow(
                [
                    csv_safe(item.id),
                    csv_safe(item.chat_id),
                    csv_safe(item.user.username if item.user else ""),
                    csv_safe(item.from_station),
                    csv_safe(item.to_station),
                    item.travel_date.isoformat(),
                    csv_safe(item.passengers),
                    csv_safe(item.train_number),
                    csv_safe(item.depart_time),
                    "yes" if item.is_active else "no",
                    csv_safe(item.stop_reason or ""),
                    csv_safe(item.seats_available),
                    csv_safe(item.min_price if item.min_price is not None else ""),
                    csv_safe(item.requests_count),
                    csv_safe(item.created_at.isoformat(sep=" ", timespec="seconds")),
                    csv_safe(item.stopped_at.isoformat(sep=" ", timespec="seconds") if item.stopped_at else ""),
                ]
            )
        filename = f"passrw_trackings_{datetime.now():%Y%m%d_%H%M}.csv"
        return StreamingResponse(
            iter([buffer.getvalue()]),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    @app.get("/healthz")
    async def healthz():
        try:
            async with database.session() as session:
                await session.execute(text("SELECT 1"))
                active = await repo.count_active_trackings(session)
                last_check = await repo.last_check_time(session)
        except Exception:  # noqa: BLE001
            return JSONResponse({"status": "error", "database": "unavailable"}, status_code=503)
        running = manager.running_count
        return {
            "status": "ok" if running >= active else "degraded",
            "database": "ok",
            "manager": {
                "running": running,
                "active_trackings": active,
                "last_check_at": last_check.isoformat(sep=" ", timespec="seconds")
                if last_check
                else None,
            },
        }

    @app.get("/metrics")
    async def metrics(request: Request):
        if settings.metrics_token:
            header = request.headers.get("authorization", "")
            expected = f"Bearer {settings.metrics_token}"
            if not safe_compare_str(header, expected):
                raise HTTPException(403, "Metrics token required")
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    return app


def _security_headers(
    response: Response, settings: Settings | None = None, *, no_store: bool = True
) -> Response:
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
        "script-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'",
    )
    if no_store:
        response.headers.setdefault("Cache-Control", "no-store")
        response.headers.setdefault("Pragma", "no-cache")
    if settings is not None and settings.session_cookie_secure:
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
    return response


async def _csrf_guard(request: Request) -> None:
    """Проверка CSRF для изменяющих запросов (кроме логина)."""
    if request.method in SAFE_METHODS or request.url.path == "/login":
        return
    supplied = request.headers.get("X-CSRF-Token")
    if not supplied:
        content_type = request.headers.get("content-type", "")
        if content_type.startswith(
            ("application/x-www-form-urlencoded", "multipart/form-data")
        ):
            form = await request.form()
            value = form.get("_csrf_token")
            supplied = value if isinstance(value, str) else None
    if not csrf_valid(request, supplied):
        raise HTTPException(400, "CSRF token missing or invalid")
