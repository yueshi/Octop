"""Gateway — global AI interaction entry point."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from octop_gateway.channel import ChannelCredentialsError
from octop_gateway.channels import ChannelKind
from octop_gateway.manager import ChannelManager
from octop_gateway.models import ChannelSubject

from octop.i18n import channel_probe_incomplete, channel_runtime_reason, tr
from octop.infra.db.repos.channels import ChannelRow
from octop.infra.db.repos.sessions import SessionRow
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.gateway.cli import CLI_CHANNEL_ID, CliChannel, CliHub
from octop.infra.gateway.history_backfill import HistoryBackfillQueue
from octop.infra.gateway.process import media_backend_for_agent
from octop.infra.gateway.process.processor import GlobalProcessor
from octop.infra.gateway.process.response_mode import (
    ChannelResponseMode,
    normalize_channel_response_mode,
    processor_for_response_mode,
    qq_channel_response_mode,
    wrap_weixin_stream_processor,
)
from octop.infra.gateway.slash.dispatcher import SlashDispatcher, build_default_dispatcher
from octop.infra.gateway.steer import SteerCoordinator
from octop.infra.gateway.threads import ThreadRegistry
from octop.infra.gateway.ws import (
    WS_CHANNEL_ID,
    WebSocketChannel,
    WebSocketHub,
)
from octop.infra.utils.locale import DEFAULT_LOCALE, Locale

if TYPE_CHECKING:
    from octop.infra.agents.manager import AgentManager
    from octop.infra.db.services import RepoBundle

logger = logging.getLogger(__name__)

# Re-export for api/cli importers
__all__ = [
    "ChannelCreateSpec",
    "ChannelKind",
    "ChannelRuntimeStatus",
    "Gateway",
    "SlashRuntimeMeta",
]


@dataclass(frozen=True)
class SlashRuntimeMeta:
    version: str
    started_at: int


@dataclass(frozen=True)
class ChannelRuntimeStatus:
    """Live connection state for a channel.

    ``reason`` is a locale-neutral code (``disabled`` / ``unregistered`` /
    ``error``) localized at serialization time; ``detail`` carries free-form
    diagnostics (e.g. an exception message) shown alongside the reason.
    """

    connected: bool
    reason: str | None = None
    detail: str | None = None
    updated_at: int = 0


@dataclass
class ChannelCreateSpec:
    channel_id: str
    agent_id: str
    user_id: int
    kind: ChannelKind | str
    name: str
    config: dict[str, Any] = field(default_factory=dict)


async def _probe_processor(_msg: Any) -> Any:
    """Stub processor for ephemeral channel probe instances."""
    if False:  # pragma: no cover - makes this an async generator
        yield None


class Gateway:
    """Global AI interaction entry point.

    Owns the octop-gateway ChannelManager. Routes IM messages
    by ``InboundMessage.tenant_id`` (== agent ULID) via GlobalProcessor.
    """

    def __init__(
        self,
        *,
        agent_manager: AgentManager,
        repos: RepoBundle,
        trajectory_service: Any | None = None,
        history_archive: Any | None = None,
    ) -> None:
        self._agent_manager = agent_manager
        self._repos = repos
        self._trajectory_service = trajectory_service
        self._history_archive = history_archive
        self._thread_registry = ThreadRegistry(
            session_repo=repos.session_repo,
            thread_repo=repos.thread_repo,
        )
        self._channel_manager: ChannelManager | None = None
        self._processor: GlobalProcessor | None = None
        self._dispatcher = build_default_dispatcher()
        self._slash_meta: SlashRuntimeMeta | None = None
        self._ws_hub = WebSocketHub()
        # Bridge turn ids explicitly cancelled while queued. The processor
        # consumes one at turn start; entries auto-expire so an active-turn
        # cancel cannot leak into a later turn.
        self._cancelled_bridge_turns: dict[str, float] = {}
        self._cli_hub = CliHub()
        self._steer = SteerCoordinator()
        self._ws_channel: WebSocketChannel | None = None
        self._cli_channel: CliChannel | None = None
        self._runtime_status: dict[str, ChannelRuntimeStatus] = {}
        self._history_backfill = HistoryBackfillQueue()
        self._team_coordinator: Any | None = None
        self._team_autopilot: Any | None = None
        self._team_dispatcher: Any | None = None

    def replace_repos(self, repos: RepoBundle) -> None:
        """Point channel/thread persistence at a rebound control-plane pool."""
        self._repos = repos
        self._thread_registry.replace_repos(
            session_repo=repos.session_repo,
            thread_repo=repos.thread_repo,
        )
        if self._processor is not None:
            self._processor.replace_thread_message_repo(repos.thread_message_repo)
            self._processor.hitl_coordinator.session_policies.replace_repo(repos.thread_repo)
        if self._team_coordinator is not None:
            self._team_coordinator.replace_repos(repos)
        if self._team_dispatcher is not None:
            self._team_dispatcher.replace_repos(repos)
        if self._team_autopilot is not None:
            self._team_autopilot.replace_repos(repos)

    @property
    def ws_hub(self) -> WebSocketHub:
        return self._ws_hub

    @property
    def team_coordinator(self) -> Any | None:
        return self._team_coordinator

    @property
    def team_autopilot(self) -> Any | None:
        return self._team_autopilot

    def push_team_frame(self, team_id: str, frame: dict[str, Any]) -> None:
        """Best-effort sync hook for the team coordinator/board tools."""
        row = self._repos.agent_repo.get(team_id)
        if row is None or row.user_id is None:
            return
        loop = asyncio.get_running_loop()
        loop.create_task(self._ws_hub.push_to_user(int(row.user_id), frame))

    @property
    def steer(self) -> SteerCoordinator:
        return self._steer

    @property
    def cli_hub(self) -> CliHub:
        return self._cli_hub

    @property
    def cli_channel_id(self) -> str:
        return CLI_CHANNEL_ID

    @property
    def channel_manager(self) -> ChannelManager | None:
        return self._channel_manager

    @property
    def dashboard_channel_id(self) -> str:
        return WS_CHANNEL_ID

    @property
    def slash_dispatcher(self) -> SlashDispatcher:
        return self._dispatcher

    @property
    def processor(self) -> GlobalProcessor:
        if self._processor is None:
            raise RuntimeError("gateway not booted")
        return self._processor

    @property
    def history_backfill(self) -> HistoryBackfillQueue:
        return self._history_backfill

    @property
    def slash_meta(self) -> SlashRuntimeMeta | None:
        return self._slash_meta

    def set_slash_meta(self, *, version: str, started_at: int) -> None:
        self._slash_meta = SlashRuntimeMeta(version=version, started_at=started_at)

    @property
    def thread_registry(self) -> ThreadRegistry:
        return self._thread_registry

    def get_runtime_status(self, channel_id: str) -> ChannelRuntimeStatus | None:
        status = self._runtime_status.get(channel_id)
        if status is None or status.reason in ("disabled", "unregistered"):
            return status
        channel = self._channel_manager.get_channel(channel_id) if self._channel_manager else None
        connected = getattr(channel, "is_connected", None)
        if isinstance(connected, bool):
            detail = getattr(channel, "runtime_error", None)
            detail = detail if isinstance(detail, str) else None
            if connected != status.connected or detail != status.detail:
                self._set_runtime_status(
                    channel_id,
                    connected=connected,
                    reason=None if connected else "error",
                    detail=detail,
                )
        return self._runtime_status.get(channel_id)

    def runtime_status_to_dict(
        self, channel_id: str, *, locale: Locale = DEFAULT_LOCALE
    ) -> dict[str, Any] | None:
        status = self.get_runtime_status(channel_id)
        if status is None:
            return None
        error: str | None = None
        if status.reason is not None:
            error = channel_runtime_reason(status.reason, locale)
            if status.detail:
                detail = self._format_probe_error(RuntimeError(status.detail), locale)
                error = f"{error}: {detail}"
        return {
            "connected": status.connected,
            "error": error,
            "updated_at": status.updated_at,
        }

    async def boot(self) -> None:
        from octop.infra.gateway.process.channel_thinking import (
            install_channel_thinking_clean,
        )

        install_channel_thinking_clean()
        from octop.infra.agents.teams.coordinator import TeamCoordinator  # noqa: PLC0415
        from octop.infra.agents.teams.decision import (  # noqa: PLC0415
            DecisionRunner,
            provider_plan_fn,
        )
        from octop.infra.agents.teams.dispatcher import TeamDispatcher  # noqa: PLC0415
        from octop.infra.agents.teams.runtime import TeamAutopilot  # noqa: PLC0415

        def shadow_gate(team_id: str) -> bool:
            """Opt-in per team: config key ``team_shadow_decisions`` (phase 2).

            Decisions burn tokens on the team's own model, so they only run
            when the team config asks for them.
            """
            try:
                return bool(self._agent_manager.get_config(team_id).get("team_shadow_decisions"))
            except Exception:
                return False

        async def team_notify(team_id: str, text: str) -> None:
            """Deliver a decision notice to the team's current room."""
            target = await autopilot.resolve_target(team_id)
            if target is None:
                return
            session = self._thread_registry.get_session(target.session_key)
            if session is not None:
                await self.push_session_text(session, text, title_source=text)

        def team_frame(team_id: str, frame: dict[str, Any]) -> None:
            """Best-effort dashboard frame for runtime/task/wait/decision changes."""
            self.push_team_frame(team_id, frame)

        autopilot = TeamAutopilot(
            repos=self._repos,
            agent_manager=self._agent_manager,
            thread_registry=self._thread_registry,
            notify=team_notify,
            frame_notify=team_frame,
        )
        dispatcher = TeamDispatcher(
            repos=self._repos,
            resolve_target=autopilot.resolve_target,
            submit_peer=autopilot.submit_peer,
        )
        from octop.infra.agents.teams.service import is_team_agent  # noqa: PLC0415

        coordinator = TeamCoordinator(
            event_repo=self._repos.team_event_repo,
            runtime_repo=self._repos.team_runtime_repo,
            task_repo=self._repos.team_task_repo,
            wait_repo=self._repos.team_wait_repo,
            dispatcher=dispatcher,
            shadow_gate=shadow_gate,
            apply_plans=True,
            frame_notify=team_frame,
            # A digital employee decides inside its own turn; only multi-agent
            # teams run a separate decision pass.
            solo_gate=lambda team_id: not is_team_agent(self._repos.agent_repo.get(team_id)),
        )
        decision_runner = DecisionRunner(
            repos=self._repos,
            plan_fn=provider_plan_fn(agent_manager=self._agent_manager, repos=self._repos),
            event_recorder=coordinator,
            notify=autopilot.notify,
            member_ids_fn=lambda team_id: set(self._agent_manager.teams.member_ids(team_id)),
        )
        coordinator.bind_decision_runner(decision_runner)
        dispatcher.bind_recorder(coordinator)
        autopilot.bind_coordinator(coordinator)
        self._team_coordinator = coordinator
        self._team_autopilot = autopilot
        self._team_dispatcher = dispatcher

        self._processor = GlobalProcessor(
            agent_manager=self._agent_manager,
            thread_registry=self._thread_registry,
            audit_repo=self._repos.audit_repo,
            agent_repo=self._repos.agent_repo,
            user_repo=self._repos.user_repo,
            connector_repo=self._repos.connector_repo,
            knowledge_repo=self._repos.knowledge_repo,
            settings_repo=self._repos.settings_repo,
            provider_repo=self._repos.provider_repo,
            dispatcher=self._dispatcher,
            usage_repo=self._repos.usage_repo,
            runtime_repo=self._repos.team_runtime_repo,
            thread_message_repo=self._repos.thread_message_repo,
            gateway=self,
            hitl_repo=self._repos.hitl_repo,
            trajectory_service=self._trajectory_service,
            history_archive=self._history_archive,
            team_coordinator=coordinator,
        )
        hitl_store = getattr(self._processor.hitl_coordinator, "store", None)
        if hitl_store is not None:
            coordinator.bind_approval_expire(hitl_store.expire_for_task)

        self._channel_manager = ChannelManager(channels={})
        self._channel_manager.set_pre_lock_handler(self._preempt_cancel_on_stop)
        await self._channel_manager.start()

        self._ws_channel = WebSocketChannel(
            self._processor,
            hub=self._ws_hub,
            steer=self._steer,
            channel_id=WS_CHANNEL_ID,
        )
        await self._channel_manager.add_channel(self._ws_channel)

        self._cli_channel = CliChannel(
            self._processor,
            hub=self._cli_hub,
            channel_id=CLI_CHANNEL_ID,
        )
        await self._channel_manager.add_channel(self._cli_channel)

        rows = self._repos.channel_repo.list_all(include_disabled=False)
        if rows:
            await asyncio.gather(*(self._safe_register_channel(row) for row in rows))

        logger.info("Gateway booted")

    async def refresh_media_backends(self) -> None:
        """Re-set media backends on all registered channels.

        Called after agents finish booting (gateway boots before agents) to
        resolve the startup ordering gap.
        """
        if not self._channel_manager:
            return
        rows = self._repos.channel_repo.list_all(include_disabled=False)
        for row in rows:
            channel = self._channel_manager.get_channel(row.channel_id)
            if channel is None:
                continue
            backend = media_backend_for_agent(self._agent_manager, row.agent_id)
            if backend is not None:
                channel.set_media_backend(backend)

    async def reload_plugin_channels(self, apply: Callable[[], None], *, kinds: set[str]) -> None:
        """Stop plugin channels before replacing factories; preserve DB rows."""
        rows = [
            row
            for row in self._repos.channel_repo.list_all(include_disabled=False)
            if row.kind in kinds
        ]
        if self._channel_manager is not None:
            for row in rows:
                await self._unregister(row.channel_id)
        apply()
        if self._channel_manager is not None:
            for row in rows:
                await self._safe_register_channel(row)

    async def reload_channels_from_db(self) -> None:
        """Drop and re-register enabled IM channels from the DB.

        Used after backup restore so Gateway matches the replaced ``channels``
        table without a process restart. Built-in dashboard/cli channels stay
        registered. Call after agents have been rehydrated so media backends
        can resolve.
        """
        if not self._channel_manager or not self._processor:
            return

        builtin = {WS_CHANNEL_ID, CLI_CHANNEL_ID}
        live_ids = [cid for cid in self._channel_manager.channel_ids if cid not in builtin]
        for channel_id in live_ids:
            await self._unregister(channel_id)

        for channel_id in list(self._runtime_status):
            if channel_id not in builtin:
                self._runtime_status.pop(channel_id, None)

        rows = self._repos.channel_repo.list_all(include_disabled=False)
        if rows:
            await asyncio.gather(*(self._safe_register_channel(row) for row in rows))
        await self.refresh_media_backends()
        logger.info("Gateway channels reloaded from DB (%d enabled)", len(rows))

    async def shutdown(self) -> None:
        await self._history_backfill.close()
        if self._channel_manager:
            await self._channel_manager.stop()
        self._channel_manager = None
        self._processor = None
        self._ws_channel = None
        self._cli_channel = None
        self._runtime_status.clear()

    def list_channels(self, agent_id: str) -> list[ChannelRow]:
        return self._repos.channel_repo.list_by_agent(agent_id)

    def get_channel(self, channel_id: str) -> ChannelRow | None:
        return self._repos.channel_repo.get(channel_id)

    async def create_channel(self, spec: ChannelCreateSpec) -> ChannelRow:
        config_json = json.dumps(spec.config)
        existing = self._repos.channel_repo.get_by_agent_and_name(spec.agent_id, spec.name)
        if existing is not None:
            if existing.kind != str(spec.kind):
                raise OctopError(
                    ErrorCode.CHANNEL_NAME_TAKEN,
                    f"channel name {spec.name!r} already in use by kind {existing.kind!r}",
                )
            row = await self.update_channel(
                existing.channel_id,
                kind=str(spec.kind),
                name=spec.name,
                config_json=config_json,
                enabled=1,
            )
            assert row is not None
            return row

        self._repos.channel_repo.create(
            channel_id=spec.channel_id,
            agent_id=spec.agent_id,
            user_id=spec.user_id,
            kind=str(spec.kind),
            name=spec.name,
            config_json=config_json,
        )
        row = self._repos.channel_repo.get(spec.channel_id)
        assert row is not None
        if row.enabled:
            await self._safe_register_channel(row)
        else:
            self._set_runtime_status(row.channel_id, connected=False, reason="disabled")
        return row

    async def update_channel(
        self,
        channel_id: str,
        *,
        kind: str | None = None,
        name: str | None = None,
        config_json: str | None = None,
        enabled: int | None = None,
    ) -> ChannelRow | None:
        self._repos.channel_repo.update(
            channel_id,
            kind=kind,
            name=name,
            config_json=config_json,
            enabled=bool(enabled) if enabled is not None else None,
        )
        row = self._repos.channel_repo.get(channel_id)
        await self._unregister(channel_id)
        if row and row.enabled:
            await self._safe_register_channel(row)
        elif row is not None:
            self._set_runtime_status(channel_id, connected=False, reason="disabled")
        return row

    async def delete_channel(self, channel_id: str) -> None:
        self._repos.channel_repo.delete(channel_id)
        await self._unregister(channel_id)
        self._runtime_status.pop(channel_id, None)

    def require_session(self, agent_id: str, session_key: str) -> SessionRow:
        """Return a session after validating its owning agent."""
        session = self._thread_registry.get_session(session_key)
        if session is None:
            raise ValueError(f"session {session_key!r} not found")
        if session.agent_id != agent_id:
            raise ValueError(f"session {session_key!r} does not belong to agent {agent_id!r}")
        return session

    def _bump_virtual_session(self, session: SessionRow, text: str) -> None:
        self._thread_registry.touch_last_active(session.thread_id)
        if text:
            self._thread_registry.set_title_if_null(session.thread_id, text)
        self._thread_registry.increment_unread(session.session_key)

    async def run_in_session(
        self,
        agent_id: str,
        session_key: str,
        operation: Callable[[], Awaitable[None]],
    ) -> None:
        """Serialize a proactive operation with inbound turns for its session."""
        session = self.require_session(agent_id, session_key)
        manager = self._require_channel_manager()
        channel_id = self._channel_id_for_session(session)
        await manager.run_in_session(channel_id, session_key, operation)

    async def push_text_from_session(
        self,
        agent_id: str,
        session_key: str,
        text: str,
    ) -> None:
        """Push complete text without running the agent."""

        async def _locked() -> None:
            session = self.require_session(agent_id, session_key)
            await self.push_session_text(session, text, title_source=text)
            if session.channel_type == ThreadRegistry.CHANNEL_DASHBOARD:
                await self.notify_dashboard_push(session, agent_id, text)

        await self.run_in_session(agent_id, session_key, _locked)

    async def push_session_text(
        self,
        session: SessionRow,
        text: str,
        *,
        title_source: str | None = None,
    ) -> None:
        """Deliver one complete proactive message to a resolved session."""
        channel_id = self._channel_id_for_session(session)
        try:
            await self.push_text(
                session.channel_type,
                channel_id,
                self._resolve_push_subject(session),
                text,
            )
        finally:
            # Title the conversation even when the channel push fails. Otherwise
            # the thread stays "New Chat" until the user sends a follow-up.
            if ThreadRegistry.is_virtual_channel(session.channel_type):
                self._bump_virtual_session(session, title_source or text)

    @staticmethod
    def _channel_id_for_session(session: SessionRow) -> str:
        if session.channel_type == ThreadRegistry.CHANNEL_DASHBOARD:
            return WS_CHANNEL_ID
        if session.channel_type == ThreadRegistry.CHANNEL_CLI:
            return CLI_CHANNEL_ID
        if not session.channel_id:
            raise ValueError(
                f"session is IM ({session.channel_type!r}) but has no channel_id bound"
            )
        return session.channel_id

    def _resolve_push_subject(self, session: SessionRow) -> ChannelSubject:
        """Build ChannelSubject from session; IM routing enrichment is in octop-gateway."""
        subject = session.to_channel_subject()
        if not ThreadRegistry.is_virtual_channel(session.channel_type):
            return subject
        metadata = dict(subject.metadata or {})
        metadata["thread_id"] = session.thread_id
        return ChannelSubject(
            subject_id=subject.subject_id,
            first_seen=subject.first_seen,
            last_seen=subject.last_seen,
            display_name=subject.display_name,
            chat_type=subject.chat_type,
            metadata=metadata,
        )

    async def notify_dashboard_push(
        self,
        session: SessionRow,
        agent_id: str,
        text: str,
    ) -> None:
        """Fan out a toast payload to the owner's dashboard notification sockets."""
        if not text.strip():
            return
        row = self._agent_manager.get_row(agent_id)
        name = getattr(row, "name", None) if row is not None else None
        agent_name = str(name).strip() if isinstance(name, str) and name.strip() else agent_id
        await self._ws_hub.push_to_user(
            session.user_id,
            {
                "type": "dashboard_push",
                "agent_id": agent_id,
                "thread_id": session.thread_id,
                "text": text,
                "agent_name": agent_name,
            },
        )

    async def notify_hitl_pending(self, user_id: int, frame: dict[str, Any]) -> None:
        """Push a HITL pause (§7.4-2 ``approval_required``) to one user's sockets."""
        await self._ws_hub.push_to_user(user_id, frame)

    async def publish_thread_updated(
        self,
        *,
        user_id: int,
        agent_id: str,
        thread_id: str,
        turn_id: str,
        active: bool,
    ) -> None:
        """Tell dashboard clients that a thread's turn state or history changed.

        This is transport-independent: IM turns call it too, so the sidebar,
        unread badge and open history do not depend on a Dashboard-originated
        WebSocket turn.
        """
        row = self._thread_registry.get_thread(thread_id)
        if row is None:
            return
        if active:
            self._ws_hub.mark_turn_active(thread_id)
        else:
            self._ws_hub.mark_turn_idle(thread_id)
        unread = 0
        try:
            counts = self._repos.session_repo.unread_counts_for_threads([thread_id])
            unread = int(counts.get(thread_id, 0))
        except Exception:
            logger.debug("unread count lookup failed thread=%s", thread_id, exc_info=True)
        # The chat socket only knows the dashboard turn protocol. IM turns
        # publish the same lifecycle frames so a Web client watching this
        # thread can open/close its streaming state and reload history.
        await self._ws_hub.push_to_thread(
            thread_id,
            {
                "type": "turn.started" if active else "turn.ended",
                "turn_id": turn_id,
                "agent_id": agent_id,
                "thread_id": thread_id,
                "source": "im",
            },
        )
        await self._ws_hub.push_to_user(
            user_id,
            {
                "type": "thread.updated",
                "agent_id": agent_id,
                "thread_id": thread_id,
                "channel_type": row.channel_type,
                "turn_active": active,
                "unread_count": unread,
                "title": row.title,
                "last_active": row.last_active,
            },
        )

    def enqueue_im_turn(self, channel_id: str, message: Any) -> bool:
        """Route a Web-originated turn through an IM channel's worker queue."""
        manager = self._require_channel_manager()
        if manager.get_channel(channel_id) is None:
            return False
        manager.enqueue(channel_id, message)
        return True

    def cancel_im_bridge_turn(self, turn_id: str) -> None:
        """Mark a Web → IM bridge turn cancelled before/while it runs."""
        cleaned = turn_id.strip()
        if not cleaned:
            return
        now = time.monotonic()
        for key, ts in list(self._cancelled_bridge_turns.items()):
            if now - ts > 3600:
                self._cancelled_bridge_turns.pop(key, None)
        self._cancelled_bridge_turns[cleaned] = now

    def consume_im_bridge_cancel(self, turn_id: str) -> bool:
        """True once when this bridge turn was cancelled before it started."""
        cleaned = turn_id.strip()
        if not cleaned:
            return False
        return self._cancelled_bridge_turns.pop(cleaned, None) is not None

    def clear_im_bridge_cancel(self, turn_id: str) -> None:
        self._cancelled_bridge_turns.pop(turn_id.strip(), None)

    def im_turn_waiting(self, channel_id: str, subject_id: str) -> bool:
        """Best-effort check that another turn owns this IM session lock.

        ``ChannelManager`` serializes per ``channel_id:subject_id``; when that
        lock is held, a freshly enqueued bridge turn will wait in the queue.
        The lock registry is an octop-gateway implementation detail, so this
        returns False when it is unavailable rather than failing the turn.
        """
        manager = self._channel_manager
        if manager is None or not channel_id or not subject_id:
            return False
        locks = getattr(manager, "_session_locks", None)
        if not isinstance(locks, dict):
            return False
        lock = locks.get(f"{channel_id}:{subject_id}")
        return bool(lock is not None and lock.locked())

    def _require_channel_manager(self) -> ChannelManager:
        if self._channel_manager is None:
            raise RuntimeError("gateway not booted")
        return self._channel_manager

    async def _preempt_cancel_on_stop(self, _channel_id: str, message: Any) -> None:
        """Signal harness cancel for ``/stop`` / ``/cancel`` before the session lock.

        Same-session IM turns are serialized by ChannelManager. Without this hook,
        ``/stop`` waits behind the in-flight turn and never interrupts it.
        """
        from octop.infra.gateway.process.message_keys import session_key_from_message
        from octop.infra.gateway.slash.parser import parse_slash

        text = getattr(message, "text", None)
        cmd = parse_slash(text if isinstance(text, str) else None)
        if cmd is None or cmd.name not in ("stop", "cancel"):
            return

        agent_id = getattr(message, "tenant_id", None) or ""
        if not isinstance(agent_id, str) or not agent_id.strip():
            return

        session_key = session_key_from_message(message, agent_id=agent_id.strip())
        thread_id = self._thread_registry.get_bound_thread_id(session_key)
        if not thread_id:
            return

        self._agent_manager.cancel_stream(agent_id.strip(), thread_id)
        logger.info(
            "preempt cancel: agent=%s thread=%s cmd=/%s",
            agent_id,
            thread_id,
            cmd.name,
        )

    async def push_text(
        self,
        channel_type: str,
        channel_id: str,
        subject: ChannelSubject,
        text: str,
    ) -> None:
        """Proactively push text to an IM user via ChannelManager.

        Applies the target channel's ``show_thinking`` flag so embedded
        ``<think>`` / ``<thinking>`` tags never leak past the display switch
        (team wrap-up and other push paths bypass the inbound event loop).
        """
        from octop.infra.utils.llm_text import prepare_channel_text

        manager = self._require_channel_manager()
        channel = manager.get_channel(channel_id)
        # Match ChannelConstraints defaults when the live channel is missing.
        show_thinking = False
        thinking_template = "💭 Thinking: {content}"
        if channel is not None:
            constraints = channel.constraints
            show_thinking = bool(constraints.show_thinking)
            thinking_template = str(
                getattr(constraints, "thinking_template", thinking_template) or thinking_template
            )
        cleaned = prepare_channel_text(
            text,
            show_thinking=show_thinking,
            thinking_template=thinking_template,
        )
        if not cleaned:
            return
        await manager.push_text(channel_id, subject, cleaned)

    async def probe_channel(
        self, channel_id: str, *, locale: Locale = DEFAULT_LOCALE
    ) -> dict[str, Any]:
        """Start/stop an ephemeral channel instance to verify credentials."""
        row = self.get_channel(channel_id)
        if row is None:
            raise OctopError(ErrorCode.NOT_FOUND, "channel not found")
        return await self._probe_row(row, locale=locale)

    async def probe_config(
        self,
        *,
        agent_id: str,
        kind: str,
        config: dict[str, Any],
        locale: Locale = DEFAULT_LOCALE,
    ) -> dict[str, Any]:
        """Probe credentials without persisting a channel row."""
        ts = int(time.time())
        row = ChannelRow(
            id=0,
            channel_id="__probe__",
            agent_id=agent_id,
            user_id=0,
            kind=kind,
            name="probe",
            config_json=json.dumps(config),
            enabled=1,
            created_at=ts,
            updated_at=ts,
        )
        return await self._probe_row(row, locale=locale)

    async def _probe_row(
        self, row: ChannelRow, *, locale: Locale = DEFAULT_LOCALE
    ) -> dict[str, Any]:
        try:
            raw_cfg = json.loads(row.config_json or "{}")
        except json.JSONDecodeError:
            return {"ok": False, "error": tr("errors.CHANNEL_INVALID_CREDENTIALS", locale)}
        if not isinstance(raw_cfg, dict):
            return {"ok": False, "error": tr("errors.CHANNEL_INVALID_CREDENTIALS", locale)}

        manager = self._channel_manager
        if manager is None:
            raise RuntimeError("gateway not booted")

        try:
            await manager.probe_channel(
                row.kind,
                raw_cfg,
                tenant_id=row.agent_id,
                channel_id=row.channel_id,
                processor=_probe_processor,
            )
            return {"ok": True}
        except ChannelCredentialsError as exc:
            return {"ok": False, "error": channel_probe_incomplete(exc.missing, locale)}
        except OctopError as exc:
            return {"ok": False, "error": exc.localized_message(locale)}
        except Exception as exc:
            logger.exception("Channel probe failed for %s", row.channel_id)
            return {"ok": False, "error": self._format_probe_error(exc, locale)}

    @staticmethod
    def _format_probe_error(exc: Exception, locale: Locale) -> str:
        msg = str(exc)
        if msg in {
            "discord_invalid_token",
            "discord_intents_required",
            "discord_connect_timeout",
            "discord_connection_failed",
            "discord_disconnected",
            "discord_proxy_auth_invalid",
        }:
            return tr(f"channel.probe.{msg}", locale)
        lower = msg.lower()
        if "invalid appid or secret" in lower or "100016" in msg:
            return tr("channel.probe.invalid_credentials", locale)
        if "feishu token refresh failed" in lower:
            return tr(
                "channel.probe.feishu_token_failed", locale, detail=msg.split(":", 1)[-1].strip()
            )
        return msg

    def _set_runtime_status(
        self,
        channel_id: str,
        *,
        connected: bool,
        reason: str | None = None,
        detail: str | None = None,
    ) -> None:
        self._runtime_status[channel_id] = ChannelRuntimeStatus(
            connected=connected,
            reason=reason,
            detail=detail,
            updated_at=int(time.time()),
        )

    async def _safe_register_channel(self, row: ChannelRow) -> None:
        try:
            await self._register_channel(row)
        except Exception as exc:
            logger.exception("Failed to register channel %s (%s)", row.channel_id, row.kind)
            self._set_runtime_status(
                row.channel_id, connected=False, reason="error", detail=str(exc)
            )

    async def _register_channel(self, row: ChannelRow) -> None:
        if not self._channel_manager or not self._processor:
            return
        config = self._config_from_row(row)
        from octop.infra.agents.teams.service import is_team_agent  # noqa: PLC0415

        team_host = False
        if self._agent_manager is not None:
            try:
                team_host = is_team_agent(self._agent_manager.get_row(row.agent_id))
            except Exception:
                team_host = False
        if team_host:
            # Team IM UX needs the host dispatch narration before ask_agent;
            # invoke-mode collapse would drop it. Force stream for team hosts.
            response_mode: ChannelResponseMode = "stream"
        elif row.kind == "qq":
            response_mode = qq_channel_response_mode(config)
        else:
            response_mode = normalize_channel_response_mode(config.get("response_mode"))
        processor = processor_for_response_mode(self._processor, response_mode)
        if row.kind == "weixin" and response_mode == "stream":
            processor = wrap_weixin_stream_processor(processor)
        manager = self._require_channel_manager()
        await manager.add_channel(
            row.kind,
            config,
            tenant_id=row.agent_id,
            channel_id=row.channel_id,
            processor=processor,
        )
        registered = manager.get_channel(row.channel_id)
        if registered is not None:
            backend = media_backend_for_agent(self._agent_manager, row.agent_id)
            if backend is not None:
                registered.set_media_backend(backend)
            if row.kind == "feishu" and not team_host:
                # Feishu has no typing indicator implementation in the adapter.
                # An invoke-mode turn would otherwise stay completely silent
                # until the final answer; a single placeholder keeps the user
                # informed without streaming tokens.
                registered.constraints.reply_timeout = 12.0
                registered.constraints.timeout_strategy = "placeholder"
        self._set_runtime_status(row.channel_id, connected=True)
        if row.kind == "weixin" and registered is not None:
            self._listen_for_weixin_context_token(registered, row)

    def _listen_for_weixin_context_token(self, channel: Any, row: ChannelRow) -> None:
        """Persist a refreshed iLink send token onto the matching WeChat session."""
        listener = getattr(channel, "set_context_token_listener", None)
        if listener is None:
            return

        def _persist(_account_id: str, user_id: str, token: str, token_ms: int) -> None:
            self._repos.session_repo.remember_weixin_context_token(
                agent_id=str(channel.tenant_id or row.agent_id),
                subject_id=user_id,
                token=token,
                token_ms=token_ms,
            )

        listener(_persist)

    def _config_from_row(self, row: ChannelRow) -> dict[str, Any]:
        """Parse the stored JSON config; alias/normalize happens in ``Config.from_dict``."""
        try:
            raw = json.loads(row.config_json or "{}")
        except json.JSONDecodeError as exc:
            raise OctopError(
                ErrorCode.CHANNEL_INVALID_CREDENTIALS,
                f"channel {row.channel_id} config is not valid JSON",
            ) from exc
        if not isinstance(raw, dict):
            raise OctopError(
                ErrorCode.CHANNEL_INVALID_CREDENTIALS,
                f"channel {row.channel_id} config must be a JSON object",
            )
        return raw

    async def _unregister(self, channel_id: str) -> None:
        if not self._channel_manager:
            return
        try:
            await self._channel_manager.remove_channel(channel_id)
        except Exception:
            logger.exception("Failed to unregister channel %s", channel_id)
        self._set_runtime_status(channel_id, connected=False, reason="unregistered")
