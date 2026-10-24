"""Runs external connector tools for light-app data sources.

Only in-process gateway connectors (``mcp_mode=gateway``) are supported: their
Python adapters are callable server-side without an MCP round trip, and their
tool list carries the metadata the binding UI needs. HTTP MCP servers would
need a client stack in Octop — out of scope for now.
"""

from __future__ import annotations

from typing import Any

from octop.infra.connectors.catalog import get_catalog_entry, is_inprocess_gateway
from octop.infra.connectors.gateway.registry import (
    call_gateway_tool,
    mcp_tools_for_kind,
)
from octop.infra.connectors.service import ConnectorService
from octop.infra.db.repos.connectors import ConnectorRepo
from octop.infra.errors import ErrorCode, OctopError


class GatewayToolRunner:
    """Calls one tool of one connector instance with that instance's credentials."""

    def __init__(self, *, repo: ConnectorRepo, service: ConnectorService) -> None:
        self._repo = repo
        self._service = service

    def run(self, *, instance_id: str, tool: str, args: dict[str, Any]) -> str:
        inst = self._repo.get(instance_id)
        if inst is None:
            raise OctopError(ErrorCode.APP_SOURCE_INVALID, f"unknown connector: {instance_id!r}")
        entry = get_catalog_entry(inst.kind)
        if entry is None or not is_inprocess_gateway(entry):
            raise OctopError(
                ErrorCode.APP_SOURCE_INVALID,
                f"connector kind {inst.kind!r} cannot be used as a data source",
            )
        _require_read_only(inst.kind, tool)
        creds = self._service.decrypt(instance_id)
        if not creds:
            raise OctopError(
                ErrorCode.APP_SOURCE_INVALID,
                f"connector instance {instance_id!r} has no usable credentials",
            )
        return call_gateway_tool(inst.kind, creds, tool, args)

    def list_tools(self, instance_id: str) -> list[dict[str, Any]]:
        """Tool metadata for the binding dialog (read-only flag included)."""
        inst = self._repo.get(instance_id)
        if inst is None:
            raise OctopError(ErrorCode.APP_SOURCE_INVALID, f"unknown connector: {instance_id!r}")
        entry = get_catalog_entry(inst.kind)
        if entry is None or not is_inprocess_gateway(entry):
            return []
        return mcp_tools_for_kind(inst.kind)

    def list_tool_meta(self, instance_id: str) -> list[dict[str, Any]]:
        """Normalized tool metadata: name/description/read_only/input_schema.

        ``read_only`` mirrors ``_require_read_only``: only an explicit
        ``readOnlyHint: false`` marks a tool as not read-only.
        """
        normalized: list[dict[str, Any]] = []
        for tool in self.list_tools(instance_id):
            annotations = tool.get("annotations")
            schema = tool.get("inputSchema")
            normalized.append(
                {
                    "name": str(tool.get("name") or ""),
                    "description": str(tool.get("description") or ""),
                    "read_only": not (
                        isinstance(annotations, dict) and annotations.get("readOnlyHint") is False
                    ),
                    "input_schema": dict(schema) if isinstance(schema, dict) else {},
                }
            )
        return normalized

    def tool_meta(self, instance_id: str, tool: str) -> dict[str, Any] | None:
        """One normalized entry, or None when the tool is unknown."""
        for meta in self.list_tool_meta(instance_id):
            if meta["name"] == tool:
                return meta
        return None


def _require_read_only(kind: str, tool: str) -> None:
    """Refuse tools that explicitly declare ``readOnlyHint: false``.

    Gateway tools carry no annotations today; the flag is enforced when a
    connector starts declaring it, and the binding UI warns for unannotated
    tools (per the design).
    """
    for tool_meta in mcp_tools_for_kind(kind):
        if str(tool_meta.get("name") or "") != tool:
            continue
        annotations = tool_meta.get("annotations")
        if isinstance(annotations, dict) and annotations.get("readOnlyHint") is False:
            raise OctopError(
                ErrorCode.APP_SOURCE_INVALID,
                f"tool {tool!r} is not read-only and cannot back a data source",
            )
        return
    raise OctopError(ErrorCode.APP_SOURCE_INVALID, f"unknown tool: {tool!r} for kind {kind!r}")
