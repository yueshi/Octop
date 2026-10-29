"""Connector polling source: read-only tool → external events.

Only in-process gateway connectors are supported (the same runner the
light-app data sources use). Each poll parses a JSON result, ingests one event
per item, and advances a cursor stored on the source row.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from octop.config import OctopConfig
from octop.infra.db.repos._base import now_ts
from octop.infra.db.repos.external_sources import ExternalSourceRow
from octop.infra.db.services import SharedServices
from octop.infra.errors import ErrorCode, OctopError

if TYPE_CHECKING:
    from octop.infra.events.service import ExternalEventService

MAX_INTERVAL_SECONDS = 24 * 3600
MIN_INTERVAL_SECONDS = 30
MAX_ITEMS_PER_POLL = 500


@dataclass(frozen=True)
class PollConfig:
    instance_id: str
    tool: str
    args: dict[str, Any]
    interval_seconds: int
    event_type: str
    cursor_arg: str
    cursor_path: str
    items_path: str
    id_path: str
    summary_path: str
    occurred_at_path: str


@dataclass(frozen=True)
class PollResult:
    items: int
    events: int
    cursor: Any


def parse_poll_config(raw: dict[str, Any]) -> PollConfig:
    if not isinstance(raw, dict):
        raise _invalid("poll config must be an object")
    instance_id = str(raw.get("instance_id") or "").strip()
    tool = str(raw.get("tool") or "").strip()
    if not instance_id or not tool:
        raise _invalid("connector_poll requires instance_id and tool")
    args = raw.get("args")
    if args is not None and not isinstance(args, dict):
        raise _invalid("poll args must be an object")
    interval = raw.get("interval_seconds", 300)
    try:
        interval_seconds = int(interval)
    except (TypeError, ValueError) as exc:
        raise _invalid("interval_seconds must be an integer") from exc
    if not MIN_INTERVAL_SECONDS <= interval_seconds <= MAX_INTERVAL_SECONDS:
        raise _invalid(
            f"interval_seconds must be between {MIN_INTERVAL_SECONDS} and {MAX_INTERVAL_SECONDS}"
        )
    event_type = str(raw.get("event_type") or "connector.event").strip()
    return PollConfig(
        instance_id=instance_id,
        tool=tool,
        args=dict(args or {}),
        interval_seconds=interval_seconds,
        event_type=event_type[:100] or "connector.event",
        cursor_arg=_text(raw.get("cursor_arg"), 80),
        cursor_path=_text(raw.get("cursor_path"), 200),
        items_path=_text(raw.get("items_path"), 200),
        id_path=_text(raw.get("id_path"), 200) or "id",
        summary_path=_text(raw.get("summary_path"), 200) or "summary",
        occurred_at_path=_text(raw.get("occurred_at_path"), 200),
    )


def poll_source(
    *,
    service: ExternalEventService,
    source: ExternalSourceRow,
    runner: Any,
    now: int | None = None,
) -> PollResult:
    current = now_ts() if now is None else int(now)
    config = parse_poll_config(_json_dict(source.config_json))
    cursor = _cursor_value(source.cursor_json)
    args = dict(config.args)
    if cursor is not None and config.cursor_arg:
        args[config.cursor_arg] = cursor
    try:
        raw = runner.run(
            instance_id=config.instance_id,
            tool=config.tool,
            args=args,
        )
    except OctopError:
        raise
    except Exception as exc:  # noqa: BLE001 - connector failures become source errors
        raise _invalid(f"connector poll failed: {exc}") from exc
    try:
        result = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise _invalid("connector poll did not return JSON") from exc
    items = _items(result, config.items_path)
    if items is None:
        raise _invalid("connector poll result has no item list")
    items = items[:MAX_ITEMS_PER_POLL]

    events = 0
    new_cursor = cursor
    for item in items:
        if not isinstance(item, dict):
            continue
        payload = dict(item)
        summary = str(_dig(payload, config.summary_path) or config.event_type)
        if not payload.get("summary"):
            payload["summary"] = summary
        external_id = str(_dig(item, config.id_path) or "").strip()
        if not external_id:
            digest = hashlib.sha256(
                json.dumps(item, ensure_ascii=False, sort_keys=True).encode("utf-8")
            ).hexdigest()
            external_id = f"hash:{digest}"
        occurred_at = (
            _int_or_none(_dig(item, config.occurred_at_path)) if config.occurred_at_path else None
        )
        ingested = service.ingest(
            source=source,
            event_type=config.event_type,
            external_id=external_id,
            occurred_at=occurred_at if occurred_at is not None else current,
            payload=payload,
        )
        if not ingested.duplicate:
            events += 1
        if config.cursor_path:
            value = _dig(item, config.cursor_path)
            if value is not None:
                new_cursor = _max_cursor(new_cursor, value)
    return PollResult(items=len(items), events=events, cursor=new_cursor)


def build_connector_runner(*, services: SharedServices, config: OctopConfig) -> Any:
    from octop.infra.apps.sources import GatewayToolRunner  # noqa: PLC0415
    from octop.infra.connectors.service import ConnectorService  # noqa: PLC0415

    repo = services.repos.connector_repo
    return GatewayToolRunner(
        repo=repo,
        service=ConnectorService(
            repo=repo,
            secret_repo=services.secret_repo,
            settings_repo=services.settings_repo,
            config=config,
        ),
    )


def _items(result: Any, path: str) -> list[Any] | None:
    value = _dig(result, path) if path else result
    if isinstance(value, list):
        return value
    if isinstance(value, dict) and not path:
        for key in ("items", "data", "records"):
            candidate = value.get(key)
            if isinstance(candidate, list):
                return candidate
    return None


def _cursor_value(raw: str) -> Any:
    data = _json_dict(raw)
    return data.get("cursor")


def _max_cursor(current: Any, value: Any) -> Any:
    if current is None:
        return value
    return value if _cursor_key(value) > _cursor_key(current) else current


def _cursor_key(value: Any) -> tuple[int, Any]:
    if isinstance(value, bool):
        return (1, str(value))
    if isinstance(value, (int, float)):
        return (0, float(value))
    try:
        return (0, float(str(value)))
    except (TypeError, ValueError):
        return (1, str(value))


def _dig(value: Any, path: str) -> Any:
    current = value
    for part in path.split("."):
        if not part:
            continue
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def _json_dict(raw: str) -> dict[str, Any]:
    try:
        data = json.loads(raw or "{}")
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _int_or_none(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _text(value: Any, limit: int) -> str:
    return str(value or "").strip()[:limit]


def _invalid(message: str) -> OctopError:
    return OctopError(ErrorCode.EXTERNAL_SOURCE_INVALID, message)


__all__ = [
    "PollConfig",
    "PollResult",
    "build_connector_runner",
    "parse_poll_config",
    "poll_source",
]
