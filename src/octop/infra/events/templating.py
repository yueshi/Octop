"""Safe literal placeholder rendering for notification templates.

Only the four documented fields are substituted; unknown braces stay as
literal text. No expressions or attribute access are evaluated.
"""

from __future__ import annotations

TEMPLATE_FIELDS = ("source", "type", "summary", "event_id")


def render_template(
    template: str,
    *,
    source: str,
    event_type: str,
    summary: str,
    event_id: str,
) -> str:
    text = template
    for key, value in (
        ("source", source),
        ("type", event_type),
        ("summary", summary),
        ("event_id", event_id),
    ):
        text = text.replace("{" + key + "}", value)
    return text.strip()
