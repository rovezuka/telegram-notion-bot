"""Точка входа: сборка зависимостей и запуск двух корутин.

Бот (long polling) и воркер живут в одном процессе и в одном event loop.
Это осознанное упрощение для персонального сервиса: меньше движущихся частей,
одна база SQLite, один деплой. Разнести на два процесса можно позже —
очередь уже развязывает их логически.
"""

from __future__ import annotations

import asyncio
import logging
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from app.bot.handlers import router
from app.bot.middlewares import AccessMiddleware, ThrottleMiddleware
from app.config import Settings, get_settings
from app.notion.client import NotionClient, NotionError
from app.storage.outbox import Outbox
from app.worker.outbox_worker import OutboxWorker

log = logging.getLogger("app")


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=level.upper(),
        format="%(asctime)s %(levelname)-8s %(name)s | %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)


async def run(settings: Settings) -> None:
    outbox = Outbox(
        settings.db_path,
        max_attempts=settings.max_attempts,
        lease_ttl=settings.lease_ttl_seconds,
    )
    await outbox.connect()

    notion = NotionClient(
        settings.notion_token,
        version=settings.notion_version,
        timeout=settings.notion_timeout,
        rps=settings.notion_rps,
    )

    # Fail fast: проверяем доступ к базе на старте, а не на первом сообщении.
    try:
        db = await notion.retrieve_database(settings.notion_database_id)
        title = "".join(t.get("plain_text", "") for t in db.get("title", []))
        log.info("Notion база подключена: «%s»", title or settings.notion_database_id)
    except NotionError as e:
        log.error("Нет доступа к базе Notion: %s", e)
        log.error("Проверьте NOTION_DATABASE_ID и что интеграция добавлена "
                  "в Connections у страницы с базой.")
        await notion.aclose()
        await outbox.close()
        raise SystemExit(1)

    bot = Bot(
        settings.telegram_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher()
    # outbox прокидывается в хендлеры через workflow_data — явный DI, без глобалов
    dp["outbox"] = outbox
    dp.message.outer_middleware(AccessMiddleware(settings.allowlist))
    dp.message.outer_middleware(ThrottleMiddleware())
    dp.include_router(router)

    worker = OutboxWorker(
        outbox=outbox,
        notion=notion,
        bot=bot,
        database_id=settings.notion_database_id,
        poll_interval=settings.worker_poll_interval,
        batch_size=settings.worker_batch_size,
    )

    worker_task = asyncio.create_task(worker.run(), name="outbox-worker")
    try:
        await dp.start_polling(bot, handle_signals=True)
    finally:
        log.info("Останавливаюсь…")
        worker.stop()
        await asyncio.wait_for(worker_task, timeout=10)
        await notion.aclose()
        await outbox.close()
        await bot.session.close()


def main() -> None:
    settings = get_settings()
    setup_logging(settings.log_level)
    try:
        asyncio.run(run(settings))
    except (KeyboardInterrupt, SystemExit):
        pass


if __name__ == "__main__":
    main()