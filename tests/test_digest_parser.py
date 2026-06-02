"""Tests for src.telegram.digest_parser — covers the full serbia4rent dump (post 4793).

The test reconstructs the entity dump we captured from a real Telegram message; if the parser
ever loses track of UTF-16 offsets, section bucketing, paren-aware splits, or rented-flag
filtering, one of these will fire.
"""

from __future__ import annotations

from dataclasses import dataclass

from telethon.tl.types import (
    MessageEntityBold,
    MessageEntityHashtag,
    MessageEntityTextUrl,
)

from src.telegram.digest_parser import is_digest_post, parse_digest


@dataclass
class FakeMsg:
    message: str
    entities: list


# --------------------------------------------------------------------------- fixture

POST_4793_TEXT = (
    "Доступны квартиры для просмотра:\n\n"
    "Однокомнатные и студии:\n\n"
    "Звездара (Джерам)👖 #O4047 - 450€\n\n"
    "Полуторакомнатные:\n\n"
    "Старый город (Ботанический сад)👔 #E4045 - 600€\n\n"
    "Звездара (Джерам). Паркинг. Рабочее место🧥 #E4044G - 700€\n\n"
    "Двухкомнатные:\n\n"
    "Новый Белград (Блок 4). Pet-friendly🥼 #D4043P - 500€\n\n"
    "Трёхкомнатные:\n\n"
    "Врачар (Храм св. Савы, Автокоманда)🩳 #T4048 - 900€\n\n"
    "Врачар (Црвени крст). Паркинг🧟‍♂️ #T3925G - 990€\n\n"
    "Четырёхкомнатные: \n\n"
    "Врачар (Лондон, Славия). Паркинг. Pet-friendly. 2 Рабочих места. Полное оснащение👕 #F4046PG - 1000€"
)

POST_4793_ENTITIES = [
    MessageEntityBold(offset=0, length=32),
    MessageEntityBold(offset=34, length=22),
    MessageEntityTextUrl(
        offset=59, length=17,
        url="https://serbia4rent.yonote.ru/share/1b3173a2-16b2-4b44-b367-51c558361175",
    ),
    MessageEntityHashtag(offset=79, length=6),
    MessageEntityBold(offset=94, length=17),
    MessageEntityTextUrl(
        offset=114, length=31,
        url="https://serbia4rent.yonote.ru/share/9a8f5608-f953-4d67-be5c-b972f1102f8b",
    ),
    MessageEntityHashtag(offset=148, length=6),
    MessageEntityTextUrl(
        offset=163, length=41,
        url="https://serbia4rent.yonote.ru/share/c0f2aadc-8396-4b30-8154-7f1cb36e12b1",
    ),
    MessageEntityHashtag(offset=207, length=7),
    MessageEntityBold(offset=223, length=13),
    MessageEntityTextUrl(
        offset=239, length=36,
        url="https://serbia4rent.yonote.ru/share/cea6e1a0-cbbc-43c2-916c-e38676994213",
    ),
    MessageEntityHashtag(offset=278, length=7),
    MessageEntityBold(offset=294, length=13),
    MessageEntityTextUrl(
        offset=310, length=35,
        url="https://serbia4rent.yonote.ru/share/a792c69e-38fb-4910-a4f9-e106846613fb",
    ),
    MessageEntityHashtag(offset=348, length=6),
    MessageEntityTextUrl(
        offset=363, length=29,
        url="https://serbia4rent.yonote.ru/share/13cb6bf9-2cd6-49e4-8c98-cee0f6356340",
    ),
    MessageEntityHashtag(offset=398, length=7),
    MessageEntityBold(offset=414, length=16),
    MessageEntityTextUrl(
        offset=434, length=81,
        url="https://serbia4rent.yonote.ru/share/f9f6e5bd-8ff0-471b-b23e-d82d7f8924b9",
    ),
    MessageEntityHashtag(offset=518, length=8),
]


# --------------------------------------------------------------------------- tests


def test_is_digest_post_recognises_post_4793():
    msg = FakeMsg(POST_4793_TEXT, POST_4793_ENTITIES)
    assert is_digest_post(msg) is True


def test_is_digest_post_rejects_regular_repost():
    """A plain Telegram repost with no entities pointing at *.yonote.ru/share/ is not a digest."""
    msg = FakeMsg(
        "Сдается квартира на Враче, 600 евро. Звоните!",
        [MessageEntityHashtag(offset=0, length=5)],
    )
    assert is_digest_post(msg) is False


def test_is_digest_post_requires_at_least_two_yonote_links():
    """A single YoNote link in an otherwise normal post is not enough — most likely a forward
    of a single apartment, not a list."""
    msg = FakeMsg(
        "Хорошая квартира — Звездара",
        [MessageEntityTextUrl(offset=0, length=18, url="https://serbia4rent.yonote.ru/share/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")],
    )
    assert is_digest_post(msg) is False


def test_parse_digest_extracts_seven_apartments_with_correct_buckets():
    lines = parse_digest(FakeMsg(POST_4793_TEXT, POST_4793_ENTITIES))
    assert [ln.room_type for ln in lines] == ["1k", "1.5k", "1.5k", "2k", "3k", "3k", "4k"]


def test_parse_digest_parses_district_and_price():
    lines = parse_digest(FakeMsg(POST_4793_TEXT, POST_4793_ENTITIES))
    summary = [(ln.district_ru, ln.subdistrict_ru, ln.price_eur) for ln in lines]
    assert summary == [
        ("Звездара", "Джерам", 450),
        ("Старый город", "Ботанический сад", 600),
        ("Звездара", "Джерам", 700),
        ("Новый Белград", "Блок 4", 500),
        ("Врачар", "Храм св. Савы, Автокоманда", 900),
        ("Врачар", "Црвени крст", 990),
        ("Врачар", "Лондон, Славия", 1000),
    ]


