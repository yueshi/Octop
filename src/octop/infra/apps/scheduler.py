"""Auto-sync scheduler for scheduled light-app data sources.

One periodic asyncio task scans ``light_app_sources`` for scheduled bindings
whose ``next_run_at`` is due, claims each atomically (the claim itself is the
lease — a conditional UPDATE pushes ``next_run_at`` forward), and runs the
import through :class:`AppService`. Failures are recorded by the service
(``last_status='error'`` + exponential backoff); this loop only logs them.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import TYPE_CHECKING

from octop.infra.apps.service import AppService
from octop.infra.db.repos.apps import LightAppRepo

if TYPE_CHECKING:
    from octop.config import OctopConfig
    from octop.infra.db.services import SharedServices

logger = logging.getLogger(__name__)

SCAN_SECONDS = 30
"""How often the loop looks for due sources (smallest usable interval anyway)."""


class AppSourceScheduler:
    def __init__(
        self,
        *,
        repo: LightAppRepo,
        service: AppService,
        scan_seconds: int = SCAN_SECONDS,
    ) -> None:
        self._repo = repo
        self._service = service
        self._scan_seconds = scan_seconds
        self._task: asyncio.Task[None] | None = None

    def replace_persistence(self, *, repo: LightAppRepo, service: AppService) -> None:
        """Retarget onto a new repo/service (setup wizard DB hot-swap)."""
        self._repo = repo
        self._service = service

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop(), name="light-app-source-sync")

    async def shutdown(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _loop(self) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("light app source sync tick failed")
            await asyncio.sleep(self._scan_seconds)

    async def tick(self, *, now: int | None = None) -> int:
        """Run every due source once; returns how many sources ran."""
        now = int(time.time()) if now is None else now
        ran = 0
        for item in self._repo.list_due_sources(now):
            if not self._repo.claim_source(
                item.source_id, now=now, schedule_seconds=item.schedule_seconds
            ):
                continue  # claimed elsewhere
            try:
                row = self._service.get(item.app_id)
                await self._service.run_source(row, item.source_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                # Outcome already recorded by run_source (last_status/backoff).
                logger.warning(
                    "light app source %s (%s/%s) failed to sync",
                    item.source_id,
                    item.app_id,
                    item.table_key,
                    exc_info=True,
                )
            ran += 1
        return ran


def build_source_service(
    *,
    services: SharedServices,
    workspaces: object,
    config: OctopConfig,
) -> AppService:
    """AppService wired for background sync (external runner included)."""
    from octop.infra.apps.sources import GatewayToolRunner  # noqa: PLC0415
    from octop.infra.connectors.service import ConnectorService  # noqa: PLC0415

    connector_repo = services.repos.connector_repo
    return AppService(
        app_repo=services.light_app_repo,
        agent_repo=services.agent_repo,
        user_repo=services.user_repo,
        workspaces=workspaces,  # type: ignore[arg-type]
        external_tool_runner=GatewayToolRunner(
            repo=connector_repo,
            service=ConnectorService(
                repo=connector_repo,
                secret_repo=services.secret_repo,
                settings_repo=services.settings_repo,
                config=config,
            ),
        ),
    )


def build_scheduler(
    *,
    services: SharedServices,
    workspaces: object,
    config: OctopConfig,
) -> AppSourceScheduler:
    """Build (not start) the auto-sync scheduler against the server's repos."""
    repo = services.light_app_repo
    return AppSourceScheduler(
        repo=repo,
        service=build_source_service(services=services, workspaces=workspaces, config=config),
    )
