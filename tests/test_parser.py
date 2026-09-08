from datetime import date

import pytest

from app.domain.models import Priority
from app.domain.parser import parse

MONDAY = date(2026, 9, 7)  # понедельник


def test_plain_text_becomes_title_and_body():
    r = parse("Позвонить в банк")
    assert r.title == "Позвонить в банк"
    assert r.body == "Позвонить в банк"
    assert r.tags == []
    assert r.priority is Priority.NONE
    assert r.due is None


def test_tags_are_extracted_and_removed_from_title():
    r = parse("Купить кофе #дом #покупки")
    assert r.tags == ["дом", "покупки"]
    assert r.title == "Купить кофе"
    # оригинал сохраняется целиком
    assert "#дом" in r.body


def test_duplicate_tags_collapse():
    r = parse("#work Отчёт #work")
    assert r.tags == ["work"]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("! мелочь", Priority.LOW),
        ("!! нормально", Priority.MEDIUM),
        ("!!! горит", Priority.HIGH),
        ("без приоритета", Priority.NONE),
    ],
)
def test_priority_from_bangs(text, expected):
    assert parse(text).priority is expected


def test_max_priority_wins():
    assert parse("! начало !!! конец").priority is Priority.HIGH


def test_exclamation_inside_word_is_not_priority():
    r = parse("Ура! Сдали релиз")
    assert r.priority is Priority.NONE
    assert r.title == "Ура! Сдали релиз"


def test_relative_dates():
    assert parse("отчёт @сегодня", today=MONDAY).due == MONDAY
    assert parse("отчёт @завтра", today=MONDAY).due == date(2026, 9, 8)
    assert parse("отчёт @послезавтра", today=MONDAY).due == date(2026, 9, 9)


def test_weekday_picks_next_occurrence():
    assert parse("созвон @пт", today=MONDAY).due == date(2026, 9, 11)
    # тот же день недели, что и сегодня -> следующая неделя
    assert parse("созвон @пн", today=MONDAY).due == date(2026, 9, 14)


def test_explicit_dates():
    assert parse("оплата @25.12", today=MONDAY).due == date(2026, 12, 25)
    assert parse("оплата @25.12.2027", today=MONDAY).due == date(2027, 12, 25)
    assert parse("оплата @2027-01-09", today=MONDAY).due == date(2027, 1, 9)


def test_date_without_year_rolls_over_to_next_year():
    assert parse("оплата @01.03", today=MONDAY).due == date(2027, 3, 1)


def test_username_is_not_a_date():
    r = parse("написать @rostislav про тестовое")
    assert r.due is None
    assert "@rostislav" in r.title


def test_invalid_date_is_ignored():
    assert parse("@32.13 что-то", today=MONDAY).due is None


def test_title_is_first_line_only():
    r = parse("Заголовок задачи\nподробности\nещё подробности")
    assert r.title == "Заголовок задачи"
    assert r.body.count("\n") == 2


def test_long_title_is_shortened_on_word_boundary():
    text = "слово " * 40
    r = parse(text)
    assert len(r.title) <= 101
    assert r.title.endswith("…")
    assert r.body == text.strip()


def test_everything_at_once():
    r = parse("!! Созвониться с банком #работа #финансы @завтра", today=MONDAY)
    assert r.title == "Созвониться с банком"
    assert r.tags == ["работа", "финансы"]
    assert r.priority is Priority.MEDIUM
    assert r.due == date(2026, 9, 8)


def test_empty_text_gets_placeholder_title():
    assert parse("   ").title == "Без названия"