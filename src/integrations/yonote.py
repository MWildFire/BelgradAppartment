"""YoNote share-link client.

YoNote is an Outline-fork (https://yonote.ru). The serbia4rent agency publishes one apartment
per share-document, each containing free-form text (address, area, conditions) and photos.

API surface we use (all unauthenticated, the share itself is the token):
- POST  https://app.yonote.ru/api/documents.info   {shareId}              → text + documentId
- POST  https://app.yonote.ru/api/attachments.list {shareId}              → list of photos
- GET   https://<team>.yonote.ru/api/attachments.redirect?id=&shareId=&documentId=  → image bytes

The `<team>` subdomain is derived from the original share URL ("serbia4rent.yonote.ru/share/<id>")
because attachments.redirect on the api host (`app.yonote.ru`) requires user auth, while the
team subdomain accepts the share triple as the credential.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

API_HOST = "https://app.yonote.ru"
SHARE_URL_RE = re.compile(
    r"^https?://(?P<team>[a-z0-9-]+)\.yonote\.ru/share/(?P<share_id>[0-9a-f-]{36})",
    re.IGNORECASE,
)


class YoNoteError(RuntimeError):
    """Raised when the YoNote API returns a non-OK response."""


@dataclass(slots=True)
class YoNoteAttachment:
    id: str
    name: str
    content_type: str
    size: int


@dataclass(slots=True)
class YoNoteDocument:
    share_id: str
    team: str
    document_id: str
    title: str
    text: str
    attachments: list[YoNoteAttachment]


def parse_share_url(url: str) -> tuple[str, str] | None:
    """Extract (team_subdomain, share_id) from a YoNote share URL. Returns None if not YoNote."""
    m = SHARE_URL_RE.match(url.strip())
    if not m:
        return None
    return m.group("team"), m.group("share_id").lower()


async def fetch_document(
    share_id: str,
    team: str,
    *,
    client: httpx.AsyncClient | None = None,
) -> YoNoteDocument:
    """Fetch document metadata + attachments list. The team subdomain is needed later for downloads."""
    own = client is None
    cl = client or httpx.AsyncClient(timeout=httpx.Timeout(20.0), follow_redirects=True)
    try:
        info = await _post_json(cl, f"{API_HOST}/api/documents.info", {"shareId": share_id})
        data = info.get("data") or {}
        document_id = str(data.get("id") or "")
        if not document_id:
            raise YoNoteError(f"documents.info returned no id for share {share_id}")
        title = str(data.get("title") or "")
        text = str(data.get("text") or "")

        atts_resp = await _post_json(cl, f"{API_HOST}/api/attachments.list", {"shareId": share_id})
        attachments = [
            YoNoteAttachment(
                id=str(a["id"]),
                name=str(a.get("name") or ""),
                content_type=str(a.get("contentType") or ""),
                size=int(a.get("size") or 0),
            )
            for a in (atts_resp.get("data") or [])
            if str(a.get("contentType", "")).startswith("image/")
        ]
        return YoNoteDocument(
            share_id=share_id,
            team=team,
            document_id=document_id,
            title=title,
            text=text,
            attachments=attachments,
        )
    finally:
        if own:
            await cl.aclose()


async def download_attachment(
    doc: YoNoteDocument,
    attachment: YoNoteAttachment,
    dest: Path,
    *,
    client: httpx.AsyncClient | None = None,
) -> Path:
    """Stream one attachment to `dest`. Returns the path on success, raises on failure.

    Uses the team subdomain (e.g. `serbia4rent.yonote.ru`) because that route accepts the
    share triple as authentication; the api host (`app.yonote.ru`) would return 401.
    """
    own = client is None
    cl = client or httpx.AsyncClient(timeout=httpx.Timeout(45.0), follow_redirects=True)
    url = (
        f"https://{doc.team}.yonote.ru/api/attachments.redirect"
        f"?id={attachment.id}&shareId={doc.share_id}&documentId={doc.document_id}"
    )
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        async with cl.stream("GET", url) as resp:
            if resp.status_code != 200:
                body = (await resp.aread())[:200]
                raise YoNoteError(
                    f"download {attachment.id} → HTTP {resp.status_code}: {body!r}"
                )
            with dest.open("wb") as fh:
                async for chunk in resp.aiter_bytes(chunk_size=64 * 1024):
                    fh.write(chunk)
        return dest
    finally:
        if own:
            await cl.aclose()


async def _post_json(client: httpx.AsyncClient, url: str, payload: dict) -> dict:
    resp = await client.post(url, json=payload)
    if resp.status_code != 200:
        raise YoNoteError(f"{url} → HTTP {resp.status_code}: {resp.text[:300]!r}")
    body = resp.json()
    if not body.get("ok", True):
        raise YoNoteError(f"{url} → {body.get('error')}: {body.get('message')}")
    return body
