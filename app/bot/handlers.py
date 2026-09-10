"""Хендлеры Telegram — транспортный слой.

Правило слоя: здесь нет ни одного обращения к Notion. Хендлер обязан
отработать за миллисекунды: собрать доменный объект, положить в очередь,
ответить пользователю. Всё остальное — забота воркера.
"""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import Message

from app.domain.models import Attachment, InboxItem, Source
from app.domain.parser import parse
from app.storage.outbox import Outbox

log = logging.getLogger(__name__)

router = Router(name="inbox")

ACK_TEXT = "📥 Принято, отправляю в Notion…"

HELP_TEXT = """<b>Я — входящий инбокс для Notion.</b>

Пришли мне любое сообщение — оно станет задачей со статусом «Входящие».
Разбирать и сортировать удобнее уже в Notion.

<b>Необязательная разметка прямо в тексте:</b>
<code>#тег</code> — добавит тег
<code>!</code> / <code>!!</code> / <code>!!!</code> — приоритет низкий / средний / высокий
<code>@завтра</code>, <code>@пт</code>, <code>@25.12</code>, <code>@2026-12-25</code> — срок

<b>Пример:</b>
<code>!! Созвониться с банком по эквайрингу #работа @завтра</code>

<b>Команды:</b>
/stats — состояние очереди
/retry — повторить отправку упавших задач
/help — эта справка"""


def _author_of(message: Message) -> str:
    user = message.from_user
    if user is None:
        return "unknown"
    name = user.full_name
    if user.username:
        name += f" (@{user.username})"
    return f"{name} [id={user.id}]"


def _link_to(message: Message) -> str | None:
    """Deep-link на исходное сообщение.

    Работает для публичных чатов (по username) и супергрупп (формат /c/).
    У личной переписки с ботом постоянной ссылки нет — вернём None.
    """
    chat = message.chat
    if chat.username:
        return f"https://t.me/{chat.username}/{message.message_id}"
    if chat.type in {"supergroup", "channel"} and str(chat.id).startswith("-100"):
        internal = str(chat.id)[4:]
        return f"https://t.me/c/{internal}/{message.message_id}"
    return None


def _attachments_of(message: Message) -> list[Attachment]:
    """Достаём метаданные вложений. Сами файлы остаются в Telegram."""
    out: list[Attachment] = []

    if message.photo:
        best = message.photo[-1]  # последний элемент — максимальное разрешение
        out.append(
            Attachment(
                kind="photo",
                file_id=best.file_id,
                file_unique_id=best.file_unique_id,
                size=best.file_size,
            )
        )
    if message.document:
        d = message.document
        out.append(
            Attachment(
                kind="document",
                file_id=d.file_id,
                file_unique_id=d.file_unique_id,
                file_name=d.file_name,
                mime_type=d.mime_type,
                size=d.file_size,
            )
        )
    if message.voice:
        v = message.voice
        out.append(
            Attachment(
                kind="voice",
                file_id=v.file_id,
                file_unique_id=v.file_unique_id,
                mime_type=v.mime_type,
                size=v.file_size,
            )
        )
    if message.video:
        v = message.video
        out.append(
            Attachment(
                kind="video",
                file_id=v.file_id,
                file_unique_id=v.file_unique_id,
                file_name=v.file_name,
                mime_type=v.mime_type,
                size=v.file_size,
            )
        )
    if message.audio:
        a = message.audio
        out.append(
            Attachment(
                kind="audio",
                file_id=a.file_id,
                file_unique_id=a.file_unique_id,
                file_name=a.file_name,
                mime_type=a.mime_type,
                size=a.file_size,
            )
        )
    return out


def _raw_text_of(message: Message) -> str:
    """Текст сообщения или подпись к медиа. Для голосового — заглушка."""
    text = message.text or message.caption or ""
    if text.strip():
        return text
    if message.voice:
        return "Голосовое сообщение"
    if message.photo:
        return "Фото без подписи"
    if message.document:
        return message.document.file_name or "Документ"
    return "Сообщение без текста"


def build_item(message: Message) -> InboxItem:
    """Message (Telegram) -> InboxItem (домен). Чистая функция, легко тестируется."""
    parsed = parse(_raw_text_of(message))
    return InboxItem(
        title=parsed.title,
        body=parsed.body,
        tags=parsed.tags,
        priority=parsed.priority,
        due=parsed.due,
        source=Source.TELEGRAM,
        author=_author_of(message),
        tg_link=_link_to(message),
        attachments=_attachments_of(message),
    )


# --------------------------- команды ---------------------------


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    await message.answer(HELP_TEXT, parse_mode="HTML")


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(HELP_TEXT, parse_mode="HTML")


@router.message(Command("stats"))
async def cmd_stats(message: Message, outbox: Outbox) -> None:
    s = await outbox.stats(user_id=message.from_user.id if message.from_user else None)
    await message.answer(
        "📊 <b>Очередь</b>\n"
        f"Ждут отправки: {s['pending']}\n"
        f"В работе: {s['in_flight']}\n"
        f"Доставлено: {s['done']}\n"
        f"Не удалось: {s['dead']}",
        parse_mode="HTML",
    )


@router.message(Command("retry"))
async def cmd_retry(message: Message, outbox: Outbox) -> None:
    if message.from_user is None:
        return
    n = await outbox.requeue_dead(message.from_user.id)
    await message.answer(
        f"🔁 Вернул в очередь: {n}" if n else "Нечего повторять — упавших задач нет."
    )


# --------------------------- основной поток ---------------------------


@router.message(F.text | F.caption | F.photo | F.document | F.voice | F.video | F.audio)
async def capture(message: Message, outbox: Outbox) -> None:
    if message.from_user is None:
        return

    # Неизвестная команда — не задача. Известные перехвачены хендлерами выше.
    if message.text and message.text.startswith("/"):
        await message.reply("Не знаю такую команду. /help — что я умею.")
        return

    item = build_item(message)
    key = f"{message.chat.id}:{message.message_id}"

    row_id = await outbox.enqueue(
        idempotency_key=key,
        chat_id=message.chat.id,
        user_id=message.from_user.id,
        tg_message_id=message.message_id,
        item=item,
    )

    if row_id is None:
        # Тот же апдейт прилетел повторно — молчим, дубля в Notion не будет.
        log.info("Дубликат апдейта, пропускаю: %s", key)
        return

    ack = await message.reply(ACK_TEXT)
    await outbox.attach_ack(row_id, ack.message_id)


@router.message()
async def fallback(message: Message) -> None:
    """Стикеры, локации, опросы и прочее, что мы пока не умеем раскладывать."""
    await message.reply("Пока умею только текст, фото, документы, аудио и голосовые.")
