"""Resolve the chat model used by per-app AI features.

Both the read-only data assistant and in-app text generation call the owning
agent's default model. Keeping the resolution here means the two features
cannot drift apart on wrapping (``ModelAccess`` vs raw chat model) or on the
error raised when the agent is unavailable.
"""

from __future__ import annotations

from typing import Any, Protocol

from octop.infra.errors import ErrorCode, OctopError


class AppModelSource(Protocol):
    """The slice of the live harness the per-app AI features need."""

    def get_agent(self, agent_id: str) -> Any: ...


def resolve_app_model(agents: AppModelSource, agent_id: str) -> tuple[Any, str]:
    """Return ``(chat_model, model_ref)`` for the app's owning agent.

    Raises the owning agent's availability error (``AGENT_NOT_RUNNING`` et al.)
    so callers can surface it before starting a request.
    """
    agent = agents.get_agent(agent_id)
    try:
        model_ref = agent.config.pick_default_model_ref()
        # The factory returns a ModelAccess wrapper in production; tests stub a
        # plain chat model. Callers need the raw model.
        access = agent.model_factory.get(model_ref)
        llm = getattr(access, "chat_model", access)
    except Exception as exc:
        raise OctopError(ErrorCode.INTERNAL_ERROR, f"no usable model: {exc}") from exc
    return llm, model_ref
