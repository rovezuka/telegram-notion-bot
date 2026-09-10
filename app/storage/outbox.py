"""Транзакционный Outbox на SQLite.

Хендлер делает ровно одну быструю операцию — INSERT в локальную очередь —
и сразу отвечает. Доставку в Notion берёт на себя фоновый воркер,
который умеет ретраить.

Идемпотентность: idempotency_key = "<chat_id>:<message_id>" с UNIQUE-индексом.
Telegram при сетевых сбоях может прислать один и тот же апдейт дважды —
второй INSERT просто отвалится, дубля в Notion не будет.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

import aiosqlite

from app.domain.models import InboxItem

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS outbox (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    idempotency_key  TEXT    NOT NULL UNIQUE,
    chat_id          INTEGER NOT NULL,
    user_id          INTEGER NOT NULL,
    tg_message_id    INTEGER NOT NULL,
    ack_message_id   INTEGER,              -- id нашего ответа «принято», его потом редактируем
    payload          TEXT    NOT NULL,     -- InboxItem в JSON
    status           TEXT    NOT NULL DEFAULT 'pending',
    attempts         INTEGER NOT NULL DEFAULT 0,
    next_attempt_at  REAL    NOT NULL DEFAULT 0,
    leased_until     REAL,
    last_error       TEXT,
    notion_page_id   TEXT,
    notion_page_url  TEXT,
    created_at       REAL    NOT NULL,
    updated_at       REAL    NOT NULL,
    CHECK (status IN ('pending', 'in_flight', 'done', 'dead'))
);

CREATE INDEX IF NOT EXISTS ix_outbox_ready
    ON outbox (status, next_attempt_at);
CREATE INDEX IF NOT EXISTS ix_outbox_user
    ON outbox (user_id, created_at);
"""


@dataclass(slots=True)
class OutboxRow:
    id: int
    idempotency_key: str
    chat_id: int
    user_id: int
    tg_message_id: int
    ack_message_id: int | None
    item: InboxItem
    attempts: int

    @classmethod
    def from_db(cls, row: aiosqlite.Row) -> OutboxRow:
        return cls(
            id=row["id"],
            idempotency_key=row["idempotency_key"],
            chat_id=row["chat_id"],
            user_id=row["user_id"],
            tg_message_id=row["tg_message_id"],
            ack_message_id=row["ack_message_id"],
            item=InboxItem.from_json(row["payload"]),
            attempts=row["attempts"],
        )


