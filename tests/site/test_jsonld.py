"""Stage S Wave 5 (S5-3): JSON-LD structured data — valid JSON, <script>-safe, privacy-safe.

Apartment+Offer on /apt + site-wide Organization+WebSite (base.html). Rendered via the real Jinja
env (shares the helper from test_routes). |tojson must keep every block valid JSON and escape any
</script> in dynamic values; D1 privacy means район + addressLocality «Београд» only, never a street.
"""

from __future__ import annotations

import json
import re

from tests.site.test_routes import _FakeItem, _real_env

_LD_RE = re.compile(r'<script type="application/ld\+json">(.*?)</script>', re.DOTALL)


def _ld_blocks(html: str) -> list:
    # json.loads raises if any block is invalid → the test fails loudly, which is what we want.
    return [json.loads(m.group(1)) for m in _LD_RE.finditer(html)]


def _render_apt(item) -> str:
    return _real_env().get_template("apt.html").render(item=item, similar=[], coords=None, maps=None)


def test_sitewide_organization_and_website_jsonld():
    blocks = _ld_blocks(_render_apt(_FakeItem()))
    graph = next(b for b in blocks if isinstance(b, dict) and b.get("@graph"))
    types = {n["@type"] for n in graph["@graph"]}
    assert {"Organization", "WebSite"} <= types
    org = next(n for n in graph["@graph"] if n["@type"] == "Organization")
    assert org["url"] == "https://rent.example.com/"
    assert org["sameAs"] == ["https://t.me/Darya_kralj"]


def test_apt_apartment_offer_jsonld_fields():
    apt = next(b for b in _ld_blocks(_render_apt(_FakeItem())) if b.get("@type") == "Apartment")
    assert apt["name"].endswith("Врачар")
    assert apt["url"] == "https://rent.example.com/apt/42"
    assert apt["image"] == "https://rent.example.com/apt/42/og.png"
    assert apt["floorSize"] == {"@type": "QuantitativeValue", "value": 45, "unitCode": "MTK"}
    assert apt["offers"]["price"] == 600
    assert apt["offers"]["priceCurrency"] == "EUR"
    assert apt["offers"]["availability"].endswith("InStock")


def test_apt_jsonld_privacy_no_street():
    apt = next(b for b in _ld_blocks(_render_apt(_FakeItem())) if b.get("@type") == "Apartment")
    assert apt["address"] == {"@type": "PostalAddress", "addressLocality": "Београд", "addressCountry": "RS"}
    assert "streetAddress" not in apt["address"]


def test_rented_apt_offer_is_soldout():
    rented = type("RentedItem", (_FakeItem,), {"is_rented": True})
    apt = next(b for b in _ld_blocks(_render_apt(rented)) if b.get("@type") == "Apartment")
    assert apt["offers"]["availability"].endswith("SoldOut")


def test_jsonld_escapes_script_injection_in_district():
    evil = type("EvilItem", (_FakeItem,), {"district": "Врачар</script><script>alert(1)"})
    html = _render_apt(evil)
    # the raw breakout payload must never appear unescaped …
    assert "</script><script>alert(1)" not in html
    # … and every JSON-LD block still parses (tojson escaped the </script> to <…).
    blocks = _ld_blocks(html)
    apt = next(b for b in blocks if b.get("@type") == "Apartment")
    assert "alert(1)" in apt["name"]  # the value is preserved, just safely encoded
