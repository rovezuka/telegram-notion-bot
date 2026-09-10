import asyncio
import time

import pytest
import pytest_asyncio

from app.domain.models import InboxItem, Priority
from app.storage.outbox import Outbox


def make_item(title: str = "Задача") -> InboxItem:
    return InboxItem(title=title, body=title, tags=["дом"], priority=Priority.HIGH)


@pytest_asyncio.fixture
async def outbox(tmp_path):
    ob = Outbox(tmp_path / "outbox.sqlite3", max_attempts=3, lease_ttl=0.05)
    await ob.connect()
    yield ob
    await ob.close()


async def enqueue(ob: Outbox, msg_id: int) -> int | None:
    return await ob.enqueue(
        idempotency_key=f"1:{msg_id}",
        chat_id=1,
        user_id=42,
        tg_message_id=msg_id,
        item=make_item(f"Задача {msg_id}"),
    )


async def test_enqueue_and_lease(outbox):
    row_id = await enqueue(outbox, 100)
    assert row_id is not None

    rows = await outbox.lease_batch(10)
    assert len(rows) == 1
    assert rows[0].item.title == "Задача 100"
    assert rows[0].item.priority is Priority.HIGH
    assert rows[0].item.tags == ["дом"]

    # повторный lease не отдаёт ту же строку — она уже in_flight
    assert await outbox.lease_batch(10) == []


async def test_duplicate_update_is_ignored(outbox):
    assert await enqueue(outbox, 200) is not None
    assert await enqueue(outbox, 200) is None
    assert (await outbox.stats())["pending"] == 1


async def test_mark_done(outbox):
    await enqueue(outbox, 300)
    (row,) = await outbox.lease_batch(10)
    await outbox.mark_done(row.id, "page-id", "https://notion.so/page")
    assert (await outbox.stats())["done"] == 1
    assert await outbox.lease_batch(10) == []


async def test_retry_uses_backoff_then_dies(outbox):
    await enqueue(outbox, 400)
    (row,) = await outbox.lease_batch(10)

    status, delay = await outbox.mark_retry(row.id, row.attempts, "503")
    assert status == "pending"
    assert delay > 0
    # backoff ещё не истёк — задачу пока не отдадут
    assert await outbox.lease_batch(10) == []

    # притворимся, что время подошло
    await outbox.db.execute(
        "UPDATE outbox SET next_attempt_at = ? WHERE id = ?", (time.time() - 1, row.id)
    )
    await outbox.db.commit()

    (row,) = await outbox.lease_batch(10)
    assert row.attempts == 1
    status, _ = await outbox.mark_retry(row.id, row.attempts, "503")
    assert status == "pending"

    await outbox.db.execute(
        "UPDATE outbox SET next_attempt_at = ? WHERE id = ?", (time.time() - 1, row.id)
    )
    await outbox.db.commit()
    (row,) = await outbox.lease_batch(10)
    status, _ = await outbox.mark_retry(row.id, row.attempts, "503")
    assert status == "dead"  # max_attempts = 3
    assert (await outbox.stats())["dead"] == 1


async def test_retry_after_overrides_backoff(outbox):
    await enqueue(outbox, 450)
    (row,) = await outbox.lease_batch(10)
    _, delay = await outbox.mark_retry(row.id, row.attempts, "429", retry_after=1.5)
    assert delay == pytest.approx(1.5)


async def test_stale_in_flight_is_reclaimed(outbox):
    await enqueue(outbox, 500)
    await outbox.lease_batch(10)
    assert (await outbox.stats())["in_flight"] == 1

    await asyncio.sleep(0.06)  # lease_ttl = 0.05
    assert await outbox.reclaim_stale() == 1
    assert (await outbox.stats())["pending"] == 1


async def test_requeue_dead(outbox):
    await enqueue(outbox, 600)
    (row,) = await outbox.lease_batch(10)
    await outbox.mark_dead(row.id, row.attempts, "400 invalid property")
    assert (await outbox.stats())["dead"] == 1

    assert await outbox.requeue_dead(user_id=42) == 1
    assert (await outbox.stats())["pending"] == 1
    assert await outbox.requeue_dead(user_id=999) == 0


async def test_stats_are_scoped_by_user(outbox):
    await enqueue(outbox, 700)
    assert (await outbox.stats(user_id=42))["pending"] == 1
    assert (await outbox.stats(user_id=1))["pending"] == 0
