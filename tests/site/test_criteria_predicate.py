"""C2 — the shared listing-matches-Filters predicate.

This is the crux of correctness: the catalog filter and the subscription matcher must apply ONE
implementation. The catalog's `_passes` is refactored to call `listing_matches_filters`, and the
subscription matcher calls the same function, so this test pins the shared contract directly.
"""

from __future__ import annotations

from src.site.readmodel import Filters, SiteListingView, listing_matches_filters


def _view(
    *,
    price_eur: int = 600,
    district: str = "Врачар",
    for_vnj: bool = False,
    for_white_card: bool = False,
    is_rented: bool = False,
) -> SiteListingView:
    return SiteListingView(
        id=1,
        price_eur=price_eur,
        area_m2=45.0,
        rooms="2-комн",
        floor=None,
        district=district,
        address=None,
        lat=None,
        lon=None,
        for_vnj=for_vnj,
        for_white_card=for_white_card,
        is_rented=is_rented,
        photos=[],
        note=None,
        channel_url="https://t.me/RentBelgrad",
        posted_at="2026-06-20T12:00:00+00:00",
    )


def test_empty_filters_match_everything() -> None:
    assert listing_matches_filters(_view(), 2.0, Filters()) is True


def test_district_filter() -> None:
    f = Filters(district=["Врачар"])
    assert listing_matches_filters(_view(district="Врачар"), 2.0, f) is True
    assert listing_matches_filters(_view(district="Земун"), 2.0, f) is False


def test_price_min_max() -> None:
    f = Filters(price_min=500, price_max=700)
    assert listing_matches_filters(_view(price_eur=600), 2.0, f) is True
    assert listing_matches_filters(_view(price_eur=400), 2.0, f) is False  # below min
    assert listing_matches_filters(_view(price_eur=900), 2.0, f) is False  # above max


def test_vnj_flag() -> None:
    f = Filters(vnj=True)
    assert listing_matches_filters(_view(for_vnj=True), 2.0, f) is True
    assert listing_matches_filters(_view(for_vnj=False), 2.0, f) is False


def test_white_card_flag() -> None:
    f = Filters(wc=True)
    assert listing_matches_filters(_view(for_white_card=True), 2.0, f) is True
    assert listing_matches_filters(_view(for_white_card=False), 2.0, f) is False


def test_rooms_filter_uses_raw_rooms() -> None:
    f = Filters(rooms=["2"])
    assert listing_matches_filters(_view(), 2.0, f) is True
    assert listing_matches_filters(_view(), 1.0, f) is False


def test_rooms_filter_studio_and_3plus() -> None:
    assert listing_matches_filters(_view(), 0.0, Filters(rooms=["studio"])) is True
    assert listing_matches_filters(_view(), None, Filters(rooms=["studio"])) is True
    assert listing_matches_filters(_view(), 4.0, Filters(rooms=["3+"])) is True
    assert listing_matches_filters(_view(), 2.0, Filters(rooms=["3+"])) is False


def test_rented_excluded_by_default_included_with_show_rented() -> None:
    # Subscriptions never want rented listings; the default (show_rented=False) excludes them.
    assert listing_matches_filters(_view(is_rented=True), 2.0, Filters()) is False
    assert (
        listing_matches_filters(_view(is_rented=True), 2.0, Filters(show_rented=True)) is True
    )
