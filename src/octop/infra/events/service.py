"""External event service: sources, subscriptions, ingest, and queries.

The HTTP layer only validates transport concerns (body cap, JSON) and maps
errors; ownership, target ACL, filtering, and deduplication live here.
"""

from __future__ import annotations

import hashlib
import json
import secrets
from dataclasses import dataclass
from typing import Any

from octop.infra.connectors.crypto import decrypt_credentials, encrypt_credentials
from octop.infra.db.repos._base import UNSET, now_ts
from octop.infra.db.repos.apps import PERMISSION_EDIT_DATA, PERMISSION_RANK
from octop.infra.db.repos.external_deliveries import ExternalDeliveryRow
from octop.infra.db.repos.external_events import ExternalEventRow
from octop.infra.db.repos.external_sources import ExternalSourceRow
from octop.infra.db.repos.external_subscriptions import ExternalSubscriptionRow
from octop.infra.db.repos.secrets import SecretRepo
from octop.infra.db.services import RepoBundle
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.events.connector_poll import parse_poll_config
from octop.infra.events.rate_limit import DEFAULT_LIMIT_PER_MINUTE, SlidingWindowLimiter
from octop.infra.events.security import REPLAY_WINDOW_SECONDS, verify_signature

SOURCE_KIND_WEBHOOK = "webhook"
SOURCE_KIND_CONNECTOR_POLL = "connector_poll"
SOURCE_KINDS = (SOURCE_KIND_WEBHOOK, SOURCE_KIND_CONNECTOR_POLL)

TARGET_EXPERT = "expert"
TARGET_TEAM = "team"
TARGET_APP = "app"
TARGET_TYPES = (TARGET_EXPERT, TARGET_TEAM, TARGET_APP)

MODE_EXPERT_TURN = "expert.turn"
MODE_EXPERT_NOTIFY = "expert.notify"
MODE_TEAM_JUDGE = "team.judge"
MODE_TEAM_NOTIFY = "team.notify"
MODE_APP_REFRESH = "app.refresh"
MODE_APP_IMPORT = "app.import"
TARGET_MODES = {
    TARGET_EXPERT: (MODE_EXPERT_TURN, MODE_EXPERT_NOTIFY),
    TARGET_TEAM: (MODE_TEAM_JUDGE, MODE_TEAM_NOTIFY),
    TARGET_APP: (MODE_APP_REFRESH, MODE_APP_IMPORT),
}

MAX_NAME_CHARS = 120
MAX_SUMMARY_CHARS = 500
MAX_EVENT_TYPE_CHARS = 100
MAX_EXTERNAL_ID_CHARS = 200


@dataclass(frozen=True)
class IngestResult:
    event: ExternalEventRow
    duplicate: bool
    delivery_count: int


def _invalid(code: ErrorCode, message: str) -> OctopError:
    return OctopError(code, message)


def _text(value: object, *, limit: int) -> str:
    return str(value or "").strip()[:limit]


def _json_dict(raw: str) -> dict[str, Any]:
    try:
        data = json.loads(raw or "{}")
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def matches_filter(
    subscription: ExternalSubscriptionRow, event_type: str, payload: dict[str, Any]
) -> bool:
    """MVP filter: exact ``event_type``, plus top-level equals/contains maps."""
    rules = _json_dict(subscription.filter_json)
    wanted = rules.get("event_type")
    if isinstance(wanted, str) and wanted and wanted != event_type:
        return False
    equals = rules.get("equals")
    if isinstance(equals, dict):
        for key, value in equals.items():
            if payload.get(str(key)) != value:
                return False
    contains = rules.get("contains")
    if isinstance(contains, dict):
        for key, value in contains.items():
            actual = payload.get(str(key))
            if not isinstance(actual, str) or str(value) not in actual:
                return False
    return True


