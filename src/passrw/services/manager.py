"""Менеджер трекингов: один asyncio-таск на трекинг вместо потока на каждого."""

from __future__ import annotations

import asyncio
import logging
import random
import time

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from passrw.config import Settings
from passrw.db import Database, repo
from passrw.db.models import Tracking, UserSettings
from passrw.domain.railway import Evaluation, Filters, evaluate_train, pick_train
from passrw.domain.rules import adaptive_interval, in_quiet_hours
from passrw.metrics import (
    ACTIVE_TRACKINGS,
    CHECK_FAILURES,
    CHECKS_TOTAL,
    NOTIFICATIONS_TOTAL,
)
from passrw.services.notifications import build_alert, build_heartbeat
from passrw.services.parser import LayoutChanged
from passrw.services.rwby import RwbyClient, SiteUnavailable
from passrw.util.time import local_now, utcnow

logger = logging.getLogger(__name__)

# Бэкофф перезапуска упавшей задачи трекинга (секунды).
ERROR_BACKOFF_START = 5.0
ERROR_BACKOFF_MAX = 60.0


class TrackingManager:
    def __init__(self, bot: Bot, database: Database, settings: Settings, client: RwbyClient) -> None:
        self.bot = bot
        self.db = database
        self.settings = settings
        self.client = client
        self._tasks: dict[int, asyncio.Task] = {}
        self._started_at = time.monotonic()

    # ── управление задачами ────────────────────────────────────

    @property
    def running_count(self) -> int:
        return len(self._tasks)

    @property
    def uptime_seconds(self) -> int:
        return int(time.monotonic() - self._started_at)

    def is_running(self, tracking_id: int) -> bool:
        task = self._tasks.get(tracking_id)
        return bool(task and not task.done())

    def add(self, tracking_id: int, start_delay: float = 0.0) -> None:
        if self.is_running(tracking_id):
            return
        task = asyncio.create_task(
            self._supervise(tracking_id, start_delay), name=f"tracking-{tracking_id}"
        )
        self._tasks[tracking_id] = task
        task.add_done_callback(lambda _t, tid=tracking_id: self._forget(tid))
        ACTIVE_TRACKINGS.set(len(self._tasks))
        logger.info("Трекинг #%s запущен (всего активно: %s)", tracking_id, len(self._tasks))

    def _forget(self, tracking_id: int) -> None:
        task = self._tasks.get(tracking_id)
        if task is not None and task.done():
            self._tasks.pop(tracking_id, None)
        ACTIVE_TRACKINGS.set(len(self._tasks))

    async def cancel(self, tracking_id: int) -> None:
        task = self._tasks.pop(tracking_id, None)
        ACTIVE_TRACKINGS.set(len(self._tasks))
        if task is None:
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def restore(self) -> int:
        expired = await self.expire()
        async with self.db.session() as session:
            trackings = await repo.list_active_trackings(session)
            ids = [t.id for t in trackings]
        for position, tracking_id in enumerate(ids):
            # Разносим первый опрос: рестарт не должен давать залп запросов.
            start_delay = min(30.0, position * 2.0 + random.uniform(0.0, 1.0))
            self.add(tracking_id, start_delay=start_delay)
        logger.info(
            "Восстановлено трекингов: %s (просрочено закрыто: %s)", len(ids), expired
        )
        return len(ids)

    async def expire(self) -> int:
        """Закрывает трекинги с прошедшей датой и сообщает пользователям."""
        now_local = local_now(self.settings.timezone)
        today = now_local.date()
        expired: list[tuple[int, str]] = []
        expired_ids: list[int] = []
        async with self.db.session() as session:
            for tracking in await repo.list_active_trackings(session):
                if tracking.travel_date >= today:
                    continue
                user_settings = await repo.get_user_settings(session, tracking.chat_id)
                await repo.deactivate_tracking(session, tracking.id, "expired")
                expired_ids.append(tracking.id)
                if in_quiet_hours(
                    _quiet_from(user_settings), _quiet_to(user_settings), now_local
                ):
                    logger.info(
                        "Закрытие трекинга #%s: уведомление пропущено (тихие часы)",
                        tracking.id,
                    )
                    continue
                expired.append(
                    (
                        tracking.chat_id,
                        f"⌛ Трекинг {tracking.from_station} → {tracking.to_station} "
                        f"({tracking.travel_date.strftime('%d.%m.%Y')}) закрыт: "
                        "дата поездки прошла.",
                    )
                )
        for tracking_id in expired_ids:
            await self.cancel(tracking_id)
        for chat_id, text in expired:
            await self._send_deactivation(chat_id, text)
        return len(expired_ids)

    async def stop(
        self,
        tracking_id: int,
        *,
        reason: str = "user_stop",
        notify_user: bool = False,
        message: str | None = None,
    ) -> bool:
        async with self.db.session() as session:
            tracking = await repo.get_tracking(session, tracking_id)
            if tracking is None:
                await self.cancel(tracking_id)
                return False
            chat_id = tracking.chat_id
            was_active = tracking.is_active
            if was_active:
                await repo.deactivate_tracking(session, tracking_id, reason)
        await self.cancel(tracking_id)
        if notify_user and was_active and message:
            try:
                await self.bot.send_message(chat_id, message)
            except TelegramAPIError as exc:
                logger.warning("Не удалось уведомить %s об остановке: %s", chat_id, exc)
        logger.info("Трекинг #%s остановлен (reason=%s)", tracking_id, reason)
        return was_active

    async def stop_all_for_chat(self, chat_id: int, reason: str = "user_stop_all") -> int:
        async with self.db.session() as session:
            trackings = await repo.list_user_trackings(session, chat_id)
            ids = [t.id for t in trackings]
            for tracking_id in ids:
                await repo.deactivate_tracking(session, tracking_id, reason)
        for tracking_id in ids:
            await self.cancel(tracking_id)
        return len(ids)

    async def shutdown(self) -> None:
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        ACTIVE_TRACKINGS.set(0)
        logger.info("Менеджер трекингов остановлен")

    # ── основной цикл ──────────────────────────────────────────

    async def _supervise(self, tracking_id: int, start_delay: float = 0.0) -> None:
        """Держит задачу трекинга живой: падение итерации не теряет трекинг."""
        if start_delay > 0:
            await asyncio.sleep(start_delay)
        backoff = ERROR_BACKOFF_START
        while True:
            started = time.monotonic()
            try:
                await self._run(tracking_id)
                return
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — перезапускаем вместо тихой смерти
                if time.monotonic() - started >= 300:
                    backoff = ERROR_BACKOFF_START
                logger.exception(
                    "Задача трекинга #%s упала; перезапуск через %.0f с",
                    tracking_id,
                    backoff,
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, ERROR_BACKOFF_MAX)

    async def _run(self, tracking_id: int) -> None:
        last_heartbeat = time.monotonic()
        try:
            while True:
                async with self.db.session() as session:
                    tracking = await repo.get_tracking(session, tracking_id)
                    if tracking is None or not tracking.is_active:
                        return
                    user_settings = await repo.get_user_settings(session, tracking.chat_id)
                    snapshot = _Snapshot.from_orm(
                        tracking, user_settings, self.settings.notify_cooldown
                    )

                if snapshot.heartbeat_enabled:
                    elapsed = time.monotonic() - last_heartbeat
                    if elapsed >= snapshot.heartbeat_interval:
                        if not in_quiet_hours(
                            _quiet_from(user_settings),
                            _quiet_to(user_settings),
                            local_now(self.settings.timezone),
                        ):
                            await self._send_heartbeat(
                                snapshot.chat_id, tracking_id, snapshot.seats_available
                            )
                        last_heartbeat = time.monotonic()

                CHECKS_TOTAL.inc()
                try:
                    route = await self.client.search(
                        snapshot.from_station, snapshot.to_station, snapshot.travel_date
                    )
                    trains = route.trains
                except LayoutChanged as exc:
                    CHECK_FAILURES.labels(reason="layout").inc()
                    await self._handle_failure(tracking_id, snapshot, "layout", str(exc))
                    await asyncio.sleep(max(900, self.settings.check_interval * 10))
                    continue
                except SiteUnavailable as exc:
                    CHECK_FAILURES.labels(reason="site").inc()
                    await self._handle_failure(tracking_id, snapshot, "site", str(exc))
                    await asyncio.sleep(max(300, self.settings.check_interval * 5))
                    continue
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 — задача не должна умирать молча
                    CHECK_FAILURES.labels(reason="unexpected").inc()
                    logger.exception("Неожиданная ошибка проверки #%s", tracking_id)
                    await self._handle_failure(tracking_id, snapshot, "error", str(exc)[:200])
                    await asyncio.sleep(max(300, self.settings.check_interval * 5))
                    continue

                train = pick_train(trains, snapshot.depart_time, snapshot.train_number)
                now = utcnow()
                if train is None:
                    async with self.db.session() as session:
                        await repo.update_tracking(
                            session,
                            tracking_id,
                            requests_count=snapshot.requests_count + 1,
                            last_checked_at=now,
                            consecutive_failures=0,
                            last_error=None,
                        )
                    await asyncio.sleep(self._next_delay(snapshot))
                    continue

                filters = snapshot.filters(user_settings)
                evaluation = evaluate_train(train, filters)
                delay = self._next_delay(snapshot)
                should_notify = self._should_notify(snapshot, evaluation, user_settings)

                async with self.db.session() as session:
                    fresh = await repo.get_tracking(session, tracking_id)
                    if fresh is None:
                        return
                    await repo.add_sample_if_changed(
                        session, fresh, seats=evaluation.max_seats, min_price=evaluation.min_price
                    )
                    await repo.update_tracking(
                        session,
                        tracking_id,
                        seats_available=evaluation.max_seats,
                        min_price=evaluation.min_price,
                        train_number=train.number,
                        requests_count=snapshot.requests_count + 1,
                        last_checked_at=now,
                        consecutive_failures=0,
                        last_error=None,
                        layout_alerted=False,
                    )

                notified = False
                if should_notify:
                    notified = await self._notify(snapshot, evaluation)
                    if notified:
                        async with self.db.session() as session:
                            await repo.update_tracking(
                                session,
                                tracking_id,
                                last_signature=evaluation.signature,
                                last_notified_at=utcnow(),
                            )
                            await repo.bump_stats(session, snapshot.chat_id, notifications=1)
                            await repo.log_user_event(
                                session,
                                snapshot.chat_id,
                                "NOTIFY",
                                f"tracking={tracking_id} train={train.number} "
                                f"seats={evaluation.max_seats}",
                            )
                        NOTIFICATIONS_TOTAL.inc()

                if notified and snapshot.stop_on_success:
                    async with self.db.session() as session:
                        await repo.deactivate_tracking(session, tracking_id, "success")
                    await self._send_deactivation(
                        snapshot.chat_id,
                        f"✅ Трекинг {snapshot.from_station} → {snapshot.to_station} "
                        f"({snapshot.travel_date.strftime('%d.%m.%Y')}) остановлен: "
                        "подходящие места найдены.",
                    )
                    logger.info("Трекинг #%s завершён после успешной находки", tracking_id)
                    return

                await asyncio.sleep(delay)
        except asyncio.CancelledError:
            logger.debug("Трекинг #%s отменён", tracking_id)
            raise
        finally:
            ACTIVE_TRACKINGS.set(len(self._tasks))

    # ── вспомогательные операции ───────────────────────────────

    def _next_delay(self, snapshot: _Snapshot) -> int:
        return adaptive_interval(
            self.settings.check_interval,
            self.settings.min_check_interval,
            snapshot.travel_date,
            local_now(self.settings.timezone).date(),
        )

    def _should_notify(
        self, snapshot: _Snapshot, evaluation: Evaluation, settings: UserSettings | None
    ) -> bool:
        if not evaluation.has_matches or not evaluation.signature:
            return False
        if evaluation.signature == snapshot.last_signature:
            return False
        if snapshot.last_notified_at is not None:
            elapsed = (utcnow() - snapshot.last_notified_at).total_seconds()
            if elapsed < snapshot.notify_cooldown:
                return False
        return not in_quiet_hours(
            _quiet_from(settings),
            _quiet_to(settings),
            local_now(self.settings.timezone),
        )

    async def _notify(self, snapshot: _Snapshot, evaluation: Evaluation) -> bool:
        text = build_alert(
            from_station=snapshot.from_station,
            to_station=snapshot.to_station,
            travel_date=snapshot.travel_date,
            passengers=snapshot.passengers,
            evaluation=evaluation,
        )
        url = self.client.buy_url(snapshot.from_station, snapshot.to_station, snapshot.travel_date)
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="🎫 Купить билет", url=url)]]
        )
        try:
            await self.bot.send_message(
                snapshot.chat_id, text, reply_markup=keyboard, disable_web_page_preview=True
            )
            return True
        except TelegramAPIError as exc:
            logger.warning("Не удалось отправить уведомление %s: %s", snapshot.chat_id, exc)
            if "blocked" in str(exc).casefold():
                # Пользователь заблокировал бота: снимаем его трекинги без отмены
                # текущей задачи (та завершится сама на следующей итерации).
                async with self.db.session() as session:
                    for tracking in await repo.list_user_trackings(session, snapshot.chat_id):
                        await repo.deactivate_tracking(session, tracking.id, "blocked")
            return False

    async def _send_heartbeat(self, chat_id: int, tracking_id: int, seats: int) -> None:
        try:
            await self.bot.send_message(chat_id, build_heartbeat(tracking_id, seats))
        except TelegramAPIError as exc:
            logger.debug("Heartbeat не доставлен %s: %s", chat_id, exc)

    async def _send_deactivation(self, chat_id: int, text: str) -> None:
        """Короткое уведомление о том, что трекинг закрыт."""
        if self.bot is None:
            return
        try:
            await self.bot.send_message(chat_id, text)
        except TelegramAPIError as exc:
            logger.debug("Не удалось сообщить о закрытии трекинга %s: %s", chat_id, exc)

    async def _handle_failure(
        self,
        tracking_id: int,
        snapshot: _Snapshot,
        kind: str,
        error: str,
    ) -> None:
        failures = snapshot.consecutive_failures + 1
        async with self.db.session() as session:
            await repo.update_tracking(
                session,
                tracking_id,
                consecutive_failures=failures,
                last_checked_at=utcnow(),
                last_error=f"{kind}: {error}"[:256],
            )
        if failures == self.settings.parse_failure_alert_threshold:
            route = f"{snapshot.from_station} → {snapshot.to_station}"
            hint = (
                "Похоже, изменилась вёрстка pass.rw.by — проверьте парсер."
                if kind == "layout"
                else "Проверки трекингов падают подряд."
            )
            await self._alert_admin(f"⚠️ {hint}\n{route}, трекинг #{tracking_id}\n{error}")

    async def _alert_admin(self, text: str) -> None:
        if not self.settings.alert_chat_id:
            return
        try:
            await self.bot.send_message(self.settings.alert_chat_id, text)
        except TelegramAPIError as exc:
            logger.warning("Не удалось отправить алерт админу: %s", exc)

    async def send_user_message(self, chat_id: int, text: str) -> bool:
        try:
            await self.bot.send_message(chat_id, text, disable_web_page_preview=True)
            return True
        except TelegramAPIError as exc:
            logger.warning("Рассылка не доставлена %s: %s", chat_id, exc)
            return False


