"""Tests for src.integrations.yonote — URL parsing, document fetch, attachment download.

httpx network is mocked via MockTransport so the tests don't hit the real YoNote API.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from src.integrations.yonote import (
    YoNoteError,
    download_attachment,
    fetch_document,
    parse_share_url,
)


# --------------------------------------------------------------------------- URL parser


def test_parse_share_url_extracts_team_and_share_id():
    parsed = parse_share_url(
        "https://serbia4rent.yonote.ru/share/1b3173a2-16b2-4b44-b367-51c558361175"
    )
    assert parsed == ("serbia4rent", "1b3173a2-16b2-4b44-b367-51c558361175")


def test_parse_share_url_lowercases_uuid_but_preserves_team():
    parsed = parse_share_url(
        "https://Acme.yonote.ru/share/1B3173A2-16B2-4B44-B367-51C558361175"
    )
    assert parsed == ("Acme", "1b3173a2-16b2-4b44-b367-51c558361175")


def test_parse_share_url_rejects_non_yonote_urls():
    assert parse_share_url("https://example.com/share/abc") is None
    assert parse_share_url("https://serbia4rent.yonote.ru/doc/abc") is None
    assert parse_share_url("") is None


# --------------------------------------------------------------------------- fetch_document


def _make_mock_client(handler) -> httpx.AsyncClient:
    """httpx.AsyncClient backed by MockTransport(handler)."""
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=httpx.Timeout(5.0))


@pytest.mark.asyncio
async def test_fetch_document_returns_text_and_image_attachments():
    captured: list[tuple[str, dict]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content.decode()) if req.content else {}
        captured.append((req.url.path, body))
        if req.url.path == "/api/documents.info":
            return httpx.Response(200, json={
                "data": {
                    "id": "doc-uuid",
                    "title": "Звездара (Джерам)👖 #O4047",
                    "text": "Просмотр организуется...\n34 м2, 7 этаж из 9",
                },
                "ok": True,
            })
        if req.url.path == "/api/attachments.list":
            return httpx.Response(200, json={
                "data": [
                    {"id": "a1", "name": "1.png", "contentType": "image/png", "size": 100},
                    {"id": "a2", "name": "logo.svg", "contentType": "image/svg+xml", "size": 1},
                    {"id": "a3", "name": "notes.pdf", "contentType": "application/pdf", "size": 9},
                ],
                "ok": True,
            })
        return httpx.Response(404, json={"ok": False, "error": "not_found"})

    async with _make_mock_client(handler) as client:
        doc = await fetch_document("share-uuid", "serbia4rent", client=client)

    assert doc.document_id == "doc-uuid"
    assert doc.title.startswith("Звездара")
    assert "34 м2" in doc.text
    # Only image/* attachments are kept; PDF is dropped.
    assert [a.id for a in doc.attachments] == ["a1", "a2"]
    assert captured[0][0] == "/api/documents.info"
    assert captured[0][1] == {"shareId": "share-uuid"}


@pytest.mark.asyncio
async def test_fetch_document_raises_on_missing_id():
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/api/documents.info":
            return httpx.Response(200, json={"data": {}, "ok": True})
        return httpx.Response(200, json={"data": [], "ok": True})

    async with _make_mock_client(handler) as client:
        with pytest.raises(YoNoteError):
            await fetch_document("share-uuid", "team", client=client)


@pytest.mark.asyncio
async def test_fetch_document_raises_on_http_error():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="server boom")

    async with _make_mock_client(handler) as client:
        with pytest.raises(YoNoteError):
            await fetch_document("share-uuid", "team", client=client)


# --------------------------------------------------------------------------- download_attachment


@pytest.mark.asyncio
async def test_download_attachment_writes_file_via_team_subdomain(tmp_path: Path):
    """The team subdomain (e.g. serbia4rent.yonote.ru) carries the share triple as auth; the
    api host (app.yonote.ru) would 401. Verify we hit the right host."""
    from src.integrations.yonote import YoNoteAttachment, YoNoteDocument

    doc = YoNoteDocument(
        share_id="share-uuid",
        team="serbia4rent",
        document_id="doc-uuid",
        title="t",
        text="t",
        attachments=[],
    )
    att = YoNoteAttachment(id="att-id", name="img.png", content_type="image/png", size=4)

    seen_hosts: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen_hosts.append(req.url.host)
        params = dict(req.url.params)
        assert params == {
            "id": "att-id",
            "shareId": "share-uuid",
            "documentId": "doc-uuid",
        }
        return httpx.Response(200, content=b"\x89PNG\r\n\x1a\n")

    async with _make_mock_client(handler) as client:
        dest = tmp_path / "out.png"
        result = await download_attachment(doc, att, dest, client=client)

    assert result == dest
    assert dest.read_bytes() == b"\x89PNG\r\n\x1a\n"
    assert seen_hosts == ["serbia4rent.yonote.ru"]


@pytest.mark.asyncio
async def test_download_attachment_raises_on_non_200(tmp_path: Path):
    from src.integrations.yonote import YoNoteAttachment, YoNoteDocument

    doc = YoNoteDocument(
        share_id="s", team="team", document_id="d",
        title="", text="", attachments=[],
    )
    att = YoNoteAttachment(id="x", name="f.png", content_type="image/png", size=0)

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"ok": False, "error": "auth"})

    async with _make_mock_client(handler) as client:
        with pytest.raises(YoNoteError):
            await download_attachment(doc, att, tmp_path / "out.png", client=client)
