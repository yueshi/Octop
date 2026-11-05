"""Generic read-only HTTP source for light-app data sources.

No credentials: this connector is for public JSON/text endpoints (open
metadata APIs, status pages, public RSS-to-JSON bridges). Every hop is checked
against the SSRF guard and the response body is capped before it reaches a
source mapping.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
from octop_harness.security.ssrf import validate_agent_fetch_url

MAX_BYTES = 5 * 1024 * 1024
MAX_REDIRECTS = 5
DEFAULT_TIMEOUT = 20.0

TOOLS: list[dict[str, Any]] = [
    {
        "name": "fetch_json",
        "description": "GET a public JSON endpoint and return its body.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "http(s) URL to fetch."},
                "timeout": {"type": "number", "description": "Seconds, default 20."},
            },
            "required": ["url"],
        },
    },
    {
        "name": "fetch_text",
        "description": "GET a public text endpoint (HTML is converted to markdown).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "http(s) URL to fetch."},
                "timeout": {"type": "number", "description": "Seconds, default 20."},
            },
            "required": ["url"],
        },
    },
]


def list_tools() -> list[dict[str, Any]]:
    return TOOLS


def call_tool(creds: dict[str, Any], name: str, args: dict[str, Any]) -> str:
    del creds  # public endpoints do not use the connector credential store
    url = str(args.get("url") or "").strip()
    if not url:
        raise ValueError("url is required")
    timeout = float(args.get("timeout") or DEFAULT_TIMEOUT)
    response = _fetch(url, timeout=timeout)
    body = response.content
    if len(body) > MAX_BYTES:
        raise ValueError(f"response is larger than {MAX_BYTES // (1024 * 1024)} MB")
    if name == "fetch_json":
        text = response.text
        try:
            payload = json.loads(text)
        except ValueError as exc:
            raise ValueError(f"response is not valid JSON: {exc}") from None
        return json.dumps(payload, ensure_ascii=False)
    if name == "fetch_text":
        content_type = response.headers.get("content-type", "").lower()
        text = response.text
        if "html" in content_type:
            from markdownify import markdownify

            text = markdownify(text, heading_style="ATX").strip()
        return text[:200_000]
    raise ValueError(f"unknown tool: {name}")


def probe_credentials(creds: dict[str, Any]) -> None:
    del creds
    return None


def _fetch(url: str, *, timeout: float) -> httpx.Response:
    current = validate_agent_fetch_url(url)
    with httpx.Client(timeout=timeout, follow_redirects=False) as client:
        for _ in range(MAX_REDIRECTS + 1):
            validate_agent_fetch_url(current)
            response = client.get(current)
            if 300 <= response.status_code < 400:
                location = (response.headers.get("location") or "").strip()
                if not location:
                    return response
                current = str(response.url.join(location))
                continue
            response.raise_for_status()
            return response
    raise ValueError("too many redirects")
