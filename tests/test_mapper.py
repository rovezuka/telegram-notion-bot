from datetime import date

from app.domain.models import Attachment, InboxItem, Priority
from app.notion.mapper import (
    MAX_CHILDREN,
    MAX_TEXT_CHUNK,
    PROP_DUE,
    PROP_PRIORITY,
    PROP_STATUS,
    PROP_TAGS,
    PROP_TITLE,
    build_children,
    build_page_payload,
)

DB_ID = "aaaaaaaabbbbccccddddeeeeeeeeeeee"


def test_payload_has_parent_properties_children():
    item = InboxItem(title="Заголовок", body="Тело", tags=["дом"],
                     priority=Priority.HIGH, due=date(2026, 12, 25))
    payload = build_page_payload(item, DB_ID)

    assert payload["parent"] == {"type": "database_id", "database_id": DB_ID}
    props = payload["properties"]
    assert props[PROP_TITLE]["title"][0]["text"]["content"] == "Заголовок"
    assert props[PROP_STATUS]["select"]["name"] == "Входящие"
    assert props[PROP_PRIORITY]["select"]["name"] == "Высокий"
    assert props[PROP_TAGS]["multi_select"] == [{"name": "дом"}]
    assert props[PROP_DUE]["date"]["start"] == "2026-12-25"


def test_optional_properties_are_omitted_when_empty():
    payload = build_page_payload(InboxItem(title="t", body="b"), DB_ID)
    props = payload["properties"]
    assert PROP_TAGS not in props
    assert PROP_DUE not in props


def test_long_body_is_split_into_chunks_under_limit():
    body = "строка текста\n" * 900
    blocks = build_children(InboxItem(title="t", body=body))
    assert len(blocks) > 1
    for block in blocks:
        for rt in block["paragraph"]["rich_text"]:
            assert len(rt["text"]["content"]) <= MAX_TEXT_CHUNK


def test_children_never_exceed_notion_limit():
    body = ("x" * MAX_TEXT_CHUNK + "\n") * 200
    blocks = build_children(InboxItem(title="t", body=body))
    assert len(blocks) <= MAX_CHILDREN


def test_attachments_are_rendered_as_list():
    item = InboxItem(
        title="t", body="b",
        attachments=[Attachment(kind="document", file_id="AgAD", file_unique_id="u",
                                file_name="акт.pdf", mime_type="application/pdf",
                                size=204800)],
    )
    blocks = build_children(item)
    types = [b["type"] for b in blocks]
    assert "heading_3" in types
    assert "bulleted_list_item" in types
    text = str(blocks)
    assert "акт.pdf" in text
    assert "200 КБ" in text


def test_telegram_link_becomes_link_block():
    item = InboxItem(title="t", body="b", tg_link="https://t.me/c/123/45")
    blocks = build_children(item)
    last = blocks[-1]["paragraph"]["rich_text"][0]
    assert last["text"]["link"]["url"] == "https://t.me/c/123/45"