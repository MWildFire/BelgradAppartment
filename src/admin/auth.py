"""HTTP Basic-auth dependency for the admin UI."""

from __future__ import annotations

import logging
import secrets

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from src.config import get_settings

logger = logging.getLogger(__name__)

_security = HTTPBasic()

_DEFAULT_PASSWORD = "change_me"
_default_password_warned = False


def require_admin(credentials: HTTPBasicCredentials = Depends(_security)) -> str:
    global _default_password_warned
    settings = get_settings()
    # Fail closed: a guessable default password must never grant access.
    if settings.admin_password == _DEFAULT_PASSWORD:
        if not _default_password_warned:
            logger.critical(
                "ADMIN_PASSWORD is still the default %r — rejecting ALL admin logins. "
                "Set a real ADMIN_PASSWORD in .env.",
                _DEFAULT_PASSWORD,
            )
            _default_password_warned = True
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Admin password not configured",
            headers={"WWW-Authenticate": 'Basic realm="otz-admin"'},
        )
    # compare_digest requires bytes (or ASCII-only str) — encode so non-ASCII
    # credentials get a clean 401 instead of a TypeError → HTTP 500.
    correct_user = secrets.compare_digest(
        credentials.username.encode("utf-8"), settings.admin_username.encode("utf-8")
    )
    correct_pass = secrets.compare_digest(
        credentials.password.encode("utf-8"), settings.admin_password.encode("utf-8")
    )
    if not (correct_user and correct_pass):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": 'Basic realm="otz-admin"'},
        )
    return credentials.username
