"""Разбор «сырого» текста сообщения в структурированную задачу.

Идея: пользователь пишет как ему удобно, но может добавить лёгкую разметку,
которая сразу заполнит свойства в Notion и сэкономит время на триаже.

Поддерживаемый синтаксис (всё опционально, порядок не важен):

    #тег              -> multi_select "Теги"
    !, !!, !!!        -> приоритет Низкий / Средний / Высокий
    @сегодня @завтра  -> дата
    @послезавтра
    @пн..@вс          -> ближайший такой день недели
    @25.12 @25.12.2026 @2026-12-25

Всё, что осталось после вырезания разметки:
    * первая непустая строка (обрезанная) -> заголовок страницы;
    * полный ОРИГИНАЛЬНЫЙ текст          -> тело страницы.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta

from app.domain.models import Priority

MAX_TITLE_LEN = 100

_TAG_RE = re.compile(r"(?<![\w#])#([^\s#,.;:!?()\[\]{}]{1,50})")
_BANG_RE = re.compile(r"(?<!\S)(!{1,3})(?!\S)")
_DATE_TOKEN_RE = re.compile(r"(?<!\S)@([^\s]{1,20})")

_WEEKDAYS = {
    "пн": 0, "вт": 1, "ср": 2, "чт": 3, "пт": 4, "сб": 5, "вс": 6,
    "mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6,
}

_RELATIVE = {
    "сегодня": 0, "today": 0,
    "завтра": 1, "tomorrow": 1, "tmr": 1,
    "послезавтра": 2,
}

_PRIORITY_BY_BANGS = {
    1: Priority.LOW,
    2: Priority.MEDIUM,
    3: Priority.HIGH,
}


@dataclass(slots=True)
class ParsedText:
    title: str
    body: str
    tags: list[str]
    priority: Priority
    due: date | None


def _parse_date_token(token: str, today: date) -> date | None:
    """Пробуем распознать один @-токен как дату. None = это не дата."""
    t = token.lower().strip(".,;:!?")

    if t in _RELATIVE:
        return today + timedelta(days=_RELATIVE[t])

    if t in _WEEKDAYS:
        delta = (_WEEKDAYS[t] - today.weekday()) % 7
        return today + timedelta(days=delta or 7)  # «@пн» в понедельник = следующий

    # 2026-12-25
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", t)
    if m:
        try:
            return date(int(m[1]), int(m[2]), int(m[3]))
        except ValueError:
            return None

    # 25.12 или 25.12.2026 / 25.12.26
    m = re.fullmatch(r"(\d{1,2})\.(\d{1,2})(?:\.(\d{2}|\d{4}))?", t)
    if m:
        day, month = int(m[1]), int(m[2])
        if m[3]:
            year = int(m[3])
            year += 2000 if year < 100 else 0
        else:
            year = today.year
        try:
            result = date(year, month, day)
        except ValueError:
            return None
        # Дата без года и уже прошла -> считаем, что это следующий год
        if not m[3] and result < today:
            result = result.replace(year=year + 1)
        return result

    return None


def _shorten(text: str, limit: int = MAX_TITLE_LEN) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    cut = text[: limit - 1]
    space = cut.rfind(" ")
    if space > limit * 0.5:
        cut = cut[:space]
    return cut.rstrip(" ,.;:-") + "…"


def parse(raw: str, *, today: date | None = None) -> ParsedText:
    """Главная функция модуля. Чистая, без побочных эффектов — легко тестировать."""
    today = today or date.today()
    original = raw.strip()

    tags: list[str] = []
    priority = Priority.NONE
    due: date | None = None

    # 1. Теги
    def _take_tag(m: re.Match[str]) -> str:
        tag = m.group(1)
        if tag not in tags:
            tags.append(tag)
        return " "

    stripped = _TAG_RE.sub(_take_tag, original)

    # 2. Приоритет — берём максимальный из встреченных
    def _take_bang(m: re.Match[str]) -> str:
        nonlocal priority
        candidate = _PRIORITY_BY_BANGS[len(m.group(1))]
        order = [Priority.NONE, Priority.LOW, Priority.MEDIUM, Priority.HIGH]
        if order.index(candidate) > order.index(priority):
            priority = candidate
        return " "

    stripped = _BANG_RE.sub(_take_bang, stripped)

    # 3. Дата — первый успешно распознанный токен побеждает
    def _take_date(m: re.Match[str]) -> str:
        nonlocal due
        parsed = _parse_date_token(m.group(1), today)
        if parsed is None:
            return m.group(0)  # это не дата (например @username) — не трогаем
        if due is None:
            due = parsed
        return " "

    stripped = _DATE_TOKEN_RE.sub(_take_date, stripped)

    # 4. Заголовок из очищенного текста
    lines = [ln.strip() for ln in stripped.splitlines()]
    first_meaningful = next((ln for ln in lines if ln), "")
    first_meaningful = re.sub(r"\s{2,}", " ", first_meaningful)
    title = _shorten(first_meaningful) or "Без названия"

    return ParsedText(
        title=title,
        body=original,
        tags=tags,
        priority=priority,
        due=due,
    )