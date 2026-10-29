"""Webhook HMAC helpers."""

from __future__ import annotations

import time

from octop.infra.events.rate_limit import SlidingWindowLimiter
from octop.infra.events.security import sign, verify_signature


def test_signature_round_trip() -> None:
    body = b'{"id":"e1"}'
    timestamp = str(int(time.time()))
    signature = sign("secret", timestamp, body)
    assert verify_signature("secret", timestamp=timestamp, signature=signature, body=body)
    assert verify_signature(
        "secret", timestamp=timestamp, signature=f"sha256={signature}", body=body
    )


def test_signature_rejects_tampering_and_stale_timestamps() -> None:
    body = b'{"id":"e1"}'
    timestamp = str(int(time.time()))
    signature = sign("secret", timestamp, body)
    assert not verify_signature(
        "secret", timestamp=timestamp, signature=signature, body=b'{"id":"e2"}'
    )
    assert not verify_signature("other", timestamp=timestamp, signature=signature, body=body)
    stale = str(int(time.time()) - 301)
    assert not verify_signature(
        "secret", timestamp=stale, signature=sign("secret", stale, body), body=body
    )
    assert not verify_signature("secret", timestamp="not-a-number", signature=signature, body=body)


def test_sliding_window_limiter() -> None:
    limiter = SlidingWindowLimiter(limit_per_minute=2)
    assert limiter.allow("src", now=0)
    assert limiter.allow("src", now=1)
    assert not limiter.allow("src", now=2)
    assert limiter.allow("src", now=61)
    assert limiter.allow("other", now=2)
