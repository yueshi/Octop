"""Webhook signing helpers (HMAC-SHA256 over ``timestamp.body``)."""

from __future__ import annotations

import hashlib
import hmac
import time

SIGNATURE_HEADER = "X-Octop-Signature"
TIMESTAMP_HEADER = "X-Octop-Timestamp"
EVENT_ID_HEADER = "X-Octop-Event-Id"
EVENT_TYPE_HEADER = "X-Octop-Event-Type"
OCCURRED_AT_HEADER = "X-Octop-Occurred-At"

REPLAY_WINDOW_SECONDS = 300
MAX_PAYLOAD_BYTES = 256 * 1024


def sign(secret: str, timestamp: str, body: bytes) -> str:
    message = timestamp.encode("utf-8") + b"." + body
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


def verify_signature(
    secret: str,
    *,
    timestamp: str,
    signature: str,
    body: bytes,
    now: int | None = None,
    window: int = REPLAY_WINDOW_SECONDS,
) -> bool:
    if not timestamp or not signature:
        return False
    try:
        sent_at = int(timestamp)
    except (TypeError, ValueError):
        return False
    current = int(time.time()) if now is None else int(now)
    if abs(current - sent_at) > window:
        return False
    candidate = signature.strip()
    if candidate.lower().startswith("sha256="):
        candidate = candidate[7:]
    return hmac.compare_digest(sign(secret, timestamp, body), candidate)
