"""Tests for post-generation: hashtags, location, full template."""

from __future__ import annotations

from src.db.models import Listing, Platform
from src.post.hashtags import (
    build_hashtags,
    district_tag,
    price_bucket,
    rooms_tag,
)
from src.post.location import yandex_maps_url
from src.post.template import extract_availability, render_post


# --- price_bucket ---


def test_price_bucket_boundaries():
    assert price_bucket(600) == "#до600"
    assert price_bucket(601) == "#до800"
    assert price_bucket(1500) == "#до1500"
    assert price_bucket(3001) == "#свыше3000"
    assert price_bucket(None) is None
    assert price_bucket(0) is None


# --- rooms_tag ---


def test_rooms_tag_studio_requires_small_area():
    assert rooms_tag(1.0, area_m2=25) == "#студия"
    assert rooms_tag(1.0, area_m2=40) == "#1k"
    assert rooms_tag(1.0, area_m2=None) == "#1k"


def test_rooms_tag_half_rooms():
    assert rooms_tag(1.5) == "#евродвушка"
    assert rooms_tag(2.5) == "#евротрешка"
    assert rooms_tag(3.5) is None  # no special tag


def test_rooms_tag_integer_rooms():
    assert rooms_tag(2.0) == "#2k"
    assert rooms_tag(3.0) == "#3k"
    assert rooms_tag(None) is None


# --- district_tag ---


def test_district_tag_serbian_to_latin():
    assert district_tag("Звездара") == "#Zvezdara"
    assert district_tag("Zvezdara") == "#Zvezdara"
    assert district_tag("Voždovac") == "#Vozdovac"
    assert district_tag("Novi Beograd") == "#NoviBeograd"


def test_district_tag_substring_lookup():
    # nekretnine often gives "Novi Beograd Blok 21 (10. Gimnazija)" — we should still extract.
    assert district_tag("Novi Beograd Blok 21 10 Gimnazija") == "#NoviBeograd"
    assert district_tag("Zemun Gornji Grad") == "#Zemun"


def test_district_tag_unknown_returns_none():
    assert district_tag("Some Random Place") is None
    assert district_tag(None) is None
    assert district_tag("") is None


# --- build_hashtags ---


def test_build_hashtags_combines_sources():
    tags = build_hashtags(
        price_eur=850, rooms=2.5, area_m2=65, district="Vračar"
    )
    assert "#Vracar" in tags
    assert "#до1000" in tags
    assert "#евротрешка" in tags


def test_build_hashtags_skips_missing():
    tags = build_hashtags(price_eur=None, rooms=None, area_m2=None, district=None)
    assert tags == []


# --- yandex_maps_url ---


def test_yandex_maps_url_with_coords():
    url = yandex_maps_url(lat=44.81, lon=20.43)
    assert url is not None
    assert "ll=20.43,44.81" in url
    assert "pt=20.43,44.81" in url


def test_yandex_maps_url_address_fallback():
    url = yandex_maps_url(lat=None, lon=None, address="Beograd, Cvijićeva 5")
    assert url is not None
    assert "text=" in url
    # space must be URL-encoded
    assert "Cvijićeva" not in url  # encoded
    assert "%" in url


def test_yandex_maps_url_none_when_no_data():
    assert yandex_maps_url(lat=None, lon=None, address=None) is None


# --- extract_availability ---


def test_extract_availability_russian():
    assert extract_availability("Свободна с 1 июня") == "1 июня"
    assert extract_availability("свободна с 01.06.2026, дополнительные детали") == "01.06.2026"


def test_extract_availability_english():
    assert extract_availability("Available from June 1") == "June 1"


def test_extract_availability_none():
    assert extract_availability(None) is None
    assert extract_availability("просто текст без даты") is None


# --- render_post ---


def _listing(**overrides) -> Listing:
    defaults = dict(
        platform=Platform.NEKRETNINE,
        external_id="x",
        url="https://example.com/x",
        price_eur=850,
        area_m2=65.0,
        rooms=2.5,
        district="Vračar",
        address_raw="Crveni krst",
        lat=44.79,
        lon=20.475,
        photo_urls=[],
        photo_phashes=[],
    )
    defaults.update(overrides)
    return Listing(**defaults)


