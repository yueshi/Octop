"""Global SecurityPolicy persistence in the settings table."""

from __future__ import annotations

import json
import logging
from typing import Any

from octop_harness.security.models import SecurityPolicy

from octop.infra.agents.security.hitl_session import MANDATORY_HITL_TOOLS
from octop.infra.db.repos.settings import SettingsRepo

logger = logging.getLogger(__name__)

_SETTINGS_KEY = "security_policy"


def _default_policy() -> SecurityPolicy:
    """Octop defaults when no ``security_policy`` row exists in settings."""
    data = SecurityPolicy.defaults().to_dict()
    data["hitl"]["enabled"] = False
    data["tool_guard"] = {"enabled": True, "mode": "warn"}
    return SecurityPolicy.from_dict(data)


class SecuritySettingsStore:
    """Read/write global :class:`SecurityPolicy` in the settings table."""

    def __init__(self, *, settings_repo: SettingsRepo) -> None:
        self._settings = settings_repo

    def load(self) -> SecurityPolicy:
        raw = self._settings.get(_SETTINGS_KEY)
        if not raw:
            return _default_policy()
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("invalid security_policy JSON in settings; using defaults")
            return _default_policy()
        if not isinstance(data, dict):
            return _default_policy()
        return SecurityPolicy.from_dict(data)

    def save(self, policy: SecurityPolicy | dict[str, Any]) -> SecurityPolicy:
        resolved = (
            policy if isinstance(policy, SecurityPolicy) else SecurityPolicy.from_dict(policy)
        )
        self._settings.set(_SETTINGS_KEY, json.dumps(resolved.to_dict(), ensure_ascii=False))
        return resolved

    def harness_policy(self) -> SecurityPolicy:
        """Policy handed to ``HarnessAgentManager`` at boot.

        ``SecurityPolicy.apply_to_config`` *replaces* ``interrupt_on`` with
        ``resolve_interrupt_on()`` every time the harness registers or rebuilds
        an agent. Any interrupt Octop adds after that point (the mandatory
        ``app_delete`` gate, connector write approvals) is therefore lost.
        Mandatory gates must live in the policy itself so they survive that
        re-application even when admin HITL is off.
        """
        policy = self.load()
        resolved = policy.resolve_interrupt_on() or {}
        missing = MANDATORY_HITL_TOOLS - resolved.keys()
        if not missing:
            return policy
        data = policy.to_dict()
        data["hitl"] = {
            **data["hitl"],
            "enabled": True,
            # Materialize the effective tool list: "default" is expanded inside
            # resolve_interrupt_on, which would otherwise drop the extra tools.
            "tools": sorted({*resolved, *missing}),
        }
        return SecurityPolicy.from_dict(data)


def tool_execution_may_pause(policy: SecurityPolicy) -> bool:
    """True when a tool call can pause for human approval.

    Covers the admin HITL switch and command-guard ``require_approval``.
    ``ask_user_question`` is a separate collaboration pause and is ignored here.
    """
    if policy.hitl.enabled:
        return True
    guard = policy.tool_guard
    return bool(guard.enabled and guard.mode == "require_approval")
