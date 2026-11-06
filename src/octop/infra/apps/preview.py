"""Validation for light-app preview images.

The agent draws the preview itself: SVG is the only image format a text model
can produce reliably without an image-generation provider. SVG is an active
document format, so every preview is checked before it is stored and the
server serves it with a sandboxing ``Content-Security-Policy`` header.
"""

from __future__ import annotations

import re

MAX_PREVIEW_BYTES = 256 * 1024
"""Largest preview an app may store (a hand-written SVG cover)."""

PREVIEW_FILENAME = "preview.svg"
"""File name of the preview inside an app (or template) directory."""

_FORBIDDEN: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"<\s*script", re.I), "SVG must not contain <script> elements"),
    (
        re.compile(r"<\s*foreignObject", re.I),
        "SVG must not contain <foreignObject> elements",
    ),
    (
        re.compile(r"<\s*(iframe|object|embed|audio|video)\b", re.I),
        "SVG must not contain embedded media elements",
    ),
    (
        re.compile(r"<\s*(animate|animateTransform|set|handler)\b", re.I),
        "SVG must not contain animation elements",
    ),
    (
        re.compile(r"\son[a-z]+\s*=", re.I),
        "SVG must not contain inline event handlers",
    ),
    (re.compile(r"javascript\s*:", re.I), "SVG must not contain javascript: URLs"),
    (
        re.compile(r"(?:xlink:)?href\s*=\s*[\"']?\s*(?:https?:|//)", re.I),
        "SVG must not reference remote resources",
    ),
    (
        re.compile(r"(?:src\s*=\s*[\"']?\s*(?:https?:|//))", re.I),
        "SVG must not reference remote resources",
    ),
    (re.compile(r"@import", re.I), "SVG must not import remote stylesheets"),
    (
        re.compile(r"url\(\s*[\"']?\s*(?:https?:|//)", re.I),
        "SVG must not reference remote resources",
    ),
)


def validate_preview_svg(svg: str) -> str:
    """Return *svg* when it is a self-contained, script-free preview.

    Raises ``ValueError`` with an actionable message otherwise — the agent
    reads the message and rewrites the preview.
    """
    text = (svg or "").strip()
    if not text:
        raise ValueError("preview SVG is empty")
    size = len(text.encode("utf-8"))
    if size > MAX_PREVIEW_BYTES:
        raise ValueError(
            f"preview SVG exceeds the {MAX_PREVIEW_BYTES // 1024} KB limit ({size} bytes)"
        )
    if "<svg" not in text or "</svg>" not in text:
        raise ValueError("preview must be a complete <svg>…</svg> document")
    for pattern, message in _FORBIDDEN:
        if pattern.search(text):
            raise ValueError(message)
    return text
