"""Expert target: one bounded agent turn, or a dashboard notification."""

from __future__ import annotations

import logging
from typing import Any

from octop.i18n import tr
from octop.infra.cron.delivery import CronDeliveryCommand, CronDeliveryService
from octop.infra.db.repos.external_events import ExternalEventRow
from octop.infra.db.repos.external_sources import ExternalSourceRow
from octop.infra.db.repos.external_subscriptions import ExternalSubscriptionRow
from octop.infra.db.services import RepoBundle
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.events.service import MODE_EXPERT_NOTIFY
from octop.infra.events.templating import render_template
from octop.infra.events.worker import DeliveryError
from octop.infra.gateway.threads import ThreadRegistry
from octop.infra.utils.locale import resolve_user_locale

logger = logging.getLogger(__name__)

EXTERNAL_CHAT_TYPE = "external"
EXTERNAL_USAGE_SOURCE = "external_event"


def external_guard(locale: str = "en") -> str:
    """Instruction wrapper for externally sourced content (i18n)."""
    return tr("external.guard", locale)


def external_session_key(*, agent_id: str, user_id: int) -> str:
    return ThreadRegistry.make_key(
        agent_id=agent_id,
        channel_type=ThreadRegistry.CHANNEL_DASHBOARD,
        channel_subject_id=str(user_id),
        channel_chat_type=EXTERNAL_CHAT_TYPE,
    )


def external_prompt(
    *, source: ExternalSourceRow, event: ExternalEventRow, locale: str = "en"
) -> str:
    return tr(
        "external.prompt_header",
        locale,
        source=source.name,
        event_type=event.event_type,
        summary=event.summary,
    ) + external_guard(locale)


class ExpertTarget:
    def __init__(
        self,
        *,
        gateway: Any,
        agent_manager: Any,
        delivery: CronDeliveryService,
        repos: RepoBundle,
    ) -> None:
        self._gateway = gateway
        self._agent_manager = agent_manager
        self._delivery = delivery
        self._repos = repos

    async def deliver(
        self,
        event: ExternalEventRow,
        subscription: ExternalSubscriptionRow,
        source: ExternalSourceRow,
        attempt: int,
    ) -> None:
        agent = self._repos.agent_repo.get(subscription.target_id)
        if agent is None or getattr(agent, "kind", "") != "expert":
            raise DeliveryError("expert target no longer exists", retryable=False)
        owner = self._repos.user_repo.get(source.owner_user_id)
        is_admin = bool(owner is not None and owner.role == "admin")
        if not is_admin and agent.user_id != source.owner_user_id:
            raise DeliveryError("expert target is not owned by the source owner", retryable=False)
        session_key = await self._ensure_session(
            agent_id=subscription.target_id,
            user_id=source.owner_user_id,
        )
        locale = resolve_user_locale(
            user_repo=self._repos.user_repo,
            user_id=source.owner_user_id,
            channel_type="dashboard",
        )
        if subscription.mode == MODE_EXPERT_NOTIFY:
            text = (
                render_template(
                    subscription.template,
                    source=source.name,
                    event_type=event.event_type,
                    summary=event.summary,
                    event_id=event.event_id,
                )
                if subscription.template
                else f"[{source.name}] {event.summary}"
            )
            await self._gateway.push_text_from_session(subscription.target_id, session_key, text)
            return

        try:
            self._agent_manager.get_agent(subscription.target_id)
        except OctopError as exc:
            if attempt <= 1:
                await self._notify_stopped(source, event, subscription.target_id, session_key, exc)
            retryable = exc.code in {
                ErrorCode.AGENT_NOT_RUNNING,
                ErrorCode.AGENT_FAILED,
            }
            raise DeliveryError(f"expert not ready: {exc.message}", retryable=retryable) from exc

        if subscription.template:
            rendered = render_template(
                subscription.template,
                source=source.name,
                event_type=event.event_type,
                summary=event.summary,
                event_id=event.event_id,
            )
            prompt = f"{rendered}\n\n{external_guard(locale)}"
        else:
            prompt = external_prompt(source=source, event=event, locale=locale)
        command = CronDeliveryCommand(
            cron_id=f"event:{event.event_id}",
            cron_name=source.name,
            agent_id=subscription.target_id,
            user_id=source.owner_user_id,
            session_key=session_key,
            prompt=prompt,
            fresh_thread=False,
            task_type="agent",
            model=None,
            mcp_servers=(),
            meta={
                "kind": "external_event",
                "event_id": event.event_id,
                "source_id": source.source_id,
                "subscription_id": subscription.subscription_id,
            },
            usage_source=EXTERNAL_USAGE_SOURCE,
        )
        await self._delivery.deliver(command)

    async def _ensure_session(self, *, agent_id: str, user_id: int) -> str:
        session_key = external_session_key(agent_id=agent_id, user_id=user_id)
        await self._gateway.thread_registry.get_or_create_by_key(
            session_key=session_key,
            agent_id=agent_id,
            user_id=user_id,
            channel_type=ThreadRegistry.CHANNEL_DASHBOARD,
            channel_chat_type=EXTERNAL_CHAT_TYPE,
        )
        return session_key

    async def _notify_stopped(
        self,
        source: ExternalSourceRow,
        event: ExternalEventRow,
        agent_id: str,
        session_key: str,
        exc: OctopError,
    ) -> None:
        locale = resolve_user_locale(
            user_repo=self._repos.user_repo,
            user_id=source.owner_user_id,
            channel_type="dashboard",
        )
        text = tr("external.expert_stopped", locale, source=source.name, summary=event.summary)
        logger.info("external event for stopped expert %s: %s", agent_id, exc.message)
        try:
            await self._gateway.push_text_from_session(agent_id, session_key, text)
        except Exception:
            logger.debug("expert stopped notification failed", exc_info=True)
