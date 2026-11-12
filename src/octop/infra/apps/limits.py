"""Process-local traffic limits for MiniApp LLM calls and public links.

A self-hosted deployment should not let one page or one shared link consume the
whole owner's model budget or flood the public read endpoints. Limits are
intentionally per process: multi-worker deployments get one budget per worker.
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

from octop.infra.errors import ErrorCode, OctopError
from octop.infra.metrics import METRICS

MAX_CONCURRENT_LLM = 2
MAX_LLM_PER_MINUTE = 30
PUBLIC_CONCURRENCY = 20
PUBLIC_PER_MINUTE = 120
WINDOW_SECONDS = 60.0


@dataclass
class _Window:
    active: int = 0
    calls: deque[float] = field(default_factory=deque)

    def prune(self, now: float) -> None:
        cutoff = now - WINDOW_SECONDS
        while self.calls and self.calls[0] <= cutoff:
            self.calls.popleft()


_APP_WINDOWS: dict[str, _Window] = {}
_PUBLIC_WINDOWS: dict[str, _Window] = {}


def _enter(window: _Window, *, concurrency: int, per_minute: int, metric: str) -> None:
    now = time.monotonic()
    window.prune(now)
    if window.active >= concurrency or len(window.calls) >= per_minute:
        METRICS.inc(metric)
        raise OctopError(
            ErrorCode.APP_BUSY,
            "too many requests; try again shortly",
            details={"retry_after": 60},
        )
    window.active += 1
    window.calls.append(now)


def _leave(window: _Window) -> None:
    window.active = max(0, window.active - 1)


def acquire_app_llm(app_id: str) -> None:
    """Take one of the app's LLM slots; raises APP_BUSY when none is free."""
    window = _APP_WINDOWS.setdefault(app_id, _Window())
    _enter(
        window,
        concurrency=MAX_CONCURRENT_LLM,
        per_minute=MAX_LLM_PER_MINUTE,
        metric="light_app_llm_rejected_total",
    )


def release_app_llm(app_id: str) -> None:
    window = _APP_WINDOWS.get(app_id)
    if window is not None:
        _leave(window)


@asynccontextmanager
async def app_llm_slot(app_id: str) -> AsyncIterator[None]:
    """Hold one of the app's LLM slots; rejects immediately with APP_BUSY."""
    acquire_app_llm(app_id)
    try:
        yield
    finally:
        release_app_llm(app_id)


@asynccontextmanager
async def public_slot(key: str) -> AsyncIterator[None]:
    """Hold one public-link slot for one token + client address."""
    window = _PUBLIC_WINDOWS.setdefault(key, _Window())
    _enter(
        window,
        concurrency=PUBLIC_CONCURRENCY,
        per_minute=PUBLIC_PER_MINUTE,
        metric="light_app_public_rate_limited_total",
    )
    try:
        yield
    finally:
        _leave(window)


def reset_for_tests() -> None:
    _APP_WINDOWS.clear()
    _PUBLIC_WINDOWS.clear()


__all__ = [
    "MAX_CONCURRENT_LLM",
    "MAX_LLM_PER_MINUTE",
    "PUBLIC_CONCURRENCY",
    "PUBLIC_PER_MINUTE",
    "acquire_app_llm",
    "app_llm_slot",
    "public_slot",
    "release_app_llm",
    "reset_for_tests",
]
