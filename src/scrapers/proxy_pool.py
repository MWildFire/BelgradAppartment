"""Secret-safe, deterministic proxy candidate selection for Nekretnine traffic."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal
from urllib.parse import quote, unquote, urlparse, urlunparse

from src.config import Settings

ProxyKind = Literal["legacy_rotating", "static_isp", "metered"]

_SESSION_SUFFIX_RE = re.compile(r"_session-[A-Za-z0-9]+_lifetime-\d+[smhd]$")


class ProxyConfigurationError(ValueError):
    """A configured proxy cannot be safely used; callers must not fall back to direct traffic."""


class NoProxyAvailableError(ProxyConfigurationError):
    """A required pool has no remaining candidate after bounded failures."""


def _validate_proxy_url(raw: str, *, slot: str) -> str:
    value = raw.strip()
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https", "socks5", "socks5h"} or not parsed.hostname:
        raise ProxyConfigurationError(f"invalid proxy configuration for slot={slot}")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ProxyConfigurationError(f"invalid proxy configuration for slot={slot}") from exc
    if port is not None and not 1 <= port <= 65535:
        raise ProxyConfigurationError(f"invalid proxy configuration for slot={slot}")
    if parsed.path not in {"", "/"} or parsed.params or parsed.query or parsed.fragment:
        raise ProxyConfigurationError(f"invalid proxy configuration for slot={slot}")
    return value


@dataclass(frozen=True, slots=True)
class ProxyCandidate:
    """Internal candidate; the URL is deliberately excluded from repr and public descriptions."""

    slot: str
    kind: ProxyKind
    url: str = field(repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "url", _validate_proxy_url(self.url, slot=self.slot))

    @property
    def redacted_host(self) -> str:
        parsed = urlparse(self.url)
        host = parsed.hostname or "invalid"
        return f"{parsed.scheme}://{host}:{parsed.port}" if parsed.port else f"{parsed.scheme}://{host}"

    def describe(self) -> dict[str, str]:
        return {"slot": self.slot, "kind": self.kind, "host": self.redacted_host}

    def materialize(self, *, session_token: str | None = None) -> str:
        """Return the client URL, adding a suffix only for the legacy rotating gateway."""
        parsed = urlparse(self.url)
        if self.kind != "legacy_rotating":
            return self.url
        if parsed.password is None:
            return self.url
        password = _SESSION_SUFFIX_RE.sub("", unquote(parsed.password))
        suffix = session_token or "session"
        password = f"{password}_session-{suffix}_lifetime-30m"
        user = quote(unquote(parsed.username or ""), safe="")
        encoded_password = quote(password, safe="")
        host = parsed.hostname or ""
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        netloc = f"{user}:{encoded_password}@{host}"
        if parsed.port:
            netloc += f":{parsed.port}"
        return urlunparse((parsed.scheme, netloc, "", "", "", ""))


class ProxyPool:
    """Primary → secondary → additional ISP → metered order with fail-closed selection."""

    def __init__(self, candidates: tuple[ProxyCandidate, ...]) -> None:
        self._candidates = candidates

    @classmethod
    def from_settings(cls, settings: Settings) -> ProxyPool:
        mode = settings.nekretnine_proxy_mode
        candidates: list[ProxyCandidate] = []
        # Keep the pre-migration gateway as a real bounded fallback once a new primary is
        # configured.  Previously the `or` expression made SCRAPER_PROXY_URL disappear from the
        # pool as soon as PRIMARY_URL was set, so one challenged ISP IP could exhaust the whole
        # browser run even though the documented rollback/metered gateway was still available.
        primary_url = settings.nekretnine_proxy_primary_url
        configured = [
            ("primary", primary_url or settings.scraper_proxy_url,
             mode if primary_url else "legacy_rotating"),
            ("secondary", settings.nekretnine_proxy_secondary_url, mode),
        ]
        additional_urls = [
            value.strip()
            for value in re.split(r"[,\r\n]+", settings.nekretnine_proxy_additional_urls or "")
            if value.strip()
        ]
        configured.extend(
            (f"additional-{index}", url, mode)
            for index, url in enumerate(additional_urls, start=1)
        )
        configured.append(("metered", settings.nekretnine_proxy_metered_url, "metered"))
        if primary_url and settings.scraper_proxy_url and not settings.nekretnine_proxy_metered_url:
            configured[-1] = ("metered", settings.scraper_proxy_url, "legacy_rotating")
        seen_urls: set[str] = set()
        for slot, url, kind in configured:
            if url and url.strip():
                normalized_url = url.strip()
                if normalized_url in seen_urls:
                    continue
                candidates.append(ProxyCandidate(slot=slot, kind=kind, url=normalized_url))
                seen_urls.add(normalized_url)
        return cls(tuple(candidates))

    @property
    def candidates(self) -> tuple[ProxyCandidate, ...]:
        return self._candidates

    def select(self, *, failed_slots: set[str] | frozenset[str] = frozenset()) -> ProxyCandidate:
        for candidate in self._candidates:
            if candidate.slot not in failed_slots:
                return candidate
        raise NoProxyAvailableError("no proxy candidate remains after bounded failover")

    def describe(self) -> list[dict[str, str]]:
        return [candidate.describe() for candidate in self._candidates]
