"""Management API for the external event plane.

Sources are owned by the caller (admins see all); subscriptions point at a
team or light app that must be writable by the source owner. The public
webhook ingress lives in ``api/routers/hooks.py``.
"""

from __future__ import annotations

import json
from typing import Any, Literal, cast

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field

from octop.api.deps import current_user, get_server
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.events.service import ExternalEventService
from octop.infra.server import OctopServer
from octop.infra.users.identity import User

router = APIRouter()

TargetType = Literal["expert", "team", "app"]
TargetMode = Literal[
    "expert.turn",
    "expert.notify",
    "team.judge",
    "team.notify",
    "app.refresh",
    "app.import",
]


class SourceCreateBody(BaseModel):
    name: str = Field(..., min_length=1, max_length=120, description="Display name.")
    kind: Literal["webhook", "connector_poll"] = Field(
        "webhook", description="webhook (push) or connector_poll (read-only tool)."
    )
    config: dict[str, Any] | None = Field(
        None,
        description=(
            "connector_poll config: instance_id, tool, args, interval_seconds, "
            "event_type, cursor_arg, cursor_path, items_path, id_path, summary_path."
        ),
    )


class SourcePatchBody(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=120)
    enabled: bool | None = None
    config: dict[str, Any] | None = Field(None, description="Replace connector_poll config.")


class SourceOut(BaseModel):
    source_id: str
    name: str
    kind: str
    enabled: bool
    webhook_path: str = Field(description="POST this path with an HMAC signature.")
    config: dict[str, Any] = Field(default_factory=dict)
    cursor: dict[str, Any] = Field(default_factory=dict)
    next_run_at: int | None = None
    last_poll_at: int | None = None
    fail_count: int = 0
    last_error: str
    created_at: int
    updated_at: int


class ConnectorToolOut(BaseModel):
    name: str
    description: str = ""
    read_only: bool
    input_schema: dict[str, Any] = Field(default_factory=dict)


class SourceCreated(SourceOut):
    secret: str = Field(description="HMAC secret; shown once at creation/rotation.")


class SecretRotated(BaseModel):
    source_id: str
    secret: str


