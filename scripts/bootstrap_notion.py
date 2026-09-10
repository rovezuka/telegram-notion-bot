"""Одноразовый скрипт: создаёт базу «Инбокс» в Notion с нужной схемой.

Запуск:
    python -m scripts.bootstrap_notion

Что нужно заранее:
  1. Создать интеграцию: https://www.notion.so/my-integrations -> Internal
     -> скопировать Internal Integration Secret в NOTION_TOKEN.
  2. В Notion открыть страницу, внутри которой должна лежать база,
     нажать ••• -> Connections -> добавить свою интеграцию.
"""

from __future__ import annotations

import asyncio
import os
import sys

from app.notion.client import NotionClient, NotionError
from app.notion.mapper import (
    PROP_AUTHOR,
    PROP_DUE,
    PROP_LINK,
    PROP_PRIORITY,
    PROP_SOURCE,
    PROP_STATUS,
    PROP_TAGS,
    PROP_TITLE,
)

SCHEMA = {
    PROP_TITLE: {"title": {}},
    PROP_STATUS: {
        "select": {
            "options": [
                {"name": "Входящие", "color": "gray"},
                {"name": "В работе", "color": "blue"},
                {"name": "Ждёт", "color": "yellow"},
                {"name": "Готово", "color": "green"},
                {"name": "Отменено", "color": "red"},
            ]
        }
    },
    PROP_PRIORITY: {
        "select": {
            "options": [
                {"name": "Без приоритета", "color": "default"},
                {"name": "Низкий", "color": "gray"},
                {"name": "Средний", "color": "yellow"},
                {"name": "Высокий", "color": "red"},
            ]
        }
    },
    PROP_TAGS: {"multi_select": {}},
    PROP_SOURCE: {"select": {"options": [{"name": "Telegram", "color": "blue"}]}},
    PROP_DUE: {"date": {}},
    PROP_AUTHOR: {"rich_text": {}},
    PROP_LINK: {"url": {}},
}


def _page_title(page: dict) -> str:
    for prop in page.get("properties", {}).values():
        if prop.get("type") == "title":
            return "".join(t.get("plain_text", "") for t in prop["title"]) or "(без имени)"
    return "(без имени)"


async def main() -> int:
    token = os.environ.get("NOTION_TOKEN")
    if not token:
        print("Не задан NOTION_TOKEN", file=sys.stderr)
        return 1

    parent_page_id = os.environ.get("NOTION_PARENT_PAGE_ID")

    async with NotionClient(token) as notion:
        if not parent_page_id:
            print("NOTION_PARENT_PAGE_ID не задан — ищу доступные страницы…")
            res = await notion.search({
                "filter": {"property": "object", "value": "page"},
                "page_size": 20,
            })
            results = res.get("results", [])
            if not results:
                print("Интеграция не подключена ни к одной странице. "
                      "Откройте нужную страницу -> ••• -> Connections.", file=sys.stderr)
                return 1
            print("\nДоступные страницы:")
            for page in results:
                print(f"  {page['id']}  {_page_title(page)}")
            print("\nВыберите одну и запустите снова с NOTION_PARENT_PAGE_ID=<id>")
            return 0

        try:
            db = await notion.create_database({
                "parent": {"type": "page_id", "page_id": parent_page_id},
                "title": [{"type": "text", "text": {"content": "Инбокс"}}],
                "properties": SCHEMA,
            })
        except NotionError as e:
            print(f"Не удалось создать базу: {e}", file=sys.stderr)
            return 1

    print("База создана.")
    print(f"NOTION_DATABASE_ID={db['id']}")
    print(f"URL: {db.get('url', '')}")
    return 0


raise SystemExit(asyncio.run(main()))