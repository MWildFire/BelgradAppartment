import asyncio

import httpx
import pytest

from src.db.models import Platform
from src.scrapers.base import ListingDetail
from src.scrapers.link_probe import SourceProbeSession


class FakeSource:
    def __init__(self, *, status=200, shell=False, hang=False):
        self.status, self.shell, self.hang = status, shell, hang
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        self.closed = True

    async def aclose(self):
        self.closed = True

    async def fetch_detail(self, url):
        if self.hang:
            await asyncio.sleep(3)
        response = httpx.Response(self.status, request=httpx.Request("GET", url))
        response.raise_for_status()
        if self.shell:
            return ListingDetail(platform=Platform.NEKRETNINE, external_id="1", url=url)
        return ListingDetail(
            platform=Platform.NEKRETNINE,
            external_id="1",
            url=url,
            title="Flat",
            price_eur=700,
            photo_urls=["https://example.test/photo.jpg"],
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,expected",
    [
        (200, "ACTIVE"),
        (404, "DEAD"),
        (410, "DEAD"),
        (402, "INCONCLUSIVE"),
        (403, "INCONCLUSIVE"),
        (503, "INCONCLUSIVE"),
    ],
)
async def test_source_probe_requires_valid_payload_and_handles_definitive_status(status, expected):
    source = FakeSource(status=status)
    async with SourceProbeSession(
        Platform.NEKRETNINE, factory=lambda source=source: source
    ) as session:
        result = await session.probe("https://example.test/1")
    assert result.status == expected
    assert source.closed


@pytest.mark.asyncio
async def test_successful_http_shell_and_timeout_are_unknown():
    for source in (FakeSource(shell=True), FakeSource(hang=True)):
        async with SourceProbeSession(
            Platform.NEKRETNINE, factory=lambda source=source: source, timeout_seconds=0.01
        ) as session:
            result = await session.probe("https://example.test/1")
        assert result.status == "INCONCLUSIVE" and result.errored
