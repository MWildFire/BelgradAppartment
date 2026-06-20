"""E2E: lead form submit (HTMX) → success state, + honeypot present-but-hidden.

TG_BOT_TOKEN="" makes the notice a no-op, so the submit exercises validate → write → swap
without any network egress. The honeypot input `company` must exist in the DOM (bots fill it)
yet be invisible to a real user.
"""

from __future__ import annotations

from tests.e2e.conftest import requires_e2e


@requires_e2e
def test_lead_flow(page, site_server: str) -> None:
    # The shared lead form lives on the home page (id="lead").
    page.goto(f"{site_server}/", wait_until="networkidle")

    form = page.locator("form.lead#lead")
    assert form.count() == 1

    # Honeypot: present in the DOM but hidden from real users. The .hp wrapper hides via
    # off-screen positioning (left:-9999px, 1px box) — a legit technique that Playwright's
    # is_visible() does NOT flag (it ignores viewport position), so assert the box is parked
    # off-screen AND the wrapper is aria-hidden, which is what actually hides it from humans/AT.
    company = page.locator("input[name='company']")
    assert company.count() == 1, "honeypot 'company' input missing"
    box = company.first.bounding_box()
    assert box is not None and box["x"] < 0, f"honeypot not parked off-screen: {box}"
    hp_aria = page.locator(".hp").first.get_attribute("aria-hidden")
    assert hp_aria == "true", "honeypot wrapper must be aria-hidden from assistive tech"

    # Fill the two required fields and submit (HTMX posts, swaps #lead with the OK partial).
    page.fill("#lead-name", "E2E Тестов")
    page.fill("#lead-contact", "@e2e_tester")
    form.locator("button[type='submit']").click()

    # Success state: _lead_ok.html renders .lead-ok with the «заявка отправлена» kicker.
    page.wait_for_selector(".lead-ok", timeout=5000)
    ok_text = page.locator(".lead-ok").inner_text()
    assert "заявка отправлена" in ok_text.lower()
    # The original form is gone (swapped out), not merely hidden.
    assert page.locator("form.lead#lead").count() == 0


@requires_e2e
def test_lead_honeypot_silent(page, site_server: str) -> None:
    """A bot that fills `company` is dropped silently — still gets the OK swap, no error."""
    page.goto(f"{site_server}/", wait_until="networkidle")
    page.fill("#lead-name", "Spam Bot")
    page.fill("#lead-contact", "spam@bot")
    # Force-fill the hidden honeypot the way a naive bot would.
    page.locator("input[name='company']").fill("ACME Spam Co", force=True)
    page.locator("form.lead#lead button[type='submit']").click()
    page.wait_for_selector(".lead-ok", timeout=5000)
    assert page.locator(".lead-ok").count() == 1
