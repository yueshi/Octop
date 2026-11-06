"""System-level MiniApp templates shipped with the package.

Agent-provided templates still live in the agent workspace under
``app_templates/``. This module owns the read-only, system-wide catalog so the
same shells are available to every user and every agent.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from octop.infra.apps.preview import PREVIEW_FILENAME
from octop.infra.errors import ErrorCode, OctopError

SYSTEM_SOURCE = "system"
AGENT_SOURCE = "agent"

SYSTEM_TEMPLATE_ROOT = Path(__file__).with_name("system_templates")
MANIFEST_NAME = "manifest.json"
SCHEMA_NAME = "schema.json"
HTML_NAME = "app.html"


@dataclass(frozen=True)
class TemplateInfo:
    """One template available in the creation gallery."""

    template_id: str
    source: str
    name: str
    description: str
    category: str
    version: int
    has_html: bool
    agent_id: str | None = None
    has_preview: bool = False

    @property
    def scope(self) -> str:
        return "global" if self.source == SYSTEM_SOURCE else "agent"

    def to_dict(self) -> dict[str, Any]:
        return {
            "template": self.template_id,
            "template_id": self.template_id,
            "label": self.name,
            "name": self.name,
            "description": self.description,
            "category": self.category,
            "version": self.version,
            "has_html": self.has_html,
            "has_preview": self.has_preview,
            "source": self.source,
            "agent_id": self.agent_id,
            "scope": self.scope,
        }


def clean_template_id(raw: str) -> str:
    """Reject ids that could escape the template directory."""
    value = str(raw or "").strip()
    if not value or value.startswith(".") or "/" in value or "\\" in value:
        raise OctopError(ErrorCode.APP_NOT_FOUND, f"unknown app template: {raw!r}")
    return value


def localized(value: object, locale: str) -> str:
    """Pick ``zh``/``en`` from a manifest label, or return a plain string."""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        for key in (locale, "zh", "en"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
    return ""


def _read_manifest(directory: Path) -> dict[str, Any]:
    path = directory / MANIFEST_NAME
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _version(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return 0
    try:
        version = int(value)
    except ValueError:
        return 0
    return max(version, 0)


def system_template_ids() -> list[str]:
    if not SYSTEM_TEMPLATE_ROOT.is_dir():
        return []
    return sorted(
        entry.name
        for entry in SYSTEM_TEMPLATE_ROOT.iterdir()
        if entry.is_dir() and (entry / MANIFEST_NAME).is_file()
    )


def list_system_templates(locale: str = "zh") -> list[TemplateInfo]:
    templates: list[TemplateInfo] = []
    for template_id in system_template_ids():
        directory = SYSTEM_TEMPLATE_ROOT / template_id
        manifest = _read_manifest(directory)
        templates.append(
            TemplateInfo(
                template_id=template_id,
                source=SYSTEM_SOURCE,
                name=localized(manifest.get("name"), locale) or template_id,
                description=localized(manifest.get("description"), locale),
                category=str(manifest.get("category") or ""),
                version=_version(manifest.get("version")),
                has_html=(directory / HTML_NAME).is_file(),
                has_preview=(directory / PREVIEW_FILENAME).is_file(),
            )
        )
    return templates


def read_system_template(template_id: str) -> tuple[str, str | None]:
    """Return ``(schema_text, html_text)`` for a bundled system template."""
    clean = clean_template_id(template_id)
    directory = SYSTEM_TEMPLATE_ROOT / clean
    if not (directory / MANIFEST_NAME).is_file():
        raise OctopError(ErrorCode.APP_NOT_FOUND, f"unknown app template: {template_id!r}")
    manifest = _read_manifest(directory)
    schema_rel = str(manifest.get("schema") or SCHEMA_NAME)
    html_rel = str(manifest.get("html") or HTML_NAME)
    schema_path = _safe_child(directory, schema_rel)
    schema_text = schema_path.read_text(encoding="utf-8") if schema_path.is_file() else None
    if schema_text is None:
        raise OctopError(ErrorCode.APP_NOT_FOUND, f"template has no schema: {template_id!r}")
    html_path = _safe_child(directory, html_rel)
    html_text = html_path.read_text(encoding="utf-8") if html_path.is_file() else None
    return schema_text, html_text


def agent_template_info(
    *,
    agent_id: str,
    template_id: str,
    manifest: dict[str, Any] | None,
    has_html: bool,
    locale: str,
    has_preview: bool = False,
) -> TemplateInfo:
    """Build a gallery entry for a workspace template."""
    data = manifest or {}
    return TemplateInfo(
        template_id=template_id,
        source=AGENT_SOURCE,
        name=localized(data.get("name") or data.get("label"), locale) or template_id,
        description=localized(data.get("description"), locale),
        category=str(data.get("category") or ""),
        version=_version(data.get("version")),
        has_html=has_html,
        agent_id=agent_id,
        has_preview=has_preview,
    )


def _safe_child(root: Path, relative: str) -> Path:
    candidate = (root / relative).resolve()
    root_resolved = root.resolve()
    if candidate != root_resolved and root_resolved not in candidate.parents:
        raise OctopError(ErrorCode.APP_NOT_FOUND, "invalid template file path")
    return candidate