def test_render_post_full():
    rendered = render_post(_listing())
    assert "Цена: 850 евро" in rendered.text
    assert "Площадь: 65 м²" in rendered.text
    # Address-first URL: text-search for street + city, with coords as map-center hint.
    assert "Локация: https://yandex.com/maps/?text=" in rendered.text
    assert "Crveni" in rendered.text  # address (URL-encoded but readable letters survive)
    assert "ll=20.475,44.79" in rendered.text
    assert "#Vracar" in rendered.text
    assert "#до1000" in rendered.text
    assert "#евротрешка" in rendered.text
    assert "@Darya_kralj" in rendered.text


def test_yandex_url_address_first_highlights_street():
    """If we have both address and coords, the URL must do a text-search (highlights street),
    with coords appended only as a centering hint — not a bare pin."""
    url = yandex_maps_url(lat=44.81, lon=20.43, address="Cara Dušana 5")
    assert url is not None
    assert "text=" in url
    assert "Cara" in url
    assert "ll=20.43,44.81" in url
    assert "pt=" not in url  # we deliberately don't pin — text-search highlights the street

    # Address already contains "Beograd" — should not be duplicated
    url2 = yandex_maps_url(lat=None, lon=None, address="Beograd, Cvijićeva 5")
    assert url2.count("Beograd") == 1


def test_yandex_url_coords_only_fallback_still_pins():
    """If listing has no address, fall back to bare pin (old behaviour)."""
    url = yandex_maps_url(lat=44.81, lon=20.43, address=None)
    assert url is not None
    assert "pt=20.43,44.81" in url
    assert "text=" not in url


def test_render_post_skips_location_when_no_geo():
    rendered = render_post(_listing(lat=None, lon=None, address_raw=None))
    assert "Локация:" not in rendered.text


def test_render_post_always_has_brand_hashtag():
    rendered = render_post(_listing(price_eur=None, rooms=None, district=None))
    # #квартирыOtz is the brand tag and is always present, even without other hashtags.
    assert "#квартирыOtz" in rendered.text
    assert "@Darya_kralj" in rendered.text


def test_render_post_full_includes_brand_hashtag():
    rendered = render_post(_listing())
    # Brand tag is last in the hashtag list so it doesn't disrupt district/price/type order.
    assert "#квартирыOtz" in rendered.text
    # And the regular tags still come before it.
    pos_vracar = rendered.text.find("#Vracar")
    pos_brand = rendered.text.find("#квартирыOtz")
    assert pos_vracar < pos_brand


def test_render_post_caption_price_beats_stale_listing_price():
    """Listing snapshot can be days out of date; trust the inbound caption (today's repost)."""
    from src.db.models import InboundPost, InboundStatus

    inbound = InboundPost(
        source_channel_id=1,
        tg_message_id=1,
        raw_text="#сдача #двушка 58м2 800 евро #до1000\nViva Residence",
        photo_paths=[],
        photo_phashes=[],
        status=InboundStatus.NEW,
    )
    rendered = render_post(_listing(price_eur=700, area_m2=58.0), inbound)
    assert "Цена: 800 евро" in rendered.text
    assert "Цена: 700 евро" not in rendered.text
    # Hashtag bucket follows the caption price too (800 → #до800, not the stale 700 → #до800 by coincidence).
    assert "#до800" in rendered.text


def test_render_post_falls_back_to_listing_when_caption_has_no_price():
    from src.db.models import InboundPost, InboundStatus

    inbound = InboundPost(
        source_channel_id=1,
        tg_message_id=1,
        raw_text="Просто описание без цены",
        photo_paths=[],
        photo_phashes=[],
        status=InboundStatus.NEW,
    )
    rendered = render_post(_listing(), inbound)
    assert "Цена: 850 евро" in rendered.text
