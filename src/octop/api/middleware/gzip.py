"""Path-selective gzip for the built dashboard.

Starlette's ``GZipMiddleware`` only excludes ``text/event-stream``; it would
also compress already-compressed images/video, byte-range responses and large
API downloads. The dashboard only needs compression for its text assets, so
gate the middleware by path and keep the API/binary/range paths untouched.
"""

from __future__ import annotations

from starlette.datastructures import Headers
from starlette.middleware.gzip import GZipMiddleware
from starlette.types import ASGIApp, Receive, Scope, Send

_TEXT_SUFFIXES = (
    ".js",
    ".mjs",
    ".css",
    ".html",
    ".json",
    ".svg",
    ".txt",
    ".xml",
    ".map",
    ".webmanifest",
)


def _compressible(path: str) -> bool:
    last = path.rsplit("/", 1)[-1].lower()
    if last.endswith(_TEXT_SUFFIXES):
        return True
    # SPA routes (/, /p/<token>, /knowledge-desk) return HTML without a suffix.
    return "." not in last


class SelectiveGZipMiddleware:
    """Compress text dashboard assets, leave API/binary/range responses alone."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        minimum_size: int = 1024,
        compresslevel: int = 6,
    ) -> None:
        self.app = app
        self._gzip = GZipMiddleware(
            app,
            minimum_size=minimum_size,
            compresslevel=compresslevel,
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = str(scope.get("path") or "")
        # API/SSE/download/range responses are deliberately left untouched.
        if (
            path.startswith(("/api/", "/ws/"))
            or Headers(scope=scope).get("range")
            or not _compressible(path)
        ):
            await self.app(scope, receive, send)
            return
        await self._gzip(scope, receive, send)


__all__ = ["SelectiveGZipMiddleware"]
