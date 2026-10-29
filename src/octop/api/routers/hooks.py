"""Public webhook ingress for external event sources.

The source id and HMAC signature are the credentials; this router is mounted
under the JWT-exempt ``/api/hooks/`` prefix. Bodies are capped before parsing
and events land in the DB inbox; agent work happens asynchronously.
"""

from __future__ import annotations

import json
from typing import cast

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from octop.api.deps import get_server
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.events.security import (
    EVENT_ID_HEADER,
    EVENT_TYPE_HEADER,
    MAX_PAYLOAD_BYTES,
    OCCURRED_AT_HEADER,
    SIGNATURE_HEADER,
    TIMESTAMP_HEADER,
)
from octop.infra.events.service import ExternalEventService
from octop.infra.server import OctopServer

router = APIRouter()


class WebhookAccepted(BaseModel):
    event_id: str = Field(description="Internal event id assigned to this delivery.")
    accepted: bool = Field(description="Always true once the event is durably stored.")
    duplicate: bool = Field(description="True when the dedupe key was already seen.")
    deliveries: int = Field(description="Number of subscriptions fanned out.")


def _service(server: OctopServer) -> ExternalEventService:
    if server.app_runtime is None or server.app_runtime.external_event_service is None:
        raise OctopError(ErrorCode.INTERNAL_ERROR, "external event service is not started")
    return cast(ExternalEventService, server.app_runtime.external_event_service)


async def _read_body_capped(request: Request) -> bytes:
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > MAX_PAYLOAD_BYTES:
            raise OctopError(
                ErrorCode.EXTERNAL_PAYLOAD_TOO_LARGE,
                f"payload exceeds {MAX_PAYLOAD_BYTES} bytes",
            )
        chunks.append(chunk)
    return b"".join(chunks)


def _event_fields(request: Request, payload: dict[str, object]) -> tuple[str, str, int | None]:
    event_type = (
        request.headers.get(EVENT_TYPE_HEADER)
        or str(payload.get("event_type") or "")
        or str(payload.get("type") or "")
        or "external.event"
    ).strip()
    external_id = (
        request.headers.get(EVENT_ID_HEADER)
        or str(payload.get("external_id") or "")
        or str(payload.get("id") or "")
    ).strip()
    raw_occurred = request.headers.get(OCCURRED_AT_HEADER) or payload.get("occurred_at")
    occurred_at: int | None = None
    if raw_occurred not in (None, ""):
        try:
            occurred_at = int(str(raw_occurred))
        except (TypeError, ValueError) as exc:
            raise OctopError(ErrorCode.EXTERNAL_EVENT_INVALID, "invalid occurred_at") from exc
    return event_type, external_id, occurred_at


@router.post(
    "/{source_id}",
    response_model=WebhookAccepted,
    status_code=202,
    summary="Receive a webhook event",
    description=(
        "Public endpoint authenticated by HMAC-SHA256 over `{timestamp}.{body}`. "
        "Send `X-Octop-Timestamp`, `X-Octop-Signature`, and optionally "
        "`X-Octop-Event-Id` / `X-Octop-Event-Type`. The raw event is stored "
        "before any agent work; repeat deliveries with the same external id "
        "are deduplicated."
    ),
)
async def receive_webhook(
    source_id: str,
    request: Request,
    server: OctopServer = Depends(get_server),
) -> WebhookAccepted:
    service = _service(server)
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            declared_bytes = int(declared)
        except ValueError:
            declared_bytes = 0
        if declared_bytes > MAX_PAYLOAD_BYTES:
            raise OctopError(
                ErrorCode.EXTERNAL_PAYLOAD_TOO_LARGE,
                f"payload exceeds {MAX_PAYLOAD_BYTES} bytes",
            )
    body = await _read_body_capped(request)
    source = service.verify_webhook(
        source_id=source_id,
        timestamp=request.headers.get(TIMESTAMP_HEADER, ""),
        signature=request.headers.get(SIGNATURE_HEADER, ""),
        body=body,
    )
    # Rate-limit only authenticated callers. Throttling before the signature
    # check let an unauthenticated caller both exhaust a real source's budget
    # (the id is visible in the owner's webhook path) and mint a limiter
    # bucket per random id, growing the map without bound.
    if not service.allow_webhook(source_id):
        raise OctopError(ErrorCode.EXTERNAL_RATE_LIMITED, "too many webhook events")
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise OctopError(ErrorCode.EXTERNAL_EVENT_INVALID, "body must be a JSON object") from exc
    if not isinstance(payload, dict):
        raise OctopError(ErrorCode.EXTERNAL_EVENT_INVALID, "body must be a JSON object")
    event_type, external_id, occurred_at = _event_fields(request, payload)
    result = service.ingest(
        source=source,
        event_type=event_type,
        external_id=external_id,
        occurred_at=occurred_at,
        payload=payload,
    )
    if result.delivery_count and server.app_runtime is not None:
        worker = server.app_runtime.external_event_worker
        if worker is not None:
            worker.wake()
    return WebhookAccepted(
        event_id=result.event.event_id,
        accepted=True,
        duplicate=result.duplicate,
        deliveries=result.delivery_count,
    )
