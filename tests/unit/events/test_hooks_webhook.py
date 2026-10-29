"""Webhook ingress ordering: authenticate before spending the rate limit."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from starlette.requests import Request

from octop.api.routers.hooks import receive_webhook
from octop.infra.errors import ErrorCode, OctopError


def _request(body: bytes = b'{"a": 1}') -> Request:
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/hooks/src-1",
        "headers": [(b"content-length", str(len(body)).encode())],
    }
    sent = False

    async def _receive() -> dict[str, Any]:
        nonlocal sent
        if sent:
            return {"type": "http.disconnect"}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(scope, _receive)


def _server(service: Any) -> Any:
    return SimpleNamespace(
        app_runtime=SimpleNamespace(external_event_service=service, external_event_worker=None)
    )


@pytest.mark.asyncio
async def test_rejected_signature_does_not_consume_the_rate_limit() -> None:
    """An unauthenticated caller must not spend a real source's budget.

    The source id is visible in the owner's webhook path, so throttling
    before the signature check let anyone exhaust it with bogus requests.
    Every random id also minted a limiter bucket, growing the map unbounded.
    """
    service = MagicMock()
    service.verify_webhook.side_effect = OctopError(ErrorCode.EXTERNAL_SIGNATURE_INVALID, "bad")

    with pytest.raises(OctopError):
        await receive_webhook("src-1", _request(), _server(service))  # type: ignore[arg-type]

    service.allow_webhook.assert_not_called()


@pytest.mark.asyncio
async def test_authenticated_request_consumes_the_rate_limit() -> None:
    service = MagicMock()
    service.verify_webhook.return_value = SimpleNamespace(source_id="src-1")
    service.allow_webhook.return_value = False

    with pytest.raises(OctopError):
        await receive_webhook("src-1", _request(), _server(service))  # type: ignore[arg-type]

    service.allow_webhook.assert_called_once_with("src-1")
