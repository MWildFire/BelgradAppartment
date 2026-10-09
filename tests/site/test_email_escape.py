"""F20: all visitor/source fields remain text in the HTML alternative."""
from html import escape

from src.site.email import _build_digest_message, _build_message
from src.site.leads import LeadPayload
from tests.site.test_subscription_email import _view


def test_lead_html_escapes_fields_and_plaintext_keeps_them():
    injected = '<a href="https://evil.invalid">click & "pay"</a>'
    payload = LeadPayload(name=injected, contact=injected, message=injected, districts=injected, rooms=injected)
    msg = _build_message(payload, {"district": injected, "url": "/apt/1"})
    html = msg.get_body(("html",)).get_content()
    plain = msg.get_body(("plain",)).get_content()
    assert injected in plain
    assert injected not in html
    assert escape(injected) in html


def test_digest_html_escapes_source_text_and_unsubscribe_url():
    injected = '<img src="x" onerror="alert(1)">'
    msg = _build_digest_message("a@example.invalid", [_view(1, 600, injected)], "https://x/?a=1&b=2")
    html = msg.get_body(("html",)).get_content()
    assert injected not in html
    assert escape(injected) in html
    assert "a=1&amp;b=2" in html
