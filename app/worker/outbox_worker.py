"""Фоновый воркер: вытаскивает задачи из очереди и создаёт страницы в Notion.

Обработка внутри пачки — последовательная. Это осознанно: параллелить
запросы к API с лимитом 3 rps смысла нет, а последовательность делает
поведение предсказуемым и упрощает отладку.
"""

from __future__ import annotations

import asyncio
import logging

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError

from app.notion.client import NotionClient, NotionPermanentError, NotionTransientError
from app.notion.mapper import build_page_payload
from app.storage.outbox import Outbox, OutboxRow

log = logging.getLogger(__name__)


class OutboxWorker:
    def __init__(
        self,
        *,
        outbox: Outbox,
        notion: NotionClient,
        bot: Bot,
        database_id: str,
        poll_interval: float = 1.0,
        batch_size: int = 10,
    ):
        self._outbox = outbox
        self._notion = notion
        self._bot = bot
        self._database_id = database_id
        self._poll_interval = poll_interval
        self._batch_size = batch_size
        self._stopping = asyncio.Event()

    def stop(self) -> None:
        self._stopping.set()

    async def run(self) -> None:
        log.info("Воркер запущен")
        while not self._stopping.is_set():
            try:
                processed = await self._tick()
            except Exception:  # noqa: BLE001 — воркер не должен умирать
                log.exception("Непредвиденная ошибка в цикле воркера")
                processed = 0

            if processed == 0:
                # Ждём либо новый тик, либо сигнал остановки — что раньше.
                try:
                    await asyncio.wait_for(
                        self._stopping.wait(), timeout=self._poll_interval
                    )
                except asyncio.TimeoutError:
                    pass
        log.info("Воркер остановлен")

    async def _tick(self) -> int:
        await self._outbox.reclaim_stale()
        rows = await self._outbox.lease_batch(self._batch_size)
        for row in rows:
            await self._process(row)
        return len(rows)

    async def _process(self, row: OutboxRow) -> None:
        payload = build_page_payload(row.item, self._database_id)
        try:
            page = await self._notion.create_page(payload)

        except NotionTransientError as e:
            status, delay = await self._outbox.mark_retry(
                row.id, row.attempts, str(e), retry_after=e.retry_after
            )
            if status == "dead":
                log.error("Задача #%s исчерпала попытки: %s", row.id, e)
                await self._notify_failure(row, str(e))
            else:
                log.warning("Задача #%s: временная ошибка, повтор через %.0fс (%s)",
                            row.id, delay, e)
            return

        except NotionPermanentError as e:
            log.error("Задача #%s: фатальная ошибка Notion: %s", row.id, e)
            await self._outbox.mark_dead(row.id, row.attempts, str(e))
            await self._notify_failure(row, str(e))
            return

        page_id = page.get("id", "")
        page_url = page.get("url", "")
        await self._outbox.mark_done(row.id, page_id, page_url)
        log.info("Задача #%s -> %s", row.id, page_url)
        await self._notify_success(row, page_url)

    # ---------- обратная связь пользователю ----------

    async def _edit_ack(self, row: OutboxRow, text: str) -> None:
        """Редактируем то самое «Принято…», а не шлём новое сообщение.

        Так в чате не растёт мусор: одно сообщение пользователя — один ответ бота.
        """
        if row.ack_message_id is None:
            return
        try:
            await self._bot.edit_message_text(
                chat_id=row.chat_id,
                message_id=row.ack_message_id,
                text=text,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
        except TelegramAPIError as e:
            log.warning("Не удалось отредактировать ack для #%s: %s", row.id, e)

    async def _notify_success(self, row: OutboxRow, url: str) -> None:
        title = row.item.title
        text = f'✅ <a href="{url}">{_escape(title)}</a>' if url else f"✅ {_escape(title)}"
        await self._edit_ack(row, text)

    async def _notify_failure(self, row: OutboxRow, error: str) -> None:
        await self._edit_ack(
            row,
            "⚠️ Не смог отправить в Notion.\n"
            f"<code>{_escape(error[:300])}</code>\n\n"
            "Сообщение сохранено локально — попробуйте /retry позже.",
        )


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")