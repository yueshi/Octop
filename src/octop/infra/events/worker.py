"""In-process outbox worker for external event deliveries.

Claims due rows with a lease, dispatches to target adapters (team/app), and
retries failures with exponential backoff. The worker is single-process and
started/stopped with :class:`OctopServer`; a restart requeues expired leases.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from octop.infra.db.repos._base import now_ts
from octop.infra.db.repos.apps import PERMISSION_EDIT_DATA, PERMISSION_RANK
from octop.infra.db.repos.external_deliveries import ExternalDeliveryRow
from octop.infra.db.repos.external_events import ExternalEventRow
from octop.infra.db.repos.external_sources import ExternalSourceRow
from octop.infra.db.repos.external_subscriptions import ExternalSubscriptionRow
from octop.infra.db.services import RepoBundle
from octop.infra.errors import OctopError
from octop.infra.events.connector_poll import parse_poll_config, poll_source
from octop.infra.events.service import (
    MODE_APP_IMPORT,
    MODE_APP_REFRESH,
    MODE_EXPERT_TURN,
    MODE_TEAM_JUDGE,
    MODE_TEAM_NOTIFY,
)
from octop.infra.events.templating import render_template

logger = logging.getLogger(__name__)

RETRY_DELAYS = (5, 30, 120, 600, 3600)
MAX_ATTEMPTS = 6
LEASE_SECONDS = 60
MAX_CONCURRENT_APP = 4
MAX_CONCURRENT_TEAM = 2
EXPERT_TURN_LEASE_SECONDS = 900
EXPERT_COALESCE_GRACE_SECONDS = 60
POLL_SCAN_SECONDS = 30
POLL_LEASE_SECONDS = 300
RETENTION_SECONDS = 30 * 24 * 3600
CLEANUP_INTERVAL_SECONDS = 6 * 3600

TeamTargetResolver = Callable[[str], Awaitable[Any | None]]
UserPusher = Callable[[int, dict[str, Any]], Awaitable[None]]
ExpertDeliver = Callable[
    [ExternalEventRow, ExternalSubscriptionRow, ExternalSourceRow, int],
    Awaitable[None],
]


class DeliveryError(Exception):
    def __init__(self, message: str, *, retryable: bool = True) -> None:
        super().__init__(message)
        self.retryable = retryable


def _json_dict(raw: str) -> dict[str, Any]:
    try:
        data = json.loads(raw or "{}")
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _dig(value: Any, path: str) -> Any:
    current = value
    for part in path.split("."):
        if not part:
            continue
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def _records(payload: dict[str, Any], path: object) -> list[dict[str, Any]] | None:
    value = _dig(payload, str(path)) if isinstance(path, str) and path else None
    if value is None:
        value = payload.get("records")
    if value is None:
        value = payload.get("data")
    # An empty replace would wipe the app table; require at least one record.
    if value and isinstance(value, list) and all(isinstance(item, dict) for item in value):
        return [dict(item) for item in value]
    return None


class ExternalEventWorker:
    def __init__(
        self,
        *,
        repos: RepoBundle,
        app_service: Any | None,
        coordinator: Any | None = None,
        resolve_team_target: TeamTargetResolver | None = None,
        push_user: UserPusher | None = None,
        expert_delivery: ExpertDeliver | None = None,
        event_service: Any | None = None,
        connector_runner: Any | None = None,
        interval_seconds: float = 1.0,
    ) -> None:
        self._repos = repos
        self._app_service = app_service
        self._coordinator = coordinator
        self._resolve_team_target = resolve_team_target
        self._push_user = push_user
        self._expert_delivery = expert_delivery
        self._event_service = event_service
        self._connector_runner = connector_runner
        self._last_poll_scan = 0
        self._last_cleanup_at = 0
        self._interval = interval_seconds
        self._task: asyncio.Task[None] | None = None
        self._wake = asyncio.Event()
        self._tasks: set[asyncio.Task[None]] = set()
        self._app_sem = asyncio.Semaphore(MAX_CONCURRENT_APP)
        self._team_sem = asyncio.Semaphore(MAX_CONCURRENT_TEAM)
        self._expert_sems: dict[str, asyncio.Semaphore] = {}
        self._expert_batches: dict[str, list[ExternalDeliveryRow]] = {}
        self._expert_flush_tasks: dict[str, asyncio.Task[None]] = {}

    def replace_persistence(
        self,
        *,
        repos: RepoBundle,
        app_service: Any | None,
        coordinator: Any | None,
        resolve_team_target: TeamTargetResolver | None,
        push_user: UserPusher | None,
        expert_delivery: ExpertDeliver | None,
        event_service: Any | None,
        connector_runner: Any | None,
    ) -> None:
        self._repos = repos
        self._app_service = app_service
        self._coordinator = coordinator
        self._resolve_team_target = resolve_team_target
        self._push_user = push_user
        self._expert_delivery = expert_delivery
        self._event_service = event_service
        self._connector_runner = connector_runner

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._last_poll_scan = 0
            self._last_cleanup_at = 0
            self._repos.external_delivery_repo.requeue_expired(now_ts())
            self._task = asyncio.create_task(self._loop(), name="external-event-worker")

    async def shutdown(self) -> None:
        task = self._task
        self._task = None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        now = now_ts()
        for batch in list(self._expert_batches.values()):
            for delivery in batch:
                self._repos.external_delivery_repo.finish(
                    delivery.delivery_id,
                    status="failed",
                    attempt=delivery.attempt + 1,
                    last_error="worker shutdown",
                    next_retry_at=now,
                )
        self._expert_batches.clear()
        for flush in list(self._expert_flush_tasks.values()):
            flush.cancel()
        self._expert_flush_tasks.clear()
        pending = list(self._tasks)
        for item in pending:
            item.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self._tasks.clear()

    def wake(self) -> None:
        self._wake.set()

    def request_poll(self) -> None:
        """Wake the loop and bypass the 30s connector-poll scan interval."""
        self._last_poll_scan = 0
        self._wake.set()

    async def drain(self) -> None:
        """Wait for every delivery currently scheduled (used by tests)."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    async def tick(self, *, now: int | None = None) -> int:
        current = now_ts() if now is None else now
        self._repos.external_delivery_repo.requeue_expired(current)
        scheduled = 0
        for row in self._repos.external_delivery_repo.list_due(current, limit=20):
            subscription = self._repos.external_subscription_repo.get(row.subscription_id)
            lease = LEASE_SECONDS
            if subscription is not None and subscription.mode == MODE_EXPERT_TURN:
                lease = max(lease, EXPERT_TURN_LEASE_SECONDS)
                if subscription.coalesce_seconds:
                    lease = max(
                        lease,
                        subscription.coalesce_seconds + EXPERT_COALESCE_GRACE_SECONDS,
                    )
            claimed = self._repos.external_delivery_repo.claim(
                row.delivery_id, now=current, lease_seconds=lease
            )
            if claimed is None:
                continue
            task = asyncio.create_task(
                self._run(claimed), name=f"external-delivery-{claimed.delivery_id}"
            )
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
            scheduled += 1
        if current - self._last_poll_scan >= POLL_SCAN_SECONDS:
            self._last_poll_scan = current
            await self.poll_tick(now=current)
        if current - self._last_cleanup_at >= CLEANUP_INTERVAL_SECONDS:
            self._last_cleanup_at = current
            self.cleanup(now=current)
        return scheduled

    async def poll_tick(self, *, now: int | None = None) -> int:
        """Claim and run due connector_poll sources; returns how many started."""
        current = now_ts() if now is None else now
        if self._event_service is None or self._connector_runner is None:
            return 0
        scheduled = 0
        for source in self._repos.external_source_repo.list_due_polls(current, limit=10):
            if not self._repos.external_source_repo.claim_poll(
                source.source_id, now=current, lease_seconds=POLL_LEASE_SECONDS
            ):
                continue
            task = asyncio.create_task(
                self._run_poll(source, current),
                name=f"external-poll-{source.source_id}",
            )
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
            scheduled += 1
        return scheduled

    async def _run_poll(self, source: ExternalSourceRow, now: int) -> None:
        service = self._event_service
        runner = self._connector_runner
        if service is None or runner is None:
            return
        try:
            result = await asyncio.to_thread(
                poll_source,
                service=service,
                source=source,
                runner=runner,
                now=now,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - surface as source last_error
            interval = _poll_interval(source)
            delay = min(interval * (2 ** min(source.fail_count, 6)), 3600)
            self._repos.external_source_repo.record_poll_error(
                source.source_id,
                error=str(exc),
                next_run_at=now + max(30, delay),
            )
            from octop.infra.metrics import METRICS  # noqa: PLC0415

            METRICS.inc("external_poll_error_total")
            logger.warning("connector poll %s failed: %s", source.source_id, exc)
            return
        cursor_json = (
            json.dumps({"cursor": result.cursor}, ensure_ascii=False)
            if result.cursor is not None
            else source.cursor_json
        )
        self._repos.external_source_repo.record_poll_success(
            source.source_id,
            cursor_json=cursor_json,
            next_run_at=now + _poll_interval(source),
        )
        from octop.infra.metrics import METRICS  # noqa: PLC0415

        METRICS.inc("external_poll_total")
        if result.events:
            self.wake()

    def cleanup(self, *, now: int | None = None) -> int:
        current = now_ts() if now is None else now
        return self._repos.external_event_repo.delete_older_than(current - RETENTION_SECONDS)

    async def _loop(self) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("external event worker tick failed")
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self._interval)
            except TimeoutError:
                pass
            else:
                self._wake.clear()

    async def _run(self, delivery: ExternalDeliveryRow) -> None:
        event = self._repos.external_event_repo.get(delivery.event_id)
        subscription = self._repos.external_subscription_repo.get(delivery.subscription_id)
        attempt = delivery.attempt + 1
        if event is None or subscription is None:
            self._repos.external_delivery_repo.finish(
                delivery.delivery_id,
                status="dead",
                attempt=attempt,
                last_error="event or subscription no longer exists",
            )
            return
        if not subscription.enabled:
            self._repos.external_delivery_repo.finish(
                delivery.delivery_id,
                status="dead",
                attempt=attempt,
                last_error="subscription disabled",
            )
            return
        if subscription.mode == MODE_EXPERT_TURN and subscription.coalesce_seconds > 0:
            self._queue_expert(delivery, subscription.coalesce_seconds)
            return
        try:
            await self._dispatch(delivery, event, subscription, attempt)
        except DeliveryError as exc:
            self._fail(delivery, attempt, str(exc), retryable=exc.retryable)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - delivery failures are data, not crashes
            logger.warning(
                "external delivery %s failed: %s", delivery.delivery_id, exc, exc_info=True
            )
            self._fail(delivery, attempt, str(exc), retryable=True)
        else:
            self._repos.external_delivery_repo.finish(
                delivery.delivery_id, status="delivered", attempt=attempt
            )
            from octop.infra.metrics import METRICS  # noqa: PLC0415

            METRICS.inc("external_delivery_delivered_total")

    def _fail(
        self, delivery: ExternalDeliveryRow, attempt: int, error: str, *, retryable: bool
    ) -> None:
        from octop.infra.metrics import METRICS  # noqa: PLC0415

        METRICS.inc("external_delivery_failed_total")
        if retryable and attempt < MAX_ATTEMPTS:
            delay = RETRY_DELAYS[min(attempt - 1, len(RETRY_DELAYS) - 1)]
            self._repos.external_delivery_repo.finish(
                delivery.delivery_id,
                status="failed",
                attempt=attempt,
                last_error=error,
                next_retry_at=int(time.time()) + delay,
            )
            return
        self._repos.external_delivery_repo.finish(
            delivery.delivery_id,
            status="dead",
            attempt=attempt,
            last_error=error,
        )

    async def _dispatch(
        self,
        delivery: ExternalDeliveryRow,
        event: ExternalEventRow,
        subscription: ExternalSubscriptionRow,
        attempt: int,
    ) -> None:
        if delivery.target_type == "expert":
            async with self._expert_sem(subscription.target_id):
                await self._deliver_expert(event, subscription, attempt)
            return
        if delivery.target_type == "team":
            async with self._team_sem:
                await self._deliver_team(event, subscription)
            return
        if delivery.target_type == "app":
            async with self._app_sem:
                await self._deliver_app(event, subscription)
            return
        raise DeliveryError(f"unsupported target type: {delivery.target_type!r}", retryable=False)

    def _expert_sem(self, agent_id: str) -> asyncio.Semaphore:
        sem = self._expert_sems.get(agent_id)
        if sem is None:
            sem = asyncio.Semaphore(1)
            self._expert_sems[agent_id] = sem
        return sem

    async def _deliver_expert(
        self,
        event: ExternalEventRow,
        subscription: ExternalSubscriptionRow,
        attempt: int,
    ) -> None:
        callback = self._expert_delivery
        if callback is None:
            raise DeliveryError("expert delivery service is unavailable", retryable=True)
        source = self._repos.external_source_repo.get(event.source_id)
        if source is None:
            raise DeliveryError("event source no longer exists", retryable=False)
        await callback(event, subscription, source, attempt)

    def _queue_expert(self, delivery: ExternalDeliveryRow, window_seconds: int) -> None:
        subscription_id = delivery.subscription_id
        self._expert_batches.setdefault(subscription_id, []).append(delivery)
        if subscription_id in self._expert_flush_tasks:
            return
        task = asyncio.create_task(
            self._flush_expert(subscription_id, window_seconds),
            name=f"external-expert-batch-{subscription_id}",
        )
        self._expert_flush_tasks[subscription_id] = task
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _flush_expert(self, subscription_id: str, window_seconds: int) -> None:
        try:
            await asyncio.sleep(window_seconds)
            deliveries = self._expert_batches.pop(subscription_id, [])
            if not deliveries:
                return
            subscription = self._repos.external_subscription_repo.get(subscription_id)
            pairs: list[tuple[ExternalDeliveryRow, ExternalEventRow]] = []
            for delivery in deliveries:
                if not subscription:
                    self._repos.external_delivery_repo.finish(
                        delivery.delivery_id,
                        status="dead",
                        attempt=delivery.attempt + 1,
                        last_error="subscription no longer exists",
                    )
                    continue
                event = self._repos.external_event_repo.get(delivery.event_id)
                if event is None:
                    self._repos.external_delivery_repo.finish(
                        delivery.delivery_id,
                        status="dead",
                        attempt=delivery.attempt + 1,
                        last_error="event no longer exists",
                    )
                    continue
                pairs.append((delivery, event))
            if not pairs or subscription is None:
                return
            if not subscription.enabled or subscription.mode != MODE_EXPERT_TURN:
                for delivery, _event in pairs:
                    self._repos.external_delivery_repo.finish(
                        delivery.delivery_id,
                        status="dead",
                        attempt=delivery.attempt + 1,
                        last_error="subscription disabled or changed mode",
                    )
                return
            summary = "\n".join(f"- {event.summary}" for _delivery, event in pairs)[:2000]
            synthetic = dataclasses.replace(pairs[0][1], summary=summary)
            attempt = max(delivery.attempt for delivery, _event in pairs) + 1
            try:
                async with self._expert_sem(subscription.target_id):
                    await self._deliver_expert(synthetic, subscription, attempt)
            except DeliveryError as exc:
                for delivery, _event in pairs:
                    self._fail(delivery, delivery.attempt + 1, str(exc), retryable=exc.retryable)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "coalesced expert delivery %s failed: %s",
                    subscription_id,
                    exc,
                    exc_info=True,
                )
                for delivery, _event in pairs:
                    self._fail(delivery, delivery.attempt + 1, str(exc), retryable=True)
            else:
                from octop.infra.metrics import METRICS  # noqa: PLC0415

                for delivery, _event in pairs:
                    self._repos.external_delivery_repo.finish(
                        delivery.delivery_id,
                        status="delivered",
                        attempt=delivery.attempt + 1,
                    )
                    METRICS.inc("external_delivery_delivered_total")
        finally:
            self._expert_flush_tasks.pop(subscription_id, None)
            if self._task is not None and self._expert_batches.get(subscription_id):
                task = asyncio.create_task(
                    self._flush_expert(subscription_id, window_seconds),
                    name=f"external-expert-batch-{subscription_id}",
                )
                self._expert_flush_tasks[subscription_id] = task
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)

    async def _deliver_team(
        self, event: ExternalEventRow, subscription: ExternalSubscriptionRow
    ) -> None:
        coordinator = self._coordinator
        source = self._repos.external_source_repo.get(event.source_id)
        team = self._repos.agent_repo.get(subscription.target_id)
        if source is None or team is None or getattr(team, "kind", "") != "team":
            raise DeliveryError("team target no longer exists", retryable=False)
        owner = self._repos.user_repo.get(source.owner_user_id)
        is_admin = bool(owner is not None and owner.role == "admin")
        if not is_admin and team.user_id != source.owner_user_id:
            raise DeliveryError("team target is not owned by the source owner", retryable=False)
        if subscription.mode == MODE_TEAM_JUDGE:
            if coordinator is None:
                raise DeliveryError("team coordinator is unavailable", retryable=True)
            if not coordinator.is_autopilot(subscription.target_id):
                raise DeliveryError("team is not in autopilot mode", retryable=False)
            coordinator.record(
                subscription.target_id,
                type="external.event",
                dedupe_key=f"external:{event.event_id}",
                actor=f"external:{event.source_id}",
                payload={
                    "event_id": event.event_id,
                    "source_id": event.source_id,
                    "external_id": event.external_id,
                    "event_type": event.event_type,
                    "summary": event.summary,
                    "subscription_id": subscription.subscription_id,
                },
            )
            return
        if subscription.mode == MODE_TEAM_NOTIFY:
            await self._notify_team(event, subscription)
            return
        raise DeliveryError(f"unsupported team mode: {subscription.mode!r}", retryable=False)

    async def _notify_team(
        self, event: ExternalEventRow, subscription: ExternalSubscriptionRow
    ) -> None:
        if self._resolve_team_target is None or self._push_user is None:
            return
        team_id = subscription.target_id
        target = await self._resolve_team_target(team_id)
        if target is None:
            raise DeliveryError("team has no notification target", retryable=False)
        source = self._repos.external_source_repo.get(event.source_id)
        agent = self._repos.agent_repo.get(team_id)
        source_name = source.name if source is not None else event.source_id
        text = (
            render_template(
                subscription.template,
                source=source_name,
                event_type=event.event_type,
                summary=event.summary,
                event_id=event.event_id,
            )
            if subscription.template
            else f"[{source_name}] {event.summary}"
        )
        await self._push_user(
            int(target.user_id),
            {
                "type": "dashboard_push",
                "agent_id": team_id,
                "thread_id": str(target.thread_id),
                "text": text,
                "agent_name": agent.name if agent is not None else team_id,
            },
        )

    async def _deliver_app(
        self, event: ExternalEventRow, subscription: ExternalSubscriptionRow
    ) -> None:
        service = self._app_service
        source = self._repos.external_source_repo.get(event.source_id)
        if service is None or source is None:
            raise DeliveryError("light-app service is unavailable", retryable=True)
        owner = self._repos.user_repo.get(source.owner_user_id)
        is_admin = bool(owner is not None and owner.role == "admin")
        try:
            app = service.get(subscription.target_id)
            permission = service.permission_for(app, source.owner_user_id, is_admin=is_admin)
        except OctopError as exc:
            raise DeliveryError(exc.message, retryable=False) from exc
        if (
            not is_admin
            and app.owner_user_id != source.owner_user_id
            and (
                permission is None
                or PERMISSION_RANK.get(permission, 0) < PERMISSION_RANK[PERMISSION_EDIT_DATA]
            )
        ):
            raise DeliveryError("app target is no longer writable", retryable=False)

        mapping = _json_dict(subscription.mapping_json)
        if subscription.mode == MODE_APP_REFRESH:
            app_source_id = mapping.get("source_id") or mapping.get("app_source_id")
            if not isinstance(app_source_id, str) or not app_source_id:
                raise DeliveryError("app.refresh requires mapping.source_id", retryable=False)
            try:
                await service.run_source(app, app_source_id)
            except OctopError as exc:
                raise DeliveryError(exc.message, retryable=True) from exc
            return
        if subscription.mode == MODE_APP_IMPORT:
            records = _records(event.payload, mapping.get("records_path"))
            if records is None:
                raise DeliveryError("payload has no records list", retryable=False)
            table = str(mapping.get("table") or "main")
            raw_columns = mapping.get("columns")
            columns = raw_columns if isinstance(raw_columns, dict) else None
            try:
                await service.import_rows(
                    app,
                    records,
                    table=table,
                    mode="replace",
                    mapping=columns,
                )
            except OctopError as exc:
                raise DeliveryError(exc.message, retryable=False) from exc
            return
        raise DeliveryError(f"unsupported app mode: {subscription.mode!r}", retryable=False)


def _poll_interval(source: ExternalSourceRow) -> int:
    try:
        return parse_poll_config(json.loads(source.config_json or "{}")).interval_seconds
    except Exception:
        return 300
