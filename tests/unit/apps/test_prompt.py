"""Unit tests for the light-app system-prompt fragment."""

from __future__ import annotations

from octop.infra.apps.prompt import (
    LIGHT_APP_PROMPT_KEY,
    append_light_app_system_prompt,
    light_app_system_prompt,
)


def test_prompt_is_localized() -> None:
    zh = light_app_system_prompt("zh")
    en = light_app_system_prompt("en")
    assert zh and en
    assert zh != en
    assert LIGHT_APP_PROMPT_KEY == "light_apps.system_prompt"


def test_prompt_mentions_the_rules_the_model_cannot_guess() -> None:
    text = light_app_system_prompt("en")
    assert "app_create" in text
    assert "app_write_html" in text
    assert "app_delete" in text
    assert "octopApp" in text
    assert "listRows" in text
    assert "data:" in text
    assert "blob:" in text


def test_append_keeps_the_persona_in_front() -> None:
    out = append_light_app_system_prompt("You are 小应.", locale="zh")
    assert out.startswith("You are 小应.")
    assert light_app_system_prompt("zh") in out


def test_append_without_persona_returns_the_rules_only() -> None:
    assert append_light_app_system_prompt(None, locale="en") == light_app_system_prompt("en")
    assert append_light_app_system_prompt("   ", locale="en") == light_app_system_prompt("en")


def test_append_is_idempotent() -> None:
    once = append_light_app_system_prompt("Persona", locale="zh")
    twice = append_light_app_system_prompt(once, locale="zh")
    assert twice == once
    assert twice.count(light_app_system_prompt("zh")) == 1