def test_parse_digest_preserves_features_outside_parens():
    """Features after the subdistrict are split on '.' but must stay together inside parens
    (the "Храм св. Савы" case below would otherwise leak into the features list)."""
    lines = parse_digest(FakeMsg(POST_4793_TEXT, POST_4793_ENTITIES))
    by_id = {ln.internal_id: ln.features for ln in lines}
    assert by_id["E4044G"] == ["Паркинг", "Рабочее место"]
    assert by_id["D4043P"] == ["Pet-friendly"]
    assert by_id["T4048"] == []  # nothing after the parens
    assert by_id["F4046PG"] == ["Паркинг", "Pet-friendly", "2 Рабочих места", "Полное оснащение"]


def test_parse_digest_returns_share_id_and_team():
    lines = parse_digest(FakeMsg(POST_4793_TEXT, POST_4793_ENTITIES))
    assert lines[0].share_id == "1b3173a2-16b2-4b44-b367-51c558361175"
    assert lines[0].yonote_team == "serbia4rent"


def _utf16_offset(text: str, marker: str) -> int:
    """UTF-16-code-unit index of `marker` inside `text` — matches what Telethon gives us."""
    idx = text.index(marker)
    return len(text[:idx].encode("utf-16-le")) // 2


def _utf16_len(s: str) -> int:
    return len(s.encode("utf-16-le")) // 2


def test_parse_digest_skips_rented_lines():
    """Lines with "(СДАНА)" — the apartment is already taken — should be dropped: scraping
    them would just produce dead-end matches and clutter the bot."""
    text = (
        "Доступны квартиры для просмотра:\n\n"
        "Однокомнатные и студии:\n\n"
        "Вождовац (Лекино брдо). Pet-friendly🛒 #S4038P (СДАНА)\n\n"
        "Звездара (Джерам)👖 #O4047 - 450€"
    )
    rented_anchor = "Вождовац (Лекино брдо). Pet-friendly"
    available_anchor = "Звездара (Джерам)"
    entities = [
        MessageEntityBold(offset=0, length=_utf16_len("Доступны квартиры для просмотра:")),
        MessageEntityBold(
            offset=_utf16_offset(text, "Однокомнатные и студии:"),
            length=_utf16_len("Однокомнатные и студии:"),
        ),
        MessageEntityTextUrl(
            offset=_utf16_offset(text, rented_anchor),
            length=_utf16_len(rented_anchor),
            url="https://serbia4rent.yonote.ru/share/aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        ),
        MessageEntityTextUrl(
            offset=_utf16_offset(text, available_anchor),
            length=_utf16_len(available_anchor),
            url="https://serbia4rent.yonote.ru/share/bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
        ),
    ]
    lines = parse_digest(FakeMsg(text, entities))
    assert [ln.internal_id for ln in lines] == ["O4047"]


def test_parse_digest_ignores_non_yonote_links():
    """A digest may contain unrelated MessageEntityTextUrl links (e.g. agency contacts);
    those should not appear in the result list."""
    text = "Доступны квартиры:\n\nЗвездара (Джерам)👖 #O4047 - 450€\nКонтакт @Manager_Serbia4Rent"
    anchor = "Звездара (Джерам)"
    contact = "@Manager_Serbia4Rent"
    entities = [
        MessageEntityBold(offset=0, length=_utf16_len("Доступны квартиры:")),
        MessageEntityTextUrl(
            offset=_utf16_offset(text, anchor),
            length=_utf16_len(anchor),
            url="https://serbia4rent.yonote.ru/share/1b3173a2-16b2-4b44-b367-51c558361175",
        ),
        MessageEntityTextUrl(
            offset=_utf16_offset(text, contact),
            length=_utf16_len(contact),
            url="https://t.me/Manager_Serbia4Rent",
        ),
    ]
    # Need a second yonote link to make it a digest; mimic by adding another line.
    text = (
        "Доступны квартиры:\n\n"
        "Звездара (Джерам)👖 #O4047 - 450€\n\n"
        "Врачар (Лондон)👕 #T4048 - 900€\n\n"
        "Контакт @Manager_Serbia4Rent"
    )
    entities = [
        MessageEntityBold(offset=0, length=_utf16_len("Доступны квартиры:")),
        MessageEntityTextUrl(
            offset=_utf16_offset(text, "Звездара (Джерам)"),
            length=_utf16_len("Звездара (Джерам)"),
            url="https://serbia4rent.yonote.ru/share/1b3173a2-16b2-4b44-b367-51c558361175",
        ),
        MessageEntityTextUrl(
            offset=_utf16_offset(text, "Врачар (Лондон)"),
            length=_utf16_len("Врачар (Лондон)"),
            url="https://serbia4rent.yonote.ru/share/2b3173a2-16b2-4b44-b367-51c558361175",
        ),
        MessageEntityTextUrl(
            offset=_utf16_offset(text, "@Manager_Serbia4Rent"),
            length=_utf16_len("@Manager_Serbia4Rent"),
            url="https://t.me/Manager_Serbia4Rent",
        ),
    ]
    lines = parse_digest(FakeMsg(text, entities))
    assert len(lines) == 2
    assert all(ln.yonote_url.startswith("https://serbia4rent.yonote.ru/share/") for ln in lines)
