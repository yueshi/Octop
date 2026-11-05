"""Unit tests for the credential-less HTTP source adapter."""

from __future__ import annotations

import pytest
from octop_harness.security.ssrf import UnsafeAgentFetchUrlError

from octop.infra.connectors.gateway.adapters import http_extract


def test_lists_public_fetch_tools() -> None:
    names = [tool["name"] for tool in http_extract.list_tools()]
    assert names == ["fetch_json", "fetch_text"]
    assert all("url" in tool["inputSchema"]["required"] for tool in http_extract.list_tools())


def test_rejects_private_targets_before_network() -> None:
    with pytest.raises(UnsafeAgentFetchUrlError):
        http_extract.call_tool({}, "fetch_json", {"url": "http://127.0.0.1:8080/x"})