class Outbox:
    """Тонкая обёртка над одним соединением aiosqlite."""

    def __init__(self, db_path: Path, *, max_attempts: int = 8, lease_ttl: float = 120.0):
        self._path = Path(db_path)
        self._max_attempts = max_attempts
        self._lease_ttl = lease_ttl
        self._db: aiosqlite.Connection | None = None

    # ---------- жизненный цикл ----------

    async def connect(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self._path)
        self._db.row_factory = aiosqlite.Row
        await self._db.executescript(SCHEMA)
        await self._db.commit()
        await self.reclaim_stale()

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    @property
    def db(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("Outbox не подключён: вызовите await outbox.connect()")
        return self._db

    # ---------- запись (со стороны бота) ----------

    async def enqueue(
        self,
        *,
        idempotency_key: str,
        chat_id: int,
        user_id: int,
        tg_message_id: int,
        item: InboxItem,
    ) -> int | None:
        """Кладём задачу в очередь. None = такой ключ уже есть (дубль апдейта)."""
        now = time.time()
        cur = await self.db.execute(
            """
            INSERT OR IGNORE INTO outbox
                (idempotency_key, chat_id, user_id, tg_message_id, payload,
                 status, next_attempt_at, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, ?)
            """,
            (idempotency_key, chat_id, user_id, tg_message_id,
             item.to_json(), now, now, now),
        )
        await self.db.commit()
        return cur.lastrowid if cur.rowcount else None

    async def attach_ack(self, row_id: int, ack_message_id: int) -> None:
        """Запоминаем id ответа бота, чтобы воркер потом дописал в него ссылку."""
        await self.db.execute(
            "UPDATE outbox SET ack_message_id = ?, updated_at = ? WHERE id = ?",
            (ack_message_id, time.time(), row_id),
        )
        await self.db.commit()

    # ---------- чтение (со стороны воркера) ----------

    async def lease_batch(self, limit: int) -> list[OutboxRow]:
        """Атомарно забираем пачку готовых задач и помечаем их in_flight."""
        now = time.time()
        await self.db.execute("BEGIN IMMEDIATE")
        try:
            cur = await self.db.execute(
                """
                SELECT * FROM outbox
                WHERE status = 'pending' AND next_attempt_at <= ?
                ORDER BY next_attempt_at, id
                LIMIT ?
                """,
                (now, limit),
            )
            rows = await cur.fetchall()
            if rows:
                ids = [r["id"] for r in rows]
                placeholders = ",".join("?" * len(ids))
                await self.db.execute(
                    f"""
                    UPDATE outbox
                       SET status = 'in_flight', leased_until = ?, updated_at = ?
                     WHERE id IN ({placeholders})
                    """,
                    (now + self._lease_ttl, now, *ids),
                )
            await self.db.commit()
        except Exception:
            await self.db.rollback()
            raise
        return [OutboxRow.from_db(r) for r in rows]

    async def reclaim_stale(self) -> int:
        """Вернуть в pending всё, что зависло in_flight (например, бот убили)."""
        now = time.time()
        cur = await self.db.execute(
            """
            UPDATE outbox
               SET status = 'pending', leased_until = NULL, updated_at = ?
             WHERE status = 'in_flight' AND (leased_until IS NULL OR leased_until < ?)
            """,
            (now, now),
        )
        await self.db.commit()
        return cur.rowcount

    # ---------- завершение ----------

    async def mark_done(self, row_id: int, page_id: str, page_url: str) -> None:
        await self.db.execute(
            """
            UPDATE outbox
               SET status = 'done', notion_page_id = ?, notion_page_url = ?,
                   last_error = NULL, leased_until = NULL, updated_at = ?
             WHERE id = ?
            """,
            (page_id, page_url, time.time(), row_id),
        )
        await self.db.commit()

    async def mark_retry(self, row_id: int, attempts: int, error: str,
                         *, retry_after: float | None = None) -> tuple[str, float]:
        """Экспоненциальный backoff: 2, 4, 8, 16 ... но не больше 10 минут.

        Если Notion прислал Retry-After — уважаем его.
        Возвращает (новый статус, задержка).
        """
        attempts += 1
        if attempts >= self._max_attempts:
            await self._mark_dead(row_id, attempts, error)
            return "dead", 0.0

        delay = retry_after if retry_after is not None else min(2 ** attempts, 600)
        now = time.time()
        await self.db.execute(
            """
            UPDATE outbox
               SET status = 'pending', attempts = ?, next_attempt_at = ?,
                   last_error = ?, leased_until = NULL, updated_at = ?
             WHERE id = ?
            """,
            (attempts, now + delay, error[:2000], now, row_id),
        )
        await self.db.commit()
        return "pending", delay

    async def mark_dead(self, row_id: int, attempts: int, error: str) -> None:
        await self._mark_dead(row_id, attempts + 1, error)

    async def _mark_dead(self, row_id: int, attempts: int, error: str) -> None:
        await self.db.execute(
            """
            UPDATE outbox
               SET status = 'dead', attempts = ?, last_error = ?,
                   leased_until = NULL, updated_at = ?
             WHERE id = ?
            """,
            (attempts, error[:2000], time.time(), row_id),
        )
        await self.db.commit()

    async def requeue_dead(self, user_id: int) -> int:
        """Команда /retry — вернуть в работу всё, что умерло у этого пользователя."""
        cur = await self.db.execute(
            """
            UPDATE outbox
               SET status = 'pending', attempts = 0, next_attempt_at = 0,
                   last_error = NULL, updated_at = ?
             WHERE status = 'dead' AND user_id = ?
            """,
            (time.time(), user_id),
        )
        await self.db.commit()
        return cur.rowcount

    # ---------- наблюдаемость ----------

    async def stats(self, user_id: int | None = None) -> dict[str, int]:
        sql = "SELECT status, COUNT(*) AS n FROM outbox"
        params: tuple = ()
        if user_id is not None:
            sql += " WHERE user_id = ?"
            params = (user_id,)
        sql += " GROUP BY status"
        cur = await self.db.execute(sql, params)
        rows = await cur.fetchall()
        result = {"pending": 0, "in_flight": 0, "done": 0, "dead": 0}
        for r in rows:
            result[r["status"]] = r["n"]
        return result