"""Real client-IP extraction behind Caddy / Cloudflare — shared by site + admin.

Top-level (not under src/site or src/admin) so BOTH packages import it without coupling.

The problem (DATA_CONTRACT-S §IP): behind Caddy (and soon Cloudflare), `request.client.host`
is the *upstream proxy* IP (127.0.0.1 / a CF edge), NOT the real visitor. If the per-IP lead
rate-limit and the admin G2 lockout key on that, every client collapses into ONE bucket and
both controls silently stop discriminating.

Resolution order (config-gated; see `client_ip`):
  1. `settings.trust_cloudflare` AND `CF-Connecting-IP` present -> that header. Only trusted
     when the flag is on: until then it is freely forgeable. Once on, the origin is CF-IP-locked
     (S3-2) so CF is the only peer that can reach us and it sets this header authentically.
  2. else `X-Forwarded-For` present -> the RIGHTMOST entry. XFF is a client→proxy chain that each
     hop appends to; the rightmost element is what our trusted peer (Caddy) actually observed and
     appended. Leftmost entries are attacker-supplied and MUST NOT be trusted.
  3. else `request.client.host`, or "unknown" if the ASGI server gave us no peer.

Header lookups are case-insensitive (Starlette Headers already are). Empty / whitespace-only
header values fall through to the next rule rather than yielding a blank key.
"""

from __future__ import annotations

from starlette.requests import Request

from src.config import get_settings

_CF_CONNECTING_IP = "cf-connecting-ip"
_X_FORWARDED_FOR = "x-forwarded-for"
_UNKNOWN = "unknown"


def client_ip(request: Request) -> str:
    """Real client IP behind Caddy/Cloudflare, config-gated. Never the proxy/edge IP."""
    settings = get_settings()

    # 1. Cloudflare's real-client header — trusted only when the flag is on (else spoofable).
    if settings.trust_cloudflare:
        cf = request.headers.get(_CF_CONNECTING_IP)
        if cf is not None:
            cf = cf.strip()
            if cf:
                return cf

    # 2. X-Forwarded-For — the RIGHTMOST non-empty entry (the peer our trusted proxy saw).
    xff = request.headers.get(_X_FORWARDED_FOR)
    if xff is not None:
        for part in reversed(xff.split(",")):
            part = part.strip()
            if part:
                return part

    # 3. Direct peer.
    client = request.client
    if client and client.host:
        return client.host
    return _UNKNOWN
