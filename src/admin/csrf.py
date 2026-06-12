"""CSRF protection for admin POST routes.

Synchronizer-token pattern with one per-process random token: the admin is
localhost + basic-auth + single-user, and sessions need not survive restarts,
so a cookie/session store would add nothing. The token is only ever rendered
into authenticated pages, hence unknowable to a cross-site attacker.
"""

from __future__ import annotations

import secrets

from fastapi import HTTPException, Request, status

CSRF_TOKEN = secrets.token_urlsafe(32)

FORM_FIELD = "csrf_token"
HEADER = "x-csrf-token"

_FORM_CONTENT_TYPES = ("application/x-www-form-urlencoded", "multipart/form-data")


async def require_csrf(request: Request) -> None:
    """Fail-closed: any POST without a valid token (header or form field) → 403."""
    token = request.headers.get(HEADER)
    if not token and request.headers.get("content-type", "").startswith(_FORM_CONTENT_TYPES):
        # Starlette caches request.form(), so FastAPI's own Form() parsing still works.
        value = (await request.form()).get(FORM_FIELD)
        token = value if isinstance(value, str) else None
    if not token or not secrets.compare_digest(token, CSRF_TOKEN):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="CSRF token missing or invalid",
        )
