"""HTTP-клиент Notion API поверх httpx.

Отвечает ровно за две вещи:
  1. корректно сходить в сеть (заголовки, таймаут, rate limit);
  2. превратить любой ответ в понятное доменное исключение —
     «повторяемая ошибка» или «фатальная ошибка».

Решение о ретрае принимает воркер, а не клиент: клиент только классифицирует.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx

log = logging.getLogger(__name__)

API_BASE = "https://api.notion.com/v1"


class NotionError(Exception):
    """Базовая ошибка Notion."""


class NotionTransientError(NotionError):
    """Временная: сеть, 5xx, 429. Имеет смысл повторить."""

    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class NotionPermanentError(NotionError):
    """Фатальная: 400/401/403/404. Повтор не поможет — нужен человек."""


class RateLimiter:
    """Простейший «не чаще, чем раз в N секунд».

    Notion разрешает ~3 запроса в секунду на интеграцию; при превышении
    отдаёт 429. Дешевле не упираться в лимит, чем ловить и ретраить.
    """

    def __init__(self, rps: float):
        self._min_interval = 1.0 / rps if rps > 0 else 0.0
        self._lock = asyncio.Lock()
        self._last = 0.0

    async def acquire(self) -> None:
        if self._min_interval <= 0:
            return
        async with self._lock:
            wait = self._last + self._min_interval - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self._last = time.monotonic()


class NotionClient:
    def __init__(
        self,
        token: str,
        *,
        version: str = "2022-06-28",
        timeout: float = 20.0,
        rps: float = 2.5,
    ):
        self._limiter = RateLimiter(rps)
        self._client = httpx.AsyncClient(
            base_url=API_BASE,
            timeout=timeout,
            headers={
                "Authorization": f"Bearer {token}",
                "Notion-Version": version,
                "Content-Type": "application/json",
            },
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> NotionClient:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    # ---------- низкий уровень ----------

    async def _request(self, method: str, path: str, json: dict | None = None) -> dict:
        await self._limiter.acquire()
        try:
            resp = await self._client.request(method, path, json=json)
        except httpx.TimeoutException as e:
            raise NotionTransientError(f"таймаут запроса к Notion: {e}") from e
        except httpx.TransportError as e:
            raise NotionTransientError(f"сетевая ошибка: {e}") from e

        if resp.status_code == 429:
            retry_after = float(resp.headers.get("Retry-After", 5))
            raise NotionTransientError("429 rate limited", retry_after=retry_after)

        if resp.status_code >= 500:
            raise NotionTransientError(f"{resp.status_code} от Notion: {resp.text[:300]}")

        if resp.status_code >= 400:
            raise NotionPermanentError(
                f"{resp.status_code} от Notion: {resp.text[:500]}"
            )

        return resp.json()

    # ---------- прикладной уровень ----------

    async def create_page(self, payload: dict) -> dict:
        """POST /v1/pages -> объект страницы (нас интересуют id и url)."""
        return await self._request("POST", "/pages", json=payload)

    async def retrieve_database(self, database_id: str) -> dict:
        """GET /v1/databases/{id} — используем на старте, чтобы упасть громко.

        Если интеграция не расшарена на базу или id неверный, лучше узнать это
        при запуске, а не когда придёт первое сообщение пользователя.
        """
        return await self._request("GET", f"/databases/{database_id}")