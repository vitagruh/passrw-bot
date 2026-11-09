"""Асинхронный клиент pass.rw.by: ретраи, общий кэш и защита от шторма запросов."""

from __future__ import annotations

import asyncio
import logging
import random
import time
from datetime import date

import httpx

from passrw.config import Settings
from passrw.domain.railway import Train
from passrw.metrics import HTTP_REQUESTS, PARSER_LAYOUT_ERRORS
from passrw.services.parser import LayoutChanged, looks_like_results_page, parse_route_html

logger = logging.getLogger(__name__)

ROUTE_URL = "https://pass.rw.by/ru/route/"

USER_AGENTS = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36 Edg/122.0.0.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64; rv:124.0) Gecko/20100101 Firefox/124.0",
)


class SiteUnavailable(Exception):
    """Сайт ответил ошибкой, таймаутом или антибот-заглушкой."""


class RwbyClient:
    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._settings = settings
        self._semaphore = asyncio.Semaphore(settings.http_concurrency)
        self._cache: dict[tuple[str, str, str], tuple[float, list[Train]]] = {}
        self._locks: dict[tuple[str, str, str], asyncio.Lock] = {}
        self._client = httpx.AsyncClient(
            timeout=settings.request_timeout,
            follow_redirects=True,
            transport=transport,
            headers={
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
            },
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def search(self, from_station: str, to_station: str, travel_date: date) -> list[Train]:
        key = (from_station, to_station, travel_date.isoformat())
        now = time.monotonic()
        cached = self._cache.get(key)
        if cached and cached[0] > now:
            return list(cached[1])

        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            cached = self._cache.get(key)
            if cached and cached[0] > time.monotonic():
                return list(cached[1])

            html = await self._fetch(from_station, to_station, travel_date)
            trains = parse_route_html(html)
            if not trains and not looks_like_results_page(html):
                PARSER_LAYOUT_ERRORS.inc()
                raise LayoutChanged("таблица результатов не найдена на странице")

            if self._settings.http_cache_ttl > 0:
                self._cache[key] = (
                    time.monotonic() + self._settings.http_cache_ttl,
                    trains,
                )
                self._prune_cache()
            return list(trains)

    async def _fetch(self, from_station: str, to_station: str, travel_date: date) -> str:
        params = {"from": from_station, "to": to_station, "date": travel_date.isoformat()}
        last_error: Exception | None = None

        for attempt in range(3):
            try:
                async with self._semaphore:
                    response = await self._client.get(
                        ROUTE_URL, params=params, headers={"User-Agent": random.choice(USER_AGENTS)}
                    )
                if response.status_code in (403, 429) or response.status_code >= 500:
                    HTTP_REQUESTS.labels(status=str(response.status_code)).inc()
                    last_error = SiteUnavailable(f"HTTP {response.status_code}")
                    if attempt < 2:
                        await asyncio.sleep(1.5 * (attempt + 1) + random.random())
                        continue
                    raise last_error

                response.raise_for_status()
                HTTP_REQUESTS.labels(status="200").inc()
                text = response.text
                if "captcha" in text.casefold() and "sch-table" not in text:
                    raise SiteUnavailable("сайт вернул капчу")
                return text
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                HTTP_REQUESTS.labels(status="error").inc()
                last_error = exc
                if attempt < 2:
                    await asyncio.sleep(1.5 * (attempt + 1))
                    continue

        raise SiteUnavailable(str(last_error) if last_error else "неизвестная ошибка запроса")

    def _prune_cache(self) -> None:
        if len(self._cache) <= 256:
            return
        now = time.monotonic()
        expired = [key for key, (expires, _) in self._cache.items() if expires <= now]
        for key in expired or list(self._cache)[:64]:
            self._cache.pop(key, None)
            self._locks.pop(key, None)

    @staticmethod
    def buy_url(from_station: str, to_station: str, travel_date: date) -> str:
        from urllib.parse import urlencode

        query = urlencode({"from": from_station, "to": to_station, "date": travel_date.isoformat()})
        return f"{ROUTE_URL}?{query}"
