"""In-process sliding-window rate limit for webhook ingress.

Single process only (Octop runs one event loop); a restart resets the buckets.
"""

from __future__ import annotations

import time
from collections import deque

WINDOW_SECONDS = 60
DEFAULT_LIMIT_PER_MINUTE = 60


class SlidingWindowLimiter:
    def __init__(self, *, limit_per_minute: int = DEFAULT_LIMIT_PER_MINUTE) -> None:
        self._limit = limit_per_minute
        self._hits: dict[str, deque[int]] = {}

    def allow(self, key: str, *, now: int | None = None, limit: int | None = None) -> bool:
        current = int(time.time()) if now is None else int(now)
        cap = self._limit if limit is None else limit
        hits = self._hits.setdefault(key, deque())
        cutoff = current - WINDOW_SECONDS
        while hits and hits[0] <= cutoff:
            hits.popleft()
        if len(hits) >= cap:
            return False
        hits.append(current)
        return True

    def reset(self, key: str | None = None) -> None:
        if key is None:
            self._hits.clear()
        else:
            self._hits.pop(key, None)