class _Snapshot:
    """Снимок трекинга + настроек, чтобы не держать сессию во время HTTP-запроса."""

    __slots__ = (
        "chat_id",
        "from_station",
        "to_station",
        "travel_date",
        "passengers",
        "train_number",
        "depart_time",
        "heartbeat_enabled",
        "heartbeat_interval",
        "requests_count",
        "seats_available",
        "last_signature",
        "last_notified_at",
        "consecutive_failures",
        "stop_on_success",
        "notify_cooldown",
        "threshold_override",
        "carriage_override",
        "price_override",
    )

    @classmethod
    def from_orm(
        cls, tracking: Tracking, settings: UserSettings | None, cooldown: int
    ) -> _Snapshot:
        snapshot = cls()
        snapshot.chat_id = tracking.chat_id
        snapshot.from_station = tracking.from_station
        snapshot.to_station = tracking.to_station
        snapshot.travel_date = tracking.travel_date
        snapshot.passengers = tracking.passengers
        snapshot.train_number = tracking.train_number
        snapshot.depart_time = tracking.depart_time
        snapshot.heartbeat_enabled = tracking.heartbeat_enabled
        snapshot.heartbeat_interval = tracking.heartbeat_interval
        snapshot.requests_count = tracking.requests_count
        snapshot.seats_available = tracking.seats_available
        snapshot.last_signature = tracking.last_signature
        snapshot.last_notified_at = tracking.last_notified_at
        snapshot.consecutive_failures = tracking.consecutive_failures
        snapshot.stop_on_success = tracking.stop_on_success
        snapshot.notify_cooldown = cooldown
        snapshot.threshold_override = tracking.notify_threshold
        snapshot.carriage_override = tracking.carriage_filter
        snapshot.price_override = tracking.max_price
        return snapshot

    def filters(self, settings: UserSettings | None) -> Filters:
        threshold = self.threshold_override
        if threshold is None:
            threshold = settings.notify_threshold if settings else 1
        carriage = self.carriage_override
        if carriage is None and settings is not None:
            carriage = settings.carriage_filter
        max_price = self.price_override
        if max_price is None and settings is not None:
            max_price = settings.max_price
        return Filters(
            threshold=max(self.passengers, threshold or 1),
            carriage=carriage,
            max_price=max_price,
        )


def _quiet_from(settings: UserSettings | None) -> str | None:
    return settings.quiet_from if settings else None


def _quiet_to(settings: UserSettings | None) -> str | None:
    return settings.quiet_to if settings else None
