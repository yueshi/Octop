"""System-prompt fragment that teaches an agent the light-app rules.

The app tools carry their own descriptions, but a model only learns the hard
parts (single-file sandboxed HTML, the ``window.octopApp`` SDK, mandatory
approval on delete) by failing at them. Injecting the rules into the system
prompt of every agent that owns the app tools avoids that.
"""

from __future__ import annotations

from octop.i18n import tr
from octop.infra.utils.locale import Locale

LIGHT_APP_PROMPT_KEY = "light_apps.system_prompt"

__all__ = [
    "LIGHT_APP_PROMPT_KEY",
    "append_light_app_system_prompt",
    "light_app_system_prompt",
]


def light_app_system_prompt(locale: str | Locale = "zh") -> str:
    """Return the localized light-app rules for an agent's system prompt."""
    return tr(LIGHT_APP_PROMPT_KEY, locale)


def append_light_app_system_prompt(
    system_prompt: str | None,
    *,
    locale: str | Locale = "zh",
) -> str:
    """Return *system_prompt* with the light-app rules appended.

    The rules are appended rather than replacing the persona: the agent keeps
    its own voice and only gains the app-specific constraints. Appending is
    idempotent so a re-built prompt never carries the rules twice.
    """
    rules = light_app_system_prompt(locale)
    base = (system_prompt or "").strip()
    if not base:
        return rules
    if rules in base:
        return base
    return f"{base}\n\n{rules}"
