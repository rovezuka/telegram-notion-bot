"""Доменная модель: что такое «входящая задача» внутри нашей системы.

Модель намеренно ничего не знает ни про Telegram, ни про Notion.
Telegram-специфика (chat_id, message_id) хранится отдельно — в строке
очереди, а не в самой задаче.
"""

from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum


class Priority(StrEnum):
    NONE = "Без приоритета"
    LOW = "Низкий"
    MEDIUM = "Средний"
    HIGH = "Высокий"


class Source(StrEnum):
    TELEGRAM = "Telegram"


@dataclass(slots=True)
class Attachment:
    """Вложение из Telegram.

    Файл физически остаётся в Telegram: мы храним file_id (вечный указатель
    внутри Bot API) и, опционально, временную ссылку на скачивание.
    Загружать файлы в Notion — отдельная история (нужен внешний storage),
    поэтому на первом шаге кладём в тело страницы ссылку и метаданные.
    """

    kind: str  # photo | document | voice | video | audio | video_note
    file_id: str
    file_unique_id: str
    file_name: str | None = None
    mime_type: str | None = None
    size: int | None = None


@dataclass(slots=True)
class InboxItem:
    """Одна входящая задача — то, что превратится в страницу Notion."""

    title: str
    body: str
    tags: list[str] = field(default_factory=list)
    priority: Priority = Priority.NONE
    due: date | None = None
    source: Source = Source.TELEGRAM
    author: str = ""  # "Rostislav (@rostislav, id=123)"
    tg_link: str | None = None  # deep-link на исходное сообщение
    attachments: list[Attachment] = field(default_factory=list)

    # ---- сериализация для хранения в SQLite ----

    def to_json(self) -> str:
        data = dataclasses.asdict(self)
        data["priority"] = self.priority.value
        data["source"] = self.source.value
        data["due"] = self.due.isoformat() if self.due else None
        return json.dumps(data, ensure_ascii=False)

    @classmethod
    def from_json(cls, raw: str) -> InboxItem:
        data = json.loads(raw)
        return cls(
            title=data["title"],
            body=data["body"],
            tags=list(data.get("tags", [])),
            priority=Priority(data.get("priority", Priority.NONE.value)),
            due=date.fromisoformat(data["due"]) if data.get("due") else None,
            source=Source(data.get("source", Source.TELEGRAM.value)),
            author=data.get("author", ""),
            tg_link=data.get("tg_link"),
            attachments=[Attachment(**a) for a in data.get("attachments", [])],
        )
