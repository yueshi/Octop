"""Unit tests for the light-app preview (agent-drawn SVG cover)."""

from __future__ import annotations

import pytest

from octop.infra.apps.preview import MAX_PREVIEW_BYTES, validate_preview_svg

SVG = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1200 630"><rect width="10" height="10"/></svg>'


def test_valid_preview_is_accepted() -> None:
    assert validate_preview_svg(SVG) == SVG
    assert validate_preview_svg(f"  {SVG}  ") == SVG


def test_empty_or_incomplete_preview_is_rejected() -> None:
    with pytest.raises(ValueError, match="empty"):
        validate_preview_svg("   ")
    with pytest.raises(ValueError, match="complete"):
        validate_preview_svg("<svg><rect/>")


def test_oversized_preview_is_rejected() -> None:
    huge = SVG.replace("</svg>", "<rect/>" * (MAX_PREVIEW_BYTES // 6) + "</svg>")
    with pytest.raises(ValueError, match="limit"):
        validate_preview_svg(huge)


@pytest.mark.parametrize(
    "markup",
    [
        '<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>',
        '<svg xmlns="http://www.w3.org/2000/svg"><foreignObject><b>x</b></foreignObject></svg>',
        '<svg xmlns="http://www.w3.org/2000/svg" onload="alert(1)"></svg>',
        '<svg xmlns="http://www.w3.org/2000/svg"><a href="javascript:alert(1)">x</a></svg>',
        '<svg xmlns="http://www.w3.org/2000/svg"><image href="https://example.com/a.png"/></svg>',
        '<svg xmlns="http://www.w3.org/2000/svg"><style>@import url(https://x/a.css);</style></svg>',
        '<svg xmlns="http://www.w3.org/2000/svg"><animate attributeName="x"/></svg>',
        '<svg xmlns="http://www.w3.org/2000/svg"><iframe src="https://x"/></svg>',
    ],
)
def test_active_content_is_rejected(markup: str) -> None:
    with pytest.raises(ValueError):
        validate_preview_svg(markup)
