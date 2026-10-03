"""Outgoing webhooks: what a URL may be, and how a receiver verifies a delivery.

Each delivery is signed like Stripe's: `X-Smarthome-Signature: t=<unix seconds>,v1=<hex>`
where v1 = HMAC-SHA256(secret, "<t>.<raw body>"). Receivers recompute it and reject old
timestamps, so a captured delivery cannot be replayed later.
"""

import hashlib
import hmac
from urllib.parse import urlsplit

from smarthome.modules.notifications.domain.errors import InvalidWebhook

MAX_URL = 500
SIGNATURE_HEADER = "X-Smarthome-Signature"


def validate_url(url: str, *, allow_http: bool) -> str:
    if len(url) > MAX_URL:
        raise InvalidWebhook(f"at most {MAX_URL} characters")
    parts = urlsplit(url)
    if parts.scheme != "https" and not (allow_http and parts.scheme == "http"):
        raise InvalidWebhook("the URL must use https")
    if not parts.hostname:
        raise InvalidWebhook("the URL needs a host")
    if parts.username or parts.password:
        raise InvalidWebhook("put credentials in the signature secret, not in the URL")
    if parts.fragment:
        raise InvalidWebhook("the URL must not have a fragment")
    return url


def signature(secret: str, timestamp: int, body: bytes) -> str:
    digest = hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256)
    return f"t={timestamp},v1={digest.hexdigest()}"


def verify(secret: str, header: str, body: bytes, *, now: int, tolerance_s: int = 300) -> bool:
    """What a receiver does (used in tests and documented for integrators)."""
    try:
        fields = dict(part.split("=", 1) for part in header.split(","))
        timestamp = int(fields["t"])
    except (KeyError, ValueError):
        return False
    if abs(now - timestamp) > tolerance_s:
        return False
    expected = signature(secret, timestamp, body).split("v1=", 1)[1]
    return hmac.compare_digest(expected, fields.get("v1", ""))