class ExternalEventService:
    def __init__(
        self,
        *,
        repos: RepoBundle,
        secret_repo: SecretRepo,
        limiter: SlidingWindowLimiter | None = None,
        connector_runner: Any | None = None,
    ) -> None:
        self._repos = repos
        self._secret_repo = secret_repo
        self._limiter = limiter or SlidingWindowLimiter()
        self._connector_runner = connector_runner

    # ----- sources -----------------------------------------------------------

    def create_source(
        self,
        *,
        owner_user_id: int,
        name: str,
        kind: str = SOURCE_KIND_WEBHOOK,
        config: dict[str, Any] | None = None,
        is_admin: bool = False,
    ) -> tuple[ExternalSourceRow, str]:
        clean_name = _text(name, limit=MAX_NAME_CHARS)
        if not clean_name:
            raise _invalid(ErrorCode.EXTERNAL_SOURCE_INVALID, "source name is required")
        if kind not in SOURCE_KINDS:
            raise _invalid(ErrorCode.EXTERNAL_SOURCE_INVALID, f"unsupported source kind: {kind}")
        if kind == SOURCE_KIND_CONNECTOR_POLL:
            self._validate_poll_config(
                owner_user_id=owner_user_id,
                config=config or {},
                is_admin=is_admin,
            )
        secret = secrets.token_urlsafe(32)
        blob = encrypt_credentials(self._secret_repo, {"secret": secret})
        row = self._repos.external_source_repo.create(
            owner_user_id=owner_user_id,
            name=clean_name,
            kind=kind,
            config_json=json.dumps(config or {}, ensure_ascii=False, separators=(",", ":")),
            secret_blob=blob,
        )
        return row, secret

    def rotate_secret(
        self, *, user_id: int, is_admin: bool, source_id: str
    ) -> tuple[ExternalSourceRow, str]:
        source = self._source_for(user_id, is_admin, source_id)
        if source.kind != SOURCE_KIND_WEBHOOK:
            raise _invalid(
                ErrorCode.EXTERNAL_SOURCE_INVALID, "secret is only used by webhook sources"
            )
        secret = secrets.token_urlsafe(32)
        blob = encrypt_credentials(self._secret_repo, {"secret": secret})
        updated = self._repos.external_source_repo.update(source_id, secret_blob=blob)
        if updated is None:
            raise _invalid(ErrorCode.EXTERNAL_SOURCE_NOT_FOUND, "source not found")
        return updated, secret

    def list_sources(self, *, user_id: int, is_admin: bool) -> list[ExternalSourceRow]:
        return self._repos.external_source_repo.list_for_owner(None if is_admin else user_id)

    def get_source(self, *, user_id: int, is_admin: bool, source_id: str) -> ExternalSourceRow:
        return self._source_for(user_id, is_admin, source_id)

    def update_source(
        self,
        *,
        user_id: int,
        is_admin: bool,
        source_id: str,
        name: str | None = None,
        enabled: bool | None = None,
        config: dict[str, Any] | None = None,
    ) -> ExternalSourceRow:
        source = self._source_for(user_id, is_admin, source_id)
        clean_name = None if name is None else _text(name, limit=MAX_NAME_CHARS)
        if clean_name is not None and not clean_name:
            raise _invalid(ErrorCode.EXTERNAL_SOURCE_INVALID, "source name is required")
        if config is not None:
            if source.kind != SOURCE_KIND_CONNECTOR_POLL:
                raise _invalid(
                    ErrorCode.EXTERNAL_SOURCE_INVALID,
                    "config is only supported for connector_poll sources",
                )
            self._validate_poll_config(
                owner_user_id=source.owner_user_id,
                config=config,
                is_admin=is_admin,
            )
        updated = self._repos.external_source_repo.update(
            source_id,
            name=clean_name if clean_name is not None else UNSET,
            enabled=enabled if enabled is not None else UNSET,
            config_json=(
                json.dumps(config, ensure_ascii=False, separators=(",", ":"))
                if config is not None
                else UNSET
            ),
        )
        if updated is None:
            raise _invalid(ErrorCode.EXTERNAL_SOURCE_NOT_FOUND, "source not found")
        return updated

    def delete_source(self, *, user_id: int, is_admin: bool, source_id: str) -> None:
        self._source_for(user_id, is_admin, source_id)
        if not self._repos.external_source_repo.delete(source_id):
            raise _invalid(ErrorCode.EXTERNAL_SOURCE_NOT_FOUND, "source not found")

    def _source_for(self, user_id: int, is_admin: bool, source_id: str) -> ExternalSourceRow:
        row = self._repos.external_source_repo.get(source_id)
        if row is None or (not is_admin and row.owner_user_id != user_id):
            raise _invalid(ErrorCode.EXTERNAL_SOURCE_NOT_FOUND, "source not found")
        return row

    def run_poll_now(self, *, user_id: int, is_admin: bool, source_id: str) -> ExternalSourceRow:
        source = self._source_for(user_id, is_admin, source_id)
        if source.kind != SOURCE_KIND_CONNECTOR_POLL:
            raise _invalid(ErrorCode.EXTERNAL_SOURCE_INVALID, "source is not a connector poll")
        updated = self._repos.external_source_repo.update(source_id, next_run_at=0)
        if updated is None:
            raise _invalid(ErrorCode.EXTERNAL_SOURCE_NOT_FOUND, "source not found")
        return updated

    def connector_tools(
        self, *, user_id: int, is_admin: bool, instance_id: str
    ) -> list[dict[str, Any]]:
        self._validate_connector_instance(
            owner_user_id=user_id, instance_id=instance_id, is_admin=is_admin
        )
        runner = self._connector_runner
        if runner is None:
            raise _invalid(ErrorCode.EXTERNAL_SOURCE_INVALID, "connector runner unavailable")
        return list(runner.list_tool_meta(instance_id))

    def _validate_poll_config(
        self, *, owner_user_id: int, config: dict[str, Any], is_admin: bool
    ) -> None:
        parsed = parse_poll_config(config)
        self._validate_connector_instance(
            owner_user_id=owner_user_id,
            instance_id=parsed.instance_id,
            is_admin=is_admin,
        )
        runner = self._connector_runner
        if runner is None:
            raise _invalid(ErrorCode.EXTERNAL_SOURCE_INVALID, "connector runner unavailable")
        meta = runner.tool_meta(parsed.instance_id, parsed.tool)
        if meta is None:
            raise _invalid(
                ErrorCode.EXTERNAL_SOURCE_INVALID,
                f"tool {parsed.tool!r} is not available on connector {parsed.instance_id!r}",
            )
        if not meta.get("read_only"):
            raise _invalid(
                ErrorCode.EXTERNAL_SOURCE_INVALID,
                f"tool {parsed.tool!r} is not read-only and cannot back an event source",
            )

    def _validate_connector_instance(
        self, *, owner_user_id: int, instance_id: str, is_admin: bool
    ) -> None:
        inst = self._repos.connector_repo.get(instance_id)
        if inst is None:
            raise _invalid(ErrorCode.EXTERNAL_SOURCE_INVALID, "connector instance not found")
        if not is_admin and inst.user_id != owner_user_id and not inst.shared:
            raise _invalid(
                ErrorCode.EXTERNAL_TARGET_INVALID,
                "connector instance is not owned by the source owner",
            )
        if inst.status != "active":
            raise _invalid(ErrorCode.EXTERNAL_SOURCE_INVALID, "connector instance is not active")

    # ----- subscriptions -----------------------------------------------------

    def create_subscription(
        self,
        *,
        user_id: int,
        is_admin: bool,
        source_id: str,
        target_type: str,
        target_id: str,
        mode: str,
        filter_rules: dict[str, Any] | None = None,
        template: str = "",
        mapping: dict[str, Any] | None = None,
        coalesce_seconds: int = 0,
        max_per_minute: int = 0,
    ) -> ExternalSubscriptionRow:
        source = self._source_for(user_id, is_admin, source_id)
        self._validate_target(
            source=source,
            target_type=target_type,
            target_id=target_id,
            mode=mode,
            mapping=mapping or {},
            is_admin=is_admin,
        )
        return self._repos.external_subscription_repo.create(
            source_id=source_id,
            target_type=target_type,
            target_id=target_id,
            mode=mode,
            created_by=user_id,
            filter_json=json.dumps(filter_rules or {}, ensure_ascii=False, separators=(",", ":")),
            template=_text(template, limit=2000),
            mapping_json=json.dumps(mapping or {}, ensure_ascii=False, separators=(",", ":")),
            coalesce_seconds=_bounded(coalesce_seconds, 0, 3600),
            max_per_minute=_bounded(max_per_minute, 0, 600),
        )

    def list_subscriptions(
        self, *, user_id: int, is_admin: bool, source_id: str | None = None
    ) -> list[ExternalSubscriptionRow]:
        if source_id is not None:
            self._source_for(user_id, is_admin, source_id)
            return self._repos.external_subscription_repo.list_for_source(source_id)
        return self._repos.external_subscription_repo.list_for_owner(None if is_admin else user_id)

    def update_subscription(
        self,
        *,
        user_id: int,
        is_admin: bool,
        subscription_id: str,
        target_id: str | None = None,
        mode: str | None = None,
        filter_rules: dict[str, Any] | None = None,
        template: str | None = None,
        mapping: dict[str, Any] | None = None,
        coalesce_seconds: int | None = None,
        max_per_minute: int | None = None,
        enabled: bool | None = None,
    ) -> ExternalSubscriptionRow:
        row = self._subscription_for(user_id, is_admin, subscription_id)
        source = self._repos.external_source_repo.get(row.source_id)
        if source is None:
            raise _invalid(ErrorCode.EXTERNAL_SOURCE_NOT_FOUND, "source not found")
        effective_target = target_id if target_id is not None else row.target_id
        effective_mode = mode if mode is not None else row.mode
        effective_mapping = mapping if mapping is not None else _json_dict(row.mapping_json)
        if target_id is not None or mode is not None or mapping is not None or enabled is True:
            self._validate_target(
                source=source,
                target_type=row.target_type,
                target_id=effective_target,
                mode=effective_mode,
                mapping=effective_mapping,
                is_admin=is_admin,
            )
        updated = self._repos.external_subscription_repo.update(
            subscription_id,
            target_id=target_id if target_id is not None else UNSET,
            mode=mode if mode is not None else UNSET,
            filter_json=(
                json.dumps(filter_rules, ensure_ascii=False, separators=(",", ":"))
                if filter_rules is not None
                else UNSET
            ),
            template=_text(template, limit=2000) if template is not None else UNSET,
            mapping_json=(
                json.dumps(mapping, ensure_ascii=False, separators=(",", ":"))
                if mapping is not None
                else UNSET
            ),
            coalesce_seconds=(
                _bounded(coalesce_seconds, 0, 3600) if coalesce_seconds is not None else UNSET
            ),
            max_per_minute=(
                _bounded(max_per_minute, 0, 600) if max_per_minute is not None else UNSET
            ),
            enabled=enabled if enabled is not None else UNSET,
        )
        if updated is None:
            raise _invalid(ErrorCode.EXTERNAL_SUBSCRIPTION_INVALID, "subscription not found")
        return updated

    def delete_subscription(self, *, user_id: int, is_admin: bool, subscription_id: str) -> None:
        self._subscription_for(user_id, is_admin, subscription_id)
        if not self._repos.external_subscription_repo.delete(subscription_id):
            raise _invalid(ErrorCode.EXTERNAL_SUBSCRIPTION_INVALID, "subscription not found")

    def _subscription_for(
        self, user_id: int, is_admin: bool, subscription_id: str
    ) -> ExternalSubscriptionRow:
        row = self._repos.external_subscription_repo.get(subscription_id)
        if row is None:
            raise _invalid(ErrorCode.EXTERNAL_SUBSCRIPTION_INVALID, "subscription not found")
        source = self._repos.external_source_repo.get(row.source_id)
        if source is None or (not is_admin and source.owner_user_id != user_id):
            raise _invalid(ErrorCode.EXTERNAL_SUBSCRIPTION_INVALID, "subscription not found")
        return row

    def _validate_target(
        self,
        *,
        source: ExternalSourceRow,
        target_type: str,
        target_id: str,
        mode: str,
        mapping: dict[str, Any],
        is_admin: bool,
    ) -> None:
        if target_type not in TARGET_TYPES:
            raise _invalid(
                ErrorCode.EXTERNAL_SUBSCRIPTION_INVALID,
                f"unsupported target type: {target_type!r}",
            )
        if mode not in TARGET_MODES[target_type]:
            raise _invalid(
                ErrorCode.EXTERNAL_SUBSCRIPTION_INVALID,
                f"mode {mode!r} is not valid for target {target_type!r}",
            )
        if target_type == TARGET_EXPERT:
            agent = self._repos.agent_repo.get(target_id)
            if agent is None or getattr(agent, "kind", "") != "expert":
                raise _invalid(ErrorCode.EXTERNAL_TARGET_INVALID, "expert target not found")
            if not is_admin and agent.user_id != source.owner_user_id:
                raise _invalid(
                    ErrorCode.EXTERNAL_TARGET_INVALID,
                    "expert target is not owned by the source owner",
                )
            return
        if target_type == TARGET_TEAM:
            agent = self._repos.agent_repo.get(target_id)
            if agent is None or getattr(agent, "kind", "") != "team":
                raise _invalid(ErrorCode.EXTERNAL_TARGET_INVALID, "team target not found")
            if not is_admin and agent.user_id != source.owner_user_id:
                raise _invalid(
                    ErrorCode.EXTERNAL_TARGET_INVALID,
                    "team target is not owned by the source owner",
                )
            if mode == MODE_TEAM_JUDGE:
                runtime = self._repos.team_runtime_repo.get(target_id)
                if runtime is None or runtime.mode != "autopilot":
                    raise _invalid(
                        ErrorCode.EXTERNAL_SUBSCRIPTION_INVALID,
                        "team.judge requires an autopilot team",
                    )
            return

        app = self._repos.light_app_repo.get(target_id)
        if app is None:
            raise _invalid(ErrorCode.EXTERNAL_TARGET_INVALID, "app target not found")
        permission = self._repos.light_app_repo.permission_for(app.app_id, source.owner_user_id)
        if (
            not is_admin
            and app.owner_user_id != source.owner_user_id
            and (
                permission is None
                or PERMISSION_RANK.get(permission, 0) < PERMISSION_RANK[PERMISSION_EDIT_DATA]
            )
        ):
            raise _invalid(
                ErrorCode.EXTERNAL_TARGET_INVALID,
                "app target is not writable by the source owner",
            )
        if mode == MODE_APP_REFRESH:
            app_source_id = mapping.get("source_id") or mapping.get("app_source_id")
            if not isinstance(app_source_id, str) or not app_source_id:
                raise _invalid(
                    ErrorCode.EXTERNAL_SUBSCRIPTION_INVALID,
                    "app.refresh requires mapping.source_id (a light-app data source)",
                )
            if self._repos.light_app_repo.get_source(app.app_id, app_source_id) is None:
                raise _invalid(ErrorCode.EXTERNAL_TARGET_INVALID, "app data source not found")

    # ----- ingest + queries --------------------------------------------------

    def allow_webhook(self, source_id: str, *, now: int | None = None) -> bool:
        return self._limiter.allow(
            source_id,
            now=now,
            limit=DEFAULT_LIMIT_PER_MINUTE,
        )

    def verify_webhook(
        self, *, source_id: str, timestamp: str, signature: str, body: bytes
    ) -> ExternalSourceRow:
        source = self._repos.external_source_repo.get(source_id)
        if (
            source is None
            or source.kind != SOURCE_KIND_WEBHOOK
            or not source.enabled
            or source.secret_blob is None
        ):
            raise _invalid(ErrorCode.EXTERNAL_SOURCE_NOT_FOUND, "source not found")
        try:
            sent_at = int(timestamp)
        except (TypeError, ValueError):
            raise _invalid(
                ErrorCode.EXTERNAL_TIMESTAMP_INVALID, "missing or invalid timestamp"
            ) from None
        now = now_ts()
        if abs(now - sent_at) > REPLAY_WINDOW_SECONDS:
            raise _invalid(ErrorCode.EXTERNAL_TIMESTAMP_INVALID, "timestamp outside replay window")
        secret = str(decrypt_credentials(self._secret_repo, source.secret_blob).get("secret") or "")
        if not secret or not verify_signature(
            secret,
            timestamp=timestamp,
            signature=signature,
            body=body,
            now=now,
        ):
            raise _invalid(ErrorCode.EXTERNAL_SIGNATURE_INVALID, "signature mismatch")
        return source

    def ingest(
        self,
        *,
        source: ExternalSourceRow,
        event_type: str,
        external_id: str,
        occurred_at: int | None,
        payload: dict[str, Any],
    ) -> IngestResult:
        now = now_ts()
        clean_type = _text(event_type, limit=MAX_EVENT_TYPE_CHARS) or "external.event"
        clean_external_id = _text(external_id, limit=MAX_EXTERNAL_ID_CHARS)
        payload_json = json.dumps(
            payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        )
        summary = _text(payload.get("summary"), limit=MAX_SUMMARY_CHARS) or clean_type
        if clean_external_id:
            dedupe_key = f"{source.source_id}:{clean_external_id}"
        else:
            digest = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
            dedupe_key = f"{source.source_id}:hash:{digest}:{now // 60}"
        existing = self._repos.external_event_repo.get_by_dedupe(dedupe_key)
        if existing is not None:
            from octop.infra.metrics import METRICS  # noqa: PLC0415

            METRICS.inc("external_event_duplicate_total")
            return IngestResult(event=existing, duplicate=True, delivery_count=0)
        subscriptions = []
        for item in self._repos.external_subscription_repo.list_for_source(
            source.source_id, enabled_only=True
        ):
            if not matches_filter(item, clean_type, payload):
                continue
            if item.max_per_minute and not self._limiter.allow(
                f"subscription:{item.subscription_id}", limit=item.max_per_minute
            ):
                continue
            subscriptions.append(item)
        event, duplicate, delivery_count = self._repos.external_event_repo.ingest(
            source_id=source.source_id,
            event_type=clean_type,
            external_id=clean_external_id,
            occurred_at=int(occurred_at) if occurred_at is not None else now,
            summary=summary,
            payload_json=payload_json,
            payload_bytes=len(payload_json.encode("utf-8")),
            dedupe_key=dedupe_key,
            subscriptions=[
                (item.subscription_id, item.target_type, item.target_id) for item in subscriptions
            ],
        )
        from octop.infra.metrics import METRICS  # noqa: PLC0415

        METRICS.inc(
            "external_event_duplicate_total" if duplicate else "external_event_received_total"
        )
        return IngestResult(event=event, duplicate=duplicate, delivery_count=delivery_count)

    def list_events(
        self,
        *,
        user_id: int,
        is_admin: bool,
        source_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[ExternalEventRow]:
        if source_id is not None:
            self._source_for(user_id, is_admin, source_id)
        return self._repos.external_event_repo.list_for_owner(
            None if is_admin else user_id,
            source_id=source_id,
            limit=_bounded(limit, 1, 200),
            offset=max(0, offset),
        )

    def list_deliveries(
        self,
        *,
        user_id: int,
        is_admin: bool,
        source_id: str | None = None,
        status: str | None = None,
        limit: int = 100,
    ) -> list[ExternalDeliveryRow]:
        if source_id is not None:
            self._source_for(user_id, is_admin, source_id)
        return self._repos.external_delivery_repo.list_for_owner(
            None if is_admin else user_id,
            source_id=source_id,
            status=status,
            limit=_bounded(limit, 1, 500),
        )

    def retry_delivery(self, *, user_id: int, is_admin: bool, delivery_id: str) -> None:
        delivery = self._repos.external_delivery_repo.get(delivery_id)
        if delivery is None:
            raise _invalid(ErrorCode.NOT_FOUND, "delivery not found")
        event = self._repos.external_event_repo.get(delivery.event_id)
        if event is None:
            raise _invalid(ErrorCode.NOT_FOUND, "event not found")
        source = self._repos.external_source_repo.get(event.source_id)
        if source is None or (not is_admin and source.owner_user_id != user_id):
            raise _invalid(ErrorCode.NOT_FOUND, "delivery not found")
        if not self._repos.external_delivery_repo.retry(delivery_id):
            raise _invalid(
                ErrorCode.EXTERNAL_EVENT_INVALID,
                "only failed or dead deliveries can be retried",
            )


def _bounded(value: int, low: int, high: int) -> int:
    return max(low, min(high, int(value)))


__all__ = [
    "ExternalEventService",
    "IngestResult",
    "matches_filter",
]