class SubscriptionCreateBody(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    source_id: str
    target_type: TargetType
    target_id: str = Field(..., min_length=1, max_length=80)
    mode: TargetMode
    filter_rules: dict[str, Any] | None = Field(
        None,
        alias="filter",
        description='Simple filter: {"event_type": "...", "equals": {...}, "contains": {...}}.',
    )
    template: str = Field("", max_length=2000)
    mapping: dict[str, Any] | None = Field(
        None,
        description=(
            'app.refresh: {"source_id": "<light-app source id>"}; app.import: '
            '{"table": "main", "columns": {...}, "records_path": "data.items"}.'
        ),
    )
    coalesce_seconds: int = Field(0, ge=0, le=3600)
    max_per_minute: int = Field(0, ge=0, le=600)


class SubscriptionPatchBody(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    target_id: str | None = Field(None, min_length=1, max_length=80)
    mode: TargetMode | None = None
    filter_rules: dict[str, Any] | None = Field(None, alias="filter")
    template: str | None = Field(None, max_length=2000)
    mapping: dict[str, Any] | None = None
    coalesce_seconds: int | None = Field(None, ge=0, le=3600)
    max_per_minute: int | None = Field(None, ge=0, le=600)
    enabled: bool | None = None


class SubscriptionOut(BaseModel):
    subscription_id: str
    source_id: str
    target_type: str
    target_id: str
    mode: str
    filter_rules: dict[str, Any] = Field(serialization_alias="filter")
    template: str
    mapping: dict[str, Any]
    coalesce_seconds: int
    max_per_minute: int
    enabled: bool
    created_at: int
    updated_at: int


class EventOut(BaseModel):
    event_id: str
    source_id: str
    external_id: str
    event_type: str
    occurred_at: int
    received_at: int
    summary: str
    payload_bytes: int
    status: str


class DeliveryOut(BaseModel):
    delivery_id: str
    event_id: str
    subscription_id: str
    target_type: str
    target_id: str
    status: str
    attempt: int
    next_retry_at: int | None
    lease_until: int | None
    last_error: str
    delivered_at: int | None
    created_at: int


class RetryDeliveryOut(BaseModel):
    delivery_id: str
    status: str


def _service(server: OctopServer) -> ExternalEventService:
    if server.app_runtime is None or server.app_runtime.external_event_service is None:
        raise OctopError(ErrorCode.INTERNAL_ERROR, "external event service is not started")
    return cast(ExternalEventService, server.app_runtime.external_event_service)


def _json_dict(raw: str) -> dict[str, Any]:
    try:
        data = json.loads(raw or "{}")
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _source_out(row: Any) -> SourceOut:
    return SourceOut(
        source_id=row.source_id,
        name=row.name,
        kind=row.kind,
        enabled=row.enabled,
        webhook_path=(f"/api/hooks/{row.source_id}" if row.kind == "webhook" else ""),
        config=_json_dict(row.config_json),
        cursor=_json_dict(row.cursor_json),
        next_run_at=row.next_run_at,
        last_poll_at=row.last_poll_at,
        fail_count=row.fail_count,
        last_error=row.last_error,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _subscription_out(row: Any) -> SubscriptionOut:
    return SubscriptionOut(
        subscription_id=row.subscription_id,
        source_id=row.source_id,
        target_type=row.target_type,
        target_id=row.target_id,
        mode=row.mode,
        filter_rules=_json_dict(row.filter_json),
        template=row.template,
        mapping=_json_dict(row.mapping_json),
        coalesce_seconds=row.coalesce_seconds,
        max_per_minute=row.max_per_minute,
        enabled=row.enabled,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _event_out(row: Any) -> EventOut:
    return EventOut(
        event_id=row.event_id,
        source_id=row.source_id,
        external_id=row.external_id,
        event_type=row.event_type,
        occurred_at=row.occurred_at,
        received_at=row.received_at,
        summary=row.summary,
        payload_bytes=row.payload_bytes,
        status=row.status,
    )


def _delivery_out(row: Any) -> DeliveryOut:
    return DeliveryOut(
        delivery_id=row.delivery_id,
        event_id=row.event_id,
        subscription_id=row.subscription_id,
        target_type=row.target_type,
        target_id=row.target_id,
        status=row.status,
        attempt=row.attempt,
        next_retry_at=row.next_retry_at,
        lease_until=row.lease_until,
        last_error=row.last_error,
        delivered_at=row.delivered_at,
        created_at=row.created_at,
    )


@router.post(
    "/sources",
    response_model=SourceCreated,
    summary="Create an external event source",
    description="Creates a webhook source and returns its HMAC secret once.",
)
async def create_source(
    body: SourceCreateBody,
    user: User = Depends(current_user),
    server: OctopServer = Depends(get_server),
) -> SourceCreated:
    row, secret = _service(server).create_source(
        owner_user_id=user.id,
        name=body.name,
        kind=body.kind,
        config=body.config,
        is_admin=user.is_admin,
    )
    return SourceCreated(**_source_out(row).model_dump(), secret=secret)


@router.get("/sources", response_model=list[SourceOut], summary="List event sources")
async def list_sources(
    user: User = Depends(current_user),
    server: OctopServer = Depends(get_server),
) -> list[SourceOut]:
    return [
        _source_out(row)
        for row in _service(server).list_sources(user_id=user.id, is_admin=user.is_admin)
    ]


@router.patch("/sources/{source_id}", response_model=SourceOut, summary="Update a source")
async def update_source(
    source_id: str,
    body: SourcePatchBody,
    user: User = Depends(current_user),
    server: OctopServer = Depends(get_server),
) -> SourceOut:
    row = _service(server).update_source(
        user_id=user.id,
        is_admin=user.is_admin,
        source_id=source_id,
        name=body.name,
        enabled=body.enabled,
        config=body.config,
    )
    return _source_out(row)


@router.delete("/sources/{source_id}", status_code=204, summary="Delete a source")
async def delete_source(
    source_id: str,
    user: User = Depends(current_user),
    server: OctopServer = Depends(get_server),
) -> None:
    _service(server).delete_source(user_id=user.id, is_admin=user.is_admin, source_id=source_id)


@router.get(
    "/connector-tools",
    response_model=list[ConnectorToolOut],
    summary="List read-only connector tools",
    description="Tools of one in-process gateway connector instance for poll setup.",
)
async def list_connector_tools(
    instance_id: str = Query(..., description="Connector instance id."),
    user: User = Depends(current_user),
    server: OctopServer = Depends(get_server),
) -> list[ConnectorToolOut]:
    return [
        ConnectorToolOut(
            name=str(meta["name"]),
            description=str(meta.get("description") or ""),
            read_only=bool(meta["read_only"]),
            input_schema=dict(meta.get("input_schema") or {}),
        )
        for meta in _service(server).connector_tools(
            user_id=user.id, is_admin=user.is_admin, instance_id=instance_id
        )
        if meta["read_only"]
    ]


@router.post(
    "/sources/{source_id}/poll",
    response_model=SourceOut,
    summary="Poll a connector source now",
    description="Schedules a connector_poll source for the next worker tick.",
)
async def poll_source_now(
    source_id: str,
    user: User = Depends(current_user),
    server: OctopServer = Depends(get_server),
) -> SourceOut:
    row = _service(server).run_poll_now(
        user_id=user.id, is_admin=user.is_admin, source_id=source_id
    )
    if server.app_runtime is not None and server.app_runtime.external_event_worker is not None:
        server.app_runtime.external_event_worker.request_poll()
    return _source_out(row)


@router.post(
    "/sources/{source_id}/rotate-secret",
    response_model=SecretRotated,
    summary="Rotate the webhook secret",
)
async def rotate_secret(
    source_id: str,
    user: User = Depends(current_user),
    server: OctopServer = Depends(get_server),
) -> SecretRotated:
    _row, secret = _service(server).rotate_secret(
        user_id=user.id, is_admin=user.is_admin, source_id=source_id
    )
    return SecretRotated(source_id=source_id, secret=secret)


@router.post(
    "/subscriptions",
    response_model=SubscriptionOut,
    summary="Create a subscription",
    description="Validates that the target is an expert/team/app writable by the source owner.",
)
async def create_subscription(
    body: SubscriptionCreateBody,
    user: User = Depends(current_user),
    server: OctopServer = Depends(get_server),
) -> SubscriptionOut:
    row = _service(server).create_subscription(
        user_id=user.id,
        is_admin=user.is_admin,
        source_id=body.source_id,
        target_type=body.target_type,
        target_id=body.target_id,
        mode=body.mode,
        filter_rules=body.filter_rules,
        template=body.template,
        mapping=body.mapping,
        coalesce_seconds=body.coalesce_seconds,
        max_per_minute=body.max_per_minute,
    )
    return _subscription_out(row)


@router.get(
    "/subscriptions",
    response_model=list[SubscriptionOut],
    summary="List subscriptions",
)
async def list_subscriptions(
    source_id: str | None = Query(None),
    user: User = Depends(current_user),
    server: OctopServer = Depends(get_server),
) -> list[SubscriptionOut]:
    return [
        _subscription_out(row)
        for row in _service(server).list_subscriptions(
            user_id=user.id, is_admin=user.is_admin, source_id=source_id
        )
    ]


@router.patch(
    "/subscriptions/{subscription_id}",
    response_model=SubscriptionOut,
    summary="Update a subscription",
)
async def update_subscription(
    subscription_id: str,
    body: SubscriptionPatchBody,
    user: User = Depends(current_user),
    server: OctopServer = Depends(get_server),
) -> SubscriptionOut:
    row = _service(server).update_subscription(
        user_id=user.id,
        is_admin=user.is_admin,
        subscription_id=subscription_id,
        target_id=body.target_id,
        mode=body.mode,
        filter_rules=body.filter_rules,
        template=body.template,
        mapping=body.mapping,
        coalesce_seconds=body.coalesce_seconds,
        max_per_minute=body.max_per_minute,
        enabled=body.enabled,
    )
    return _subscription_out(row)


@router.delete(
    "/subscriptions/{subscription_id}",
    status_code=204,
    summary="Delete a subscription",
)
async def delete_subscription(
    subscription_id: str,
    user: User = Depends(current_user),
    server: OctopServer = Depends(get_server),
) -> None:
    _service(server).delete_subscription(
        user_id=user.id, is_admin=user.is_admin, subscription_id=subscription_id
    )


@router.get("/events", response_model=list[EventOut], summary="List received events")
async def list_events(
    source_id: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user: User = Depends(current_user),
    server: OctopServer = Depends(get_server),
) -> list[EventOut]:
    return [
        _event_out(row)
        for row in _service(server).list_events(
            user_id=user.id,
            is_admin=user.is_admin,
            source_id=source_id,
            limit=limit,
            offset=offset,
        )
    ]


@router.get(
    "/deliveries",
    response_model=list[DeliveryOut],
    summary="List event deliveries",
)
async def list_deliveries(
    source_id: str | None = Query(None),
    status: Literal["pending", "delivering", "delivered", "failed", "dead"] | None = Query(None),
    limit: int = Query(100, ge=1, le=500),
    user: User = Depends(current_user),
    server: OctopServer = Depends(get_server),
) -> list[DeliveryOut]:
    return [
        _delivery_out(row)
        for row in _service(server).list_deliveries(
            user_id=user.id,
            is_admin=user.is_admin,
            source_id=source_id,
            status=status,
            limit=limit,
        )
    ]


@router.post(
    "/deliveries/{delivery_id}/retry",
    response_model=RetryDeliveryOut,
    summary="Retry a failed delivery",
)
async def retry_delivery(
    delivery_id: str,
    user: User = Depends(current_user),
    server: OctopServer = Depends(get_server),
) -> RetryDeliveryOut:
    _service(server).retry_delivery(
        user_id=user.id, is_admin=user.is_admin, delivery_id=delivery_id
    )
    if server.app_runtime is not None and server.app_runtime.external_event_worker is not None:
        server.app_runtime.external_event_worker.wake()
    return RetryDeliveryOut(delivery_id=delivery_id, status="pending")
