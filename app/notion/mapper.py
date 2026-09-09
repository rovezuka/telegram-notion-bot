"""Перевод доменной модели в JSON, который понимает Notion API."""

from __future__ import annotations

from app.domain.models import InboxItem

# Названия свойств в базе Notion. Должны совпадать с реальными — Notion
# сопоставляет свойства по имени (или по id).
PROP_TITLE = "Задача"
PROP_STATUS = "Статус"
PROP_PRIORITY = "Приоритет"
PROP_TAGS = "Теги"
PROP_SOURCE = "Источник"
PROP_DUE = "Срок"
PROP_AUTHOR = "Автор"
PROP_LINK = "Сообщение"

STATUS_INBOX = "Входящие"

STATUS_PROPERTY_TYPE = "select"

# Ограничения Notion API
MAX_TEXT_CHUNK = 2000   # максимум символов в одном rich_text-объекте
MAX_CHILDREN = 100      # максимум блоков в одном запросе создания страницы
MAX_TITLE = 2000


def _chunks(text: str, size: int = MAX_TEXT_CHUNK) -> list[str]:
    """Режем длинный текст по границам строк, чтобы не рвать слова посередине."""
    if not text:
        return []
    result: list[str] = []
    current = ""
    for line in text.splitlines(keepends=True):
        while len(line) > size:
            if current:
                result.append(current)
                current = ""
            result.append(line[:size])
            line = line[size:]
        if len(current) + len(line) > size:
            result.append(current)
            current = line
        else:
            current += line
    if current:
        result.append(current)
    return result


def _paragraph(text: str) -> dict:
    return {
        "object": "block",
        "type": "paragraph",
        "paragraph": {
            "rich_text": [{"type": "text", "text": {"content": text}}]
        },
    }


def build_children(item: InboxItem) -> list[dict]:
    """Тело страницы: полный текст сообщения + блок с вложениями."""
    blocks: list[dict] = [_paragraph(chunk) for chunk in _chunks(item.body)]

    if item.attachments:
        blocks.append({
            "object": "block",
            "type": "heading_3",
            "heading_3": {
                "rich_text": [{"type": "text", "text": {"content": "Вложения"}}]
            },
        })
        for att in item.attachments:
            label = att.file_name or att.kind
            meta = f"{label} · {att.mime_type or att.kind}"
            if att.size:
                meta += f" · {att.size // 1024} КБ"
            blocks.append({
                "object": "block",
                "type": "bulleted_list_item",
                "bulleted_list_item": {
                    "rich_text": [
                        {"type": "text", "text": {"content": meta}},
                        {"type": "text",
                         "text": {"content": f"  (file_id: {att.file_id})"},
                         "annotations": {"code": True}},
                    ]
                },
            })

    if item.tg_link:
        blocks.append({
            "object": "block",
            "type": "paragraph",
            "paragraph": {
                "rich_text": [{
                    "type": "text",
                    "text": {"content": "Открыть в Telegram",
                             "link": {"url": item.tg_link}},
                }]
            },
        })

    # Если блоков больше лимита — оставляем первые и честно говорим об обрезке.
    if len(blocks) > MAX_CHILDREN:
        blocks = blocks[: MAX_CHILDREN - 1]
        blocks.append(_paragraph("… текст обрезан при импорте (лимит Notion API)"))
    return blocks


def build_properties(item: InboxItem) -> dict:
    props: dict = {
        PROP_TITLE: {
            "title": [{"type": "text",
                       "text": {"content": item.title[:MAX_TITLE]}}]
        },
        PROP_STATUS: {STATUS_PROPERTY_TYPE: {"name": STATUS_INBOX}},
        PROP_SOURCE: {"select": {"name": item.source.value}},
        PROP_PRIORITY: {"select": {"name": item.priority.value}},
    }

    if item.tags:
        props[PROP_TAGS] = {"multi_select": [{"name": t[:100]} for t in item.tags]}
    if item.due:
        props[PROP_DUE] = {"date": {"start": item.due.isoformat()}}
    if item.author:
        props[PROP_AUTHOR] = {
            "rich_text": [{"type": "text", "text": {"content": item.author[:MAX_TEXT_CHUNK]}}]
        }
    if item.tg_link:
        props[PROP_LINK] = {"url": item.tg_link}

    return props


def build_page_payload(item: InboxItem, database_id: str) -> dict:
    return {
        "parent": {"type": "database_id", "database_id": database_id},
        "properties": build_properties(item),
        "children": build_children(item),
    }