"""Light app lifecycle: workspace files + control-plane metadata.

Workspace layout (workspace-relative, resolved through ``BackendWorkspace``)::

    apps/<app_id>/app.html      rendered UI (written by the agent)
    apps/<app_id>/data.csv      rows; header == schema column names
    apps/<app_id>/schema.json   column definitions
    apps/<app_id>/manifest.json human-readable snapshot
    apps/<app_id>/preview.svg   cover image drawn by the agent

Schema and data updates are staged to ``*.new`` files and committed through
``.pending.json`` so an interrupted write is finished by the next read instead
of leaving the pair truncated or inconsistent.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import re
import secrets
import threading
import time
import zipfile
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Protocol

from octop_harness.backends import BackendWorkspace

from octop.infra.apps import capabilities as app_capabilities
from octop.infra.apps.importer import parse_import_file
from octop.infra.apps.inspect import detect_pii, inspect_file
from octop.infra.apps.models import (
    MAIN_TABLE_KEY,
    MAX_APPS_PER_OWNER,
    MAX_COLLABORATORS,
    MAX_CSV_BYTES,
    MAX_LABEL_LEN,
    MAX_ROW_READ,
    MAX_ROWS,
    MAX_TABLES_PER_APP,
    TABLE_KEY_RE,
    Column,
    Schema,
    extend_schema,
    parse_csv,
    parse_schema_payload,
    redact_rows,
    render_csv,
    validate_app_id,
    validate_description,
    validate_html,
    validate_name,
    validate_permission,
    validate_row,
    validate_table_key,
)
from octop.infra.apps.pii import find_unmarked_pii
from octop.infra.apps.preview import PREVIEW_FILENAME, validate_preview_svg
from octop.infra.apps.templates import (
    AGENT_SOURCE,
    SYSTEM_SOURCE,
    SYSTEM_TEMPLATE_ROOT,
    TemplateInfo,
    agent_template_info,
    clean_template_id,
    list_system_templates,
    read_system_template,
)
from octop.infra.apps.viewgen import render_html as render_view_html
from octop.infra.apps.viewgen import render_multi_html, suggest_view
from octop.infra.db.repos.agents import AgentRepo
from octop.infra.db.repos.apps import (
    PERMISSION_RANK,
    LightAppCollaboratorRow,
    LightAppDataRow,
    LightAppPublicationRow,
    LightAppRepo,
    LightAppRow,
    LightAppSourceRow,
)
from octop.infra.db.repos.secrets import SecretRepo
from octop.infra.db.repos.users import UserRepo, UserRow
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.utils.ulid import new_short_id

logger = logging.getLogger(__name__)

APPS_DIR = "apps"
FILE_HTML = "app.html"
FILE_DATA = "data.csv"
FILE_SCHEMA = "schema.json"
FILE_MANIFEST = "manifest.json"
FILE_PENDING = ".pending.json"
FILE_PREVIEW = PREVIEW_FILENAME
"""Cover image of an app, drawn by the agent as SVG."""
TABLES_DIR = "tables"
STAGED_SUFFIX = ".new"
ASSETS_DIR = "assets"

MAX_ASSET_BYTES = 5 * 1024 * 1024

MAX_FAILED_ROWS = 200
"""Largest per-row failure list returned by one import."""
"""Largest asset an app may store (covers book covers and small icons)."""

BACKUP_FORMAT = "octop.light-app.backup"
BACKUP_VERSION = 1
MAX_BACKUP_BYTES = 64 * 1024 * 1024
"""Largest backup archive accepted for restore."""

MAX_BACKUP_UNCOMPRESSED_BYTES = 256 * 1024 * 1024
"""Cap on the expanded archive; guards against zip bombs."""

ASSET_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
"""Asset file names: no path separators, no leading dot, no traversal."""

# The app page may not load remote images, so a cover or an icon has to be
# stored next to the app. SVG is deliberately absent: it can carry script.
_ASSET_SIGNATURES: tuple[tuple[str, bytes], ...] = (
    ("image/png", b"\x89PNG\r\n\x1a\n"),
    ("image/jpeg", b"\xff\xd8\xff"),
    ("image/gif", b"GIF8"),
)

_APP_LOCKS: dict[str, asyncio.Lock] = {}
_APP_LOCKS_GUARD = threading.Lock()


def _lock_for(app_id: str) -> asyncio.Lock:
    with _APP_LOCKS_GUARD:
        lock = _APP_LOCKS.get(app_id)
        if lock is None:
            lock = asyncio.Lock()
            _APP_LOCKS[app_id] = lock
        return lock


def _forget_lock(app_id: str) -> None:
    with _APP_LOCKS_GUARD:
        _APP_LOCKS.pop(app_id, None)


def app_dir(app_id: str) -> str:
    return f"{APPS_DIR}/{validate_app_id(app_id)}"


def app_path(app_id: str, filename: str) -> str:
    return f"{app_dir(app_id)}/{filename}"


def table_file_names(table_key: str) -> tuple[str, str]:
    """Workspace-relative ``(schema, data)`` file names for a table.

    The main table keeps the historical ``schema.json`` / ``data.csv`` names so
    apps created before multi-table support keep working untouched.
    """
    if table_key == MAIN_TABLE_KEY:
        return FILE_SCHEMA, FILE_DATA
    return f"{TABLES_DIR}/{table_key}.schema.json", f"{TABLES_DIR}/{table_key}.csv"


def _is_atomic_app_file(name: str) -> bool:
    """Whether *name* may be written through the staged-write journal."""
    if name in (FILE_DATA, FILE_SCHEMA):
        return True
    if not name.startswith(f"{TABLES_DIR}/"):
        return False
    stem, _, filename = name[len(TABLES_DIR) + 1 :].partition(".")
    if not stem or not TABLE_KEY_RE.fullmatch(stem):
        return False
    return filename in ("csv", "schema.json")


class WorkspaceSource(Protocol):
    def workspace_for_agent(self, agent_id: str) -> BackendWorkspace | None: ...


class ExternalToolRunner(Protocol):
    """Runs one connector tool with the app owner's credentials.

    Implemented by the server wiring (``ConnectorService`` + gateway registry);
    kept as a protocol so the app service stays free of connector imports.
    """

    def run(self, *, instance_id: str, tool: str, args: dict[str, Any]) -> str: ...


class AppRecord(Protocol):
    """Read-only view of an app row used by the service (avoids repo imports above)."""

    @property
    def app_id(self) -> str: ...

    @property
    def agent_id(self) -> str: ...

    @property
    def owner_user_id(self) -> int: ...

    @property
    def name(self) -> str: ...

    @property
    def description(self) -> str: ...

    @property
    def row_count(self) -> int: ...

    @property
    def html_bytes(self) -> int: ...

    @property
    def data_bytes(self) -> int: ...

    @property
    def storage_version(self) -> int: ...

    @property
    def capabilities_json(self) -> str: ...

    @property
    def created_at(self) -> int: ...

    @property
    def updated_at(self) -> int: ...


@dataclass(frozen=True)
class AppView:
    app_id: str
    name: str
    description: str
    agent_id: str
    agent_name: str
    owner_user_id: int
    owner_username: str
    permission: str
    row_count: int
    html_bytes: int
    data_bytes: int
    created_at: int
    updated_at: int
    has_preview: bool = False
    capabilities: tuple[str, ...] = ()

    @property
    def has_html(self) -> bool:
        """Whether the agent has written the app UI yet."""
        return self.html_bytes > 0


@dataclass(frozen=True)
class ShareView:
    user_id: int
    username: str
    display_name: str
    permission: str


@dataclass(frozen=True)
class AppFile:
    """One file of an app's workspace directory."""

    name: str
    path: str
    size: int | None = None


@dataclass(frozen=True)
class AppTableView:
    """One table of an app as the dashboard and tools see it."""

    table_key: str
    label: str
    row_count: int
    is_main: bool
    public_visible: bool


@dataclass(frozen=True)
class PublicationStatus:
    """Whether the public link still shows the current app version."""

    published_at: int | None
    stale: bool
    reasons: tuple[str, ...] = ()


IMPORT_MODE_APPEND = "append"
IMPORT_MODE_REPLACE = "replace"
IMPORT_MODES = (IMPORT_MODE_APPEND, IMPORT_MODE_REPLACE)

IMPORT_DIR = "inbound/imports"
MAX_IMPORT_ERRORS = 5
"""Row-level validation failures reported per import (the rest are counted)."""

SOURCE_KIND_WORKSPACE_FILE = "workspace_file"
SOURCE_KIND_EXTERNAL = "external"
SOURCE_KINDS = (SOURCE_KIND_WORKSPACE_FILE, SOURCE_KIND_EXTERNAL)
SOURCE_STATUS_OK = "ok"
SOURCE_STATUS_ERROR = "error"
MAX_SCHEDULE_SECONDS = 30 * 24 * 3600
MAX_BACKOFF_SECONDS = 24 * 3600
"""Auto-sync failure backoff cap (a day between retries, however often it fails)."""


@dataclass(frozen=True)
class ImportResult:
    """Outcome of one manual import into a single table."""

    imported: int
    skipped: int
    errors: list[str]
    updated: int = 0
    """Rows replaced in place by an upsert key."""
    warnings: list[str] = field(default_factory=list)
    """Non-fatal notes, e.g. columns that look like sensitive data."""
    would_import: int = 0
    """Rows a dry run would have written."""
    dry_run: bool = False
    """True when nothing was written."""
    pii_columns: list[str] = field(default_factory=list)
    """Columns detected or marked as sensitive during this import."""
    blocked_pii: list[str] = field(default_factory=list)
    """Sensitive columns whose values were not written (``include_pii=false``)."""
    failed_rows: list[dict[str, Any]] = field(default_factory=list)
    """Up to ``MAX_FAILED_ROWS`` rejected rows with their errors, for retry."""


@dataclass(frozen=True)
class SourceView:
    """A data-source binding as the dashboard and tools see it."""

    source_id: str
    app_id: str
    table_key: str
    kind: str
    mode: str
    schedule_seconds: int
    params: dict[str, Any]
    mapping: dict[str, str]
    last_run_at: int | None
    last_status: str | None
    last_error: str | None
    last_row_count: int | None
    created_by: int
    created_at: int
    updated_at: int


def _safe_import_path(path: str) -> str:
    """Normalize a workspace-relative import source path.

    Rejects empty paths, traversal, and any hidden segment (``.octop`` system
    files, ``.pending.json`` journal, …) so imports cannot read credentials or
    internal state into app data.
    """
    clean = "/".join(part for part in path.strip().split("/") if part and part != ".")
    if (
        not clean
        or clean.startswith("/")
        or any(part.startswith(".") or part == ".." for part in clean.split("/"))
    ):
        raise _invalid(ErrorCode.APP_ROW_INVALID, f"invalid import path: {path!r}")
    return clean


def _safe_import_filename(filename: str) -> str:
    """Basename of an uploaded file for storage under ``inbound/imports/``."""
    name = filename.replace("\\", "/").rsplit("/", 1)[-1].strip()
    name = " ".join(name.split())
    if not name or name.startswith(".") or "/" in name:
        name = "data.csv"
    return name[:100]


def _file_stem(filename: str) -> str:
    name = _safe_import_filename(filename)
    return name.rsplit(".", 1)[0].strip()[:100] or "工作表"


def _select_sheet_report(report: Mapping[str, Any], sheet: str | None) -> dict[str, Any]:
    sheets = list(report.get("sheets") or [])
    if not sheets:
        raise _invalid(ErrorCode.APP_ROW_INVALID, "file has no sheets")
    if sheet is None or not str(sheet).strip():
        return dict(sheets[0])
    wanted = str(sheet).strip()
    for item in sheets:
        if str(item.get("name") or "") == wanted:
            return dict(item)
    try:
        index = int(wanted)
    except ValueError:
        index = -1
    if 0 <= index < len(sheets):
        return dict(sheets[index])
    raise _invalid(ErrorCode.APP_ROW_INVALID, f"sheet not found: {sheet!r}")


_QUICK_COLUMN_TYPES = {"string", "text", "integer", "number", "boolean", "date", "datetime"}


def _quick_columns(report: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Turn inspection suggestions into a valid, editable schema."""
    columns: list[dict[str, Any]] = []
    used: set[str] = set()
    for index, item in enumerate(report.get("columns") or []):
        if not isinstance(item, Mapping):
            continue
        base = str(item.get("name_suggestion") or f"column_{index + 1}").strip()
        name = re.sub(r"[^a-z0-9_]+", "_", base.lower()).strip("_") or f"column_{index + 1}"
        candidate = name
        suffix = 2
        while candidate in used:
            candidate = f"{name}_{suffix}"
            suffix += 1
        used.add(candidate)
        column_type = str(item.get("type") or "string")
        if column_type not in _QUICK_COLUMN_TYPES:
            column_type = "string"
        label = (str(item.get("source") or candidate).strip() or candidate)[:100]
        column: dict[str, Any] = {
            "name": candidate,
            "label": label,
            "type": column_type,
            "required": False,
        }
        pii = item.get("pii")
        if isinstance(pii, str) and pii:
            column["pii"] = pii
        columns.append(column)
    if not columns:
        raise _invalid(ErrorCode.APP_SCHEMA_INVALID, "file has no usable columns")
    return columns


def _json_path(payload: Any, path: str) -> Any:
    node = payload
    for part in path.split("."):
        if isinstance(node, dict) and part in node:
            node = node[part]
        else:
            return None
    return node


def _extract_records(output: str, result_path: str) -> list[dict[str, str]]:
    """Parse a connector tool's text output into rows.

    ``result_path`` is a dot path (e.g. ``data.items``) pointing at the array
    of objects inside the returned JSON; empty means the output itself is the
    array. Every element is stringified — column validation happens later.
    """
    try:
        payload = json.loads(output)
    except json.JSONDecodeError as exc:
        raise _invalid(
            ErrorCode.APP_SOURCE_INVALID, f"tool output is not JSON: {exc.msg}"
        ) from None
    if result_path:
        node: Any = payload
        for part in result_path.split("."):
            if isinstance(node, dict) and part in node:
                node = node[part]
            else:
                raise _invalid(
                    ErrorCode.APP_SOURCE_INVALID,
                    f"result_path {result_path!r} not found in tool output",
                )
    else:
        node = payload
    if not isinstance(node, list) or any(not isinstance(item, dict) for item in node):
        raise _invalid(
            ErrorCode.APP_SOURCE_INVALID,
            "tool result must be an array of objects (adjust result_path)",
        )
    records: list[dict[str, str]] = []
    for item in node:
        records.append(
            {
                str(key).strip(): "" if value is None else str(value)
                for key, value in item.items()
                if str(key).strip()
            }
        )
    return records


def _invalid(code: ErrorCode, reason: str, **details: object) -> OctopError:
    return OctopError(code, reason, details={**details})


def _entry_field(entry: Any, key: str) -> Any:
    """Read a field from an ``ls`` entry (mapping or object, per backend)."""
    if isinstance(entry, Mapping):
        return entry.get(key)
    return getattr(entry, key, None)


def _zip_entries(entries: list[tuple[str, bytes]]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in entries:
            archive.writestr(name, data)
    return buffer.getvalue()


def _read_backup_zip(
    data: bytes,
) -> tuple[
    dict[str, Any],
    dict[str, tuple[str, bytes, bytes]],
    str | None,
    str | None,
    list[tuple[str, bytes]],
]:
    """Read and validate a backup archive without touching the workspace."""
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        infos = archive.infolist()
        if sum(info.file_size for info in infos) > MAX_BACKUP_UNCOMPRESSED_BYTES:
            raise ValueError("archive expands beyond the allowed size")
        names = {info.filename for info in infos}
        if FILE_MANIFEST not in names:
            raise ValueError("manifest.json is missing")
        # Read through a running budget: the per-entry header size can be
        # forged, so trust the bytes actually decompressed, not the manifest.
        remaining = MAX_BACKUP_UNCOMPRESSED_BYTES

        def read_entry(name: str) -> bytes:
            nonlocal remaining
            with archive.open(name) as handle:
                content = handle.read(remaining + 1)
            if len(content) > remaining:
                raise ValueError("archive expands beyond the allowed size")
            remaining -= len(content)
            return content

        try:
            manifest = json.loads(read_entry(FILE_MANIFEST))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ValueError("manifest.json is not valid JSON") from None
        if not isinstance(manifest, dict) or manifest.get("format") != BACKUP_FORMAT:
            raise ValueError("not an Octop MiniApp backup")
        version = manifest.get("version")
        if not isinstance(version, int) or version > BACKUP_VERSION:
            raise ValueError("backup was made by a newer version")
        if not isinstance(manifest.get("app"), dict):
            raise ValueError("manifest is missing app metadata")
        raw_tables = manifest.get("tables")
        if not isinstance(raw_tables, list) or not raw_tables:
            raise ValueError("manifest is missing tables")
        tables: dict[str, tuple[str, bytes, bytes]] = {}
        for item in raw_tables:
            if not isinstance(item, dict):
                raise ValueError("manifest has an invalid table entry")
            key = validate_table_key(str(item.get("table_key") or ""))
            if key in tables:
                raise ValueError(f"duplicate table: {key}")
            schema_name = f"{TABLES_DIR}/{key}/{FILE_SCHEMA}"
            csv_name = f"{TABLES_DIR}/{key}/{FILE_DATA}"
            if schema_name not in names:
                raise ValueError(f"table {key} has no schema")
            label = str(item.get("label") or "")[:MAX_LABEL_LEN]
            tables[key] = (
                label,
                read_entry(schema_name),
                read_entry(csv_name) if csv_name in names else b"",
            )
        html: str | None = None
        if FILE_HTML in names:
            try:
                html = read_entry(FILE_HTML).decode("utf-8")
            except UnicodeDecodeError:
                raise ValueError("app.html is not valid UTF-8") from None
        assets: list[tuple[str, bytes]] = []
        for name in names:
            if not name.startswith(f"{ASSETS_DIR}/"):
                continue
            asset_name = name[len(ASSETS_DIR) + 1 :]
            if not asset_name or "/" in asset_name or "\\" in asset_name:
                continue
            assets.append((asset_name, read_entry(name)))
        preview: str | None = None
        if FILE_PREVIEW in names:
            try:
                preview = read_entry(FILE_PREVIEW).decode("utf-8")
            except UnicodeDecodeError:
                raise ValueError("preview.svg is not valid UTF-8") from None
    return manifest, tables, html, preview, assets


def _normalize_rows(schema: Schema, rows: list[dict[str, str]]) -> list[dict[str, str]]:
    """Fill columns added after a row was written with empty cells."""
    names = schema.names
    return [{name: item.get(name, "") for name in names} for item in rows]


def _with_detected_pii(schema: Schema, rows: Sequence[Mapping[str, str]]) -> Schema:
    """Mark columns whose name or sample values look sensitive."""
    columns: list[Column] = []
    changed = False
    for column in schema.columns:
        if column.pii:
            columns.append(column)
            continue
        samples = [str(row.get(column.name, "")) for row in rows[:50]]
        kind = detect_pii(column.name, samples)
        if kind is None and column.label != column.name:
            kind = detect_pii(column.label, samples)
        if kind is None:
            columns.append(column)
        else:
            columns.append(replace(column, pii=kind))
            changed = True
    return replace(schema, columns=tuple(columns)) if changed else schema


def _pii_names(schema: Schema) -> list[str]:
    return [column.name for column in schema.columns if column.pii]


def _apply_pii(
    schema: Schema,
    rows: list[dict[str, str]],
    *,
    redact_pii: bool,
) -> tuple[Schema, list[dict[str, str]]]:
    schema = _with_detected_pii(schema, rows)
    return schema, (redact_rows(schema, rows, redact_text=True) if redact_pii else rows)


def _reject_unmarked_pii(
    schema: Schema,
    values: Mapping[str, object],
    *,
    table: str,
) -> None:
    """Refuse a write that would put PII into an unclassified free-text column."""
    hits = find_unmarked_pii(
        ((column.name, column.pii, column.type) for column in schema.columns),
        values,
    )
    if not hits:
        return
    column, hit = hits[0]
    raise _invalid(
        ErrorCode.APP_PII_UNMARKED,
        (
            f"column {column!r} contains {hit.kind} data; mark it with pii "
            "or move the value to a column marked as sensitive"
        ),
        table=table,
        column=column,
        kind=hit.kind,
    )


def _sniff_asset_mime(data: bytes) -> str | None:
    """Return the media type of an image by its magic bytes.

    The content type header of an upload is not trusted: an SVG or an HTML
    document renamed to ``.png`` would end up in the app directory.
    """
    for mime, signature in _ASSET_SIGNATURES:
        if data.startswith(signature):
            return mime
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    return None


class AppService:
    def __init__(
        self,
        *,
        app_repo: LightAppRepo,
        agent_repo: AgentRepo,
        user_repo: UserRepo,
        workspaces: WorkspaceSource,
        external_tool_runner: ExternalToolRunner | None = None,
        secret_repo: SecretRepo | None = None,
    ) -> None:
        self._app_repo = app_repo
        self._agent_repo = agent_repo
        self._user_repo = user_repo
        self._workspaces = workspaces
        self._external_tool_runner = external_tool_runner
        self._secret_repo = secret_repo

    # ----- lookup / ACL -----

    def get(self, app_id: str) -> LightAppRow:
        row = self._app_repo.get(validate_app_id(app_id))
        if row is None:
            raise OctopError.localized(ErrorCode.APP_NOT_FOUND)
        return row

    def permission_for(self, row: AppRecord, user_id: int, *, is_admin: bool) -> str | None:
        if is_admin or row.owner_user_id == user_id:
            return "edit_structure"
        return self._app_repo.permission_for(row.app_id, user_id)

    def require(
        self,
        row: AppRecord,
        user_id: int,
        needed: str,
        *,
        is_admin: bool,
    ) -> str:
        granted = self.permission_for(row, user_id, is_admin=is_admin)
        if granted is None or PERMISSION_RANK.get(granted, 0) < PERMISSION_RANK[needed]:
            raise OctopError.localized(ErrorCode.APP_FORBIDDEN)
        return granted

    def list_for_user(self, user_id: int, *, is_admin: bool) -> list[tuple[LightAppRow, str]]:
        if is_admin:
            return [(row, "edit_structure") for row in self._app_repo.list_all()]
        entries: list[tuple[LightAppRow, str]] = [
            (row, "edit_structure") for row in self._app_repo.list_owned(user_id)
        ]
        for row in self._app_repo.list_shared(user_id):
            permission = self._app_repo.permission_for(row.app_id, user_id)
            if permission is not None:
                entries.append((row, permission))
        entries.sort(key=lambda item: item[0].updated_at, reverse=True)
        return entries

    def view(
        self,
        row: AppRecord,
        permission: str,
        *,
        owners: dict[int, str] | None = None,
    ) -> AppView:
        owner_names = owners if owners is not None else {}
        if row.owner_user_id not in owner_names:
            owner = self._user_repo.get(row.owner_user_id)
            owner_names[row.owner_user_id] = owner.username if owner is not None else ""
        agent = self._agent_repo.get(row.agent_id)
        return AppView(
            app_id=row.app_id,
            name=row.name,
            description=row.description,
            agent_id=row.agent_id,
            agent_name=agent.name if agent is not None else "",
            owner_user_id=row.owner_user_id,
            owner_username=owner_names[row.owner_user_id],
            permission=permission,
            row_count=row.row_count,
            html_bytes=row.html_bytes,
            data_bytes=row.data_bytes,
            created_at=row.created_at,
            updated_at=row.updated_at,
            capabilities=tuple(app_capabilities.describe(row.capabilities_json)),
        )

    def capabilities(self, row: AppRecord) -> frozenset[str]:
        """The page actions this app currently allows."""
        return app_capabilities.parse(row.capabilities_json)

    def capability_list(self, row: AppRecord) -> list[str]:
        return sorted(self.capabilities(row))

    def set_capabilities(self, row: AppRecord, values: Iterable[str]) -> LightAppRow:
        """Replace the page capability manifest; requires edit_structure upstream."""
        clean = app_capabilities.validate(values)
        self._app_repo.set_capabilities(row.app_id, app_capabilities.dump(clean))
        return self.get(row.app_id)

    def require_capability(self, row: AppRecord, capability: str) -> None:
        if capability not in self.capabilities(row):
            raise OctopError(
                ErrorCode.APP_CAPABILITY_DENIED,
                f"app does not allow {capability}",
            )

    async def views_for_app_ids(
        self, app_ids: Iterable[str], user_id: int, *, is_admin: bool
    ) -> list[AppView]:
        """Views for *app_ids* the user may open, preserving their order.

        Ids that no longer resolve (deleted app) or that the user cannot see are
        skipped. Used by the chat dock, whose ids come from the transcript.
        """
        owners: dict[int, str] = {}
        candidates: list[tuple[AppRecord, str]] = []
        for app_id in app_ids:
            try:
                row = self.get(app_id)
            except OctopError:
                continue
            permission = self.permission_for(row, user_id, is_admin=is_admin)
            if permission is None:
                continue
            candidates.append((row, permission))
        # Preview reads hit the workspace; do them concurrently so the chat
        # dock does not wait for one remote round trip per app.
        previews = await asyncio.gather(*(self.has_preview(row) for row, _permission in candidates))
        return [
            replace(
                self.view(row, permission, owners=owners),
                has_preview=preview,
            )
            for (row, permission), preview in zip(candidates, previews, strict=True)
        ]

    async def list_views_with_stats(self, user_id: int, *, is_admin: bool) -> list[AppView]:
        """List views, backfilling the cached file sizes for apps that predate them.

        Apps written before the file-size columns existed carry cached sizes of
        zero even though their files are on disk; listing the app directory
        once repairs the cache instead of showing them as "no interface yet".
        The preview flag is read from the workspace as well: the cover is an
        optional file, not a column.
        """
        owners: dict[int, str] = {}
        out: list[AppView] = []
        for row, permission in self.list_for_user(user_id, is_admin=is_admin):
            view = self.view(row, permission, owners=owners)
            if not (view.html_bytes and view.data_bytes):
                html_bytes, data_bytes = await self._file_stats(row)
                if (html_bytes, data_bytes) != (view.html_bytes, view.data_bytes):
                    view = replace(view, html_bytes=html_bytes, data_bytes=data_bytes)
            out.append(replace(view, has_preview=await self.has_preview(row)))
        return out

    async def _file_stats(self, row: AppRecord) -> tuple[int, int]:
        """Cached (html_bytes, data_bytes), refreshed from the workspace when unknown.

        ``data_bytes`` covers every table's CSV, matching the aggregate the
        registry maintains on writes.
        """
        ws = await self._workspace(row.agent_id)
        sizes = await self._dir_sizes(ws, row.app_id)
        html_bytes = sizes.get(FILE_HTML, 0)
        if row.storage_version >= 2:
            # Rows live in the DB; the CSV is a create-time snapshot and must
            # not overwrite the live data_bytes cache.
            if html_bytes != row.html_bytes:
                self._app_repo.set_file_stats(
                    row.app_id,
                    html_bytes=html_bytes,
                    data_bytes=row.data_bytes,
                )
            return html_bytes, row.data_bytes
        data_bytes = 0
        for table in self.list_tables(row):
            _, data_file = table_file_names(table.table_key)
            size = sizes.get(data_file, 0)
            data_bytes += size
            registered = self._app_repo.get_table(row.app_id, table.table_key)
            if registered is not None and registered.data_bytes != size:
                self._app_repo.set_table_stats(
                    row.app_id,
                    table.table_key,
                    row_count=registered.row_count,
                    data_bytes=size,
                )
        if html_bytes != row.html_bytes or data_bytes != row.data_bytes:
            self._app_repo.set_file_stats(
                row.app_id,
                html_bytes=html_bytes,
                data_bytes=data_bytes,
            )
        return html_bytes, data_bytes

    async def _dir_sizes(self, ws: BackendWorkspace, app_id: str) -> dict[str, int]:
        """File sizes of the app directory, keyed by file name.

        Table files live in a ``tables/`` subdirectory and are keyed by their
        path relative to the app directory so they do not collide with the
        app's own files.
        """
        sizes: dict[str, int] = {}
        for prefix in ("", f"{TABLES_DIR}/"):
            result = await ws.als(
                app_path(app_id, prefix.rstrip("/")) if prefix else app_dir(app_id)
            )
            entries = list(getattr(result, "entries", None) or []) if result is not None else []
            for entry in entries:
                if _entry_field(entry, "is_dir"):
                    continue
                path = str(_entry_field(entry, "path") or "").replace("\\", "/")
                name = path.rsplit("/", 1)[-1]
                if prefix:
                    name = f"{prefix}{name}"
                size = _entry_field(entry, "size")
                if name and size is not None:
                    sizes[name] = int(size)
        return sizes

    def list_for_agent(self, agent_id: str, owner_user_id: int) -> list[LightAppRow]:
        return self._app_repo.list_for_agent(agent_id, owner_user_id)

    def is_agent_owner(self, agent_id: str, user_id: int) -> bool:
        agent = self._agent_repo.get(agent_id)
        return agent is not None and agent.user_id == user_id

    def is_agent_accessible(self, agent_id: str, user_id: int) -> bool:
        """True when the user may use this agent at all (owner or shared)."""
        agent = self._agent_repo.get(agent_id)
        if agent is None:
            return False
        if agent.user_id == user_id:
            return True
        return bool(getattr(agent, "is_shared", False))

    def apps_for_agent(self, agent_id: str) -> list[LightAppRow]:
        """Every app hosted in an agent's workspace, whoever created it."""
        return self._app_repo.list_by_agent(agent_id)

    async def transfer(
        self,
        row: AppRecord,
        *,
        target_agent_id: str,
        actor_user_id: int,
    ) -> LightAppRow:
        """Move an app to another agent's workspace.

        The app files are copied to the target workspace and the metadata
        follows. The source copy is left in place: the backend protocol has no
        portable delete, and it disappears with the source agent anyway.
        """
        if target_agent_id == row.agent_id:
            raise _invalid(
                ErrorCode.APP_SOURCE_INVALID,
                "the app already belongs to that agent",
            )
        if not self.is_agent_accessible(target_agent_id, actor_user_id):
            raise _invalid(
                ErrorCode.APP_SOURCE_INVALID,
                f"target agent is not accessible: {target_agent_id}",
            )
        source_ws = await self._workspace(row.agent_id)
        target_ws = await self._workspace(target_agent_id)
        async with _lock_for(row.app_id):
            # Copy and metadata move together so an append cannot land in the
            # source after the app has already moved.
            await self._copy_app_files(source_ws, target_ws, row.app_id)
            self._app_repo.set_agent_id(row.app_id, target_agent_id)
            self._app_repo.touch(row.app_id)
        moved = self._app_repo.get(row.app_id)
        if moved is None:
            raise OctopError.localized(ErrorCode.APP_NOT_FOUND)
        return moved

    async def _copy_app_files(
        self,
        source_ws: BackendWorkspace,
        target_ws: BackendWorkspace,
        app_id: str,
    ) -> None:
        """Copy the app directory (and its ``tables/`` subdirectory) between workspaces."""
        for prefix in ("", f"{TABLES_DIR}/", f"{ASSETS_DIR}/"):
            source_dir = app_path(app_id, prefix.rstrip("/")) if prefix else app_dir(app_id)
            target_dir = app_path(app_id, prefix.rstrip("/")) if prefix else app_dir(app_id)
            result = await source_ws.als(source_dir)
            entries = list(getattr(result, "entries", None) or []) if result is not None else []
            for entry in entries:
                if _entry_field(entry, "is_dir"):
                    continue
                path = str(_entry_field(entry, "path") or "").replace("\\", "/")
                name = path.rsplit("/", 1)[-1]
                if not name or name.startswith("."):
                    continue
                # Read through the known directory rather than the entry's own
                # path: backends report different shapes (relative vs root).
                data = await source_ws.adownload_bytes(f"{source_dir}/{name}")
                if data is None:
                    continue
                await target_ws.aupload_bytes(f"{target_dir}/{name}", data)

    # ----- workspace helpers -----

    async def _workspace(self, agent_id: str) -> BackendWorkspace:
        ws = await asyncio.to_thread(self._workspaces.workspace_for_agent, agent_id)
        if ws is None:
            raise OctopError.localized(ErrorCode.AGENT_NOT_FOUND)
        return ws

    async def _read_schema(
        self,
        ws: BackendWorkspace,
        app_id: str,
        table_key: str = MAIN_TABLE_KEY,
    ) -> Schema:
        app = self._app_repo.get(app_id)
        if app is not None and app.storage_version >= 2:
            stored = self._app_repo.get_table_schema(app_id, table_key)
            if stored is not None:
                return parse_schema_payload(stored[0])
        return await self._read_schema_file(ws, app_id, table_key)

    async def _read_schema_file(
        self,
        ws: BackendWorkspace,
        app_id: str,
        table_key: str = MAIN_TABLE_KEY,
    ) -> Schema:
        await self._recover(ws, app_id)
        schema_file, _ = table_file_names(table_key)
        text = await ws.aread_text(app_path(app_id, schema_file))
        if text is None:
            raise _invalid(
                ErrorCode.APP_SCHEMA_INVALID,
                f"schema file for table {table_key!r} is missing",
            )
        return parse_schema_payload(text)

    async def _read_rows_file(
        self,
        ws: BackendWorkspace,
        app_id: str,
        schema: Schema,
        table_key: str = MAIN_TABLE_KEY,
    ) -> list[dict[str, str]]:
        _, data_file = table_file_names(table_key)
        text = await ws.aread_text(app_path(app_id, data_file))
        if text is None:
            # aread_text is fail-soft: None also covers transient backend
            # errors. Treating that as an empty file would overwrite every
            # existing row on the next append.
            raise OctopError(
                ErrorCode.INTERNAL_ERROR,
                f"failed to read app data file for table {table_key!r}",
            )
        return parse_csv(text, schema)

    async def _ensure_db_rows(self, row: AppRecord) -> bool:
        """Migrate a legacy CSV app into the DB on first access.

        Returns True when the app is on DB storage. A migration failure keeps
        the app on CSV storage (the caller falls back) rather than losing rows.
        """
        if row.storage_version >= 2:
            return True
        async with _lock_for(row.app_id):
            current = self._app_repo.get(row.app_id)
            if current is None:
                raise OctopError.localized(ErrorCode.APP_NOT_FOUND)
            if current.storage_version >= 2:
                return True
            try:
                ws = await self._workspace(current.agent_id)
                self._ensure_main_table(current)
                for table in self.list_tables(current):
                    schema = await self._read_schema_file(ws, current.app_id, table.table_key)
                    rows = await self._read_rows_file(
                        ws,
                        current.app_id,
                        schema,
                        table.table_key,
                    )
                    self._app_repo.set_table_schema(
                        current.app_id,
                        table.table_key,
                        schema.to_json(),
                        schema.version,
                    )
                    self._app_repo.replace_data_rows(
                        current.app_id,
                        table.table_key,
                        rows,
                        source="migration",
                    )
                self._app_repo.set_storage_version(current.app_id, 2)
                await self._refresh_manifest(ws, current.app_id)
                return True
            except Exception:
                logger.warning(
                    "light app %s migration to DB storage failed; keeping CSV",
                    current.app_id,
                    exc_info=True,
                )
                return False

    def _require_live(self, app_id: str) -> None:
        if self._app_repo.get(app_id) is None:
            # A stale request re-creates the lock in ``_lock_for``; drop it so
            # deleted app ids do not accumulate entries.
            _forget_lock(app_id)
            raise OctopError.localized(ErrorCode.APP_NOT_FOUND)

    async def _write_atomic(
        self,
        ws: BackendWorkspace,
        app_id: str,
        files: dict[str, str],
    ) -> None:
        """Stage app files, then promote them as one recoverable group.

        A crash while staging leaves the previous files untouched; a crash
        during promotion leaves the journal so the next read finishes it.
        """
        if not files or not all(_is_atomic_app_file(name) for name in files):
            raise ValueError("unsupported atomic app files")
        for filename, content in files.items():
            await ws.awrite_text(app_path(app_id, filename + STAGED_SUFFIX), content)
        await ws.awrite_text(
            app_path(app_id, FILE_PENDING),
            json.dumps({"files": list(files)}, ensure_ascii=False),
        )
        await self._promote(ws, app_id, list(files))
        await self._clear_journal(ws, app_id)

    async def _recover(self, ws: BackendWorkspace, app_id: str) -> None:
        files = await self._pending_files(ws, app_id)
        if files is None:
            return
        await self._promote(ws, app_id, files)
        await self._clear_journal(ws, app_id)

    async def _pending_files(self, ws: BackendWorkspace, app_id: str) -> list[str] | None:
        marker = app_path(app_id, FILE_PENDING)
        if not await ws.aexists(marker):
            return None
        text = await ws.aread_text(marker)
        if text is None:
            raise OctopError(ErrorCode.INTERNAL_ERROR, "failed to read app update journal")
        names: list[str] = []
        try:
            payload = json.loads(text)
        except ValueError:
            payload = None
        files = payload.get("files") if isinstance(payload, dict) else None
        if isinstance(files, list):
            for item in files:
                if not isinstance(item, str):
                    names = []
                    break
                names.append(item)
        if not names or not all(_is_atomic_app_file(name) for name in names):
            # The journal is written after staging and before promotion, so an
            # unreadable journal means no target was touched.
            await self._clear_journal(ws, app_id)
            return None
        return names

    async def _promote(self, ws: BackendWorkspace, app_id: str, files: list[str]) -> None:
        for filename in files:
            staged = app_path(app_id, filename + STAGED_SUFFIX)
            if not await ws.aexists(staged):
                continue  # promoted by an earlier attempt
            content = await ws.aread_text(staged)
            if content is None:
                raise OctopError(ErrorCode.INTERNAL_ERROR, "failed to read staged app file")
            await ws.awrite_text(app_path(app_id, filename), content)
            try:
                await ws.adelete(staged)
            except Exception:
                # The target is already committed; a leftover staged file is
                # harmless and gets overwritten by the next write.
                logger.warning("failed to remove staged app file: %s", staged, exc_info=True)

    async def _clear_journal(self, ws: BackendWorkspace, app_id: str) -> None:
        try:
            await ws.adelete(app_path(app_id, FILE_PENDING))
        except Exception:
            logger.warning("failed to clear app update journal: %s", app_id, exc_info=True)

    @staticmethod
    def _manifest_text(row: AppRecord, tables: list[dict[str, Any]]) -> str:
        main_version = next(
            (table["schema_version"] for table in tables if table.get("is_main")),
            1,
        )
        manifest = {
            "app_id": row.app_id,
            "name": row.name,
            "description": row.description,
            "agent_id": row.agent_id,
            "owner_user_id": row.owner_user_id,
            "row_count": row.row_count,
            "schema_version": main_version,
            "data_storage": ("control-plane-db" if row.storage_version >= 2 else "workspace-csv"),
            "created_at": row.created_at,
            "updated_at": row.updated_at,
            "tables": tables,
        }
        return json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"

    async def _refresh_manifest(self, ws: BackendWorkspace, app_id: str) -> None:
        row = self._app_repo.get(app_id)
        if row is None:
            return
        tables: list[dict[str, Any]] = []
        for table in self.list_tables(row):
            try:
                version: int | None = (await self._read_schema(ws, app_id, table.table_key)).version
            except OctopError:
                version = None
            tables.append(
                {
                    "key": table.table_key,
                    "label": table.label,
                    "row_count": table.row_count,
                    "schema_version": version,
                    "is_main": table.is_main,
                    "public_visible": table.public_visible,
                }
            )
        try:
            await ws.awrite_text(
                app_path(app_id, FILE_MANIFEST),
                self._manifest_text(row, tables),
            )
        except Exception:
            logger.warning("failed to refresh app manifest: %s", app_id, exc_info=True)

    # ----- app templates (system and expert-provided shells) -----

    async def list_templates(
        self, agent_id: str, *, source: str = "all", locale: str = "zh"
    ) -> list[dict[str, Any]]:
        """List system and workspace templates for the creation gallery."""
        if source not in ("all", SYSTEM_SOURCE, AGENT_SOURCE):
            raise _invalid(ErrorCode.APP_NOT_FOUND, f"unknown template source: {source!r}")
        templates: list[dict[str, Any]] = []
        if source in ("all", SYSTEM_SOURCE):
            templates.extend(info.to_dict() for info in list_system_templates(locale))
        if source in ("all", AGENT_SOURCE):
            templates.extend(
                info.to_dict() for info in await self._agent_templates(agent_id, locale)
            )
        return templates

    async def _agent_templates(self, agent_id: str, locale: str) -> list[TemplateInfo]:
        ws = await self._workspace(agent_id)
        result = await ws.aglob("app_templates/*/schema.json")
        matches = list(getattr(result, "matches", None) or []) if result is not None else []
        names: set[str] = set()
        for entry in matches:
            path = str(_entry_field(entry, "path") or "").replace("\\", "/")
            parts = [part for part in path.split("/") if part]
            if len(parts) >= 2 and parts[-1] == "schema.json":
                names.add(parts[-2])
        templates = []
        for name in sorted(names):
            if not name or name.startswith("."):
                continue
            manifest: dict[str, Any] = {}
            manifest_text = await ws.aread_text(f"app_templates/{name}/manifest.json")
            if manifest_text:
                try:
                    payload = json.loads(manifest_text)
                except json.JSONDecodeError:
                    payload = None
                if isinstance(payload, dict):
                    manifest = payload
            templates.append(
                agent_template_info(
                    agent_id=agent_id,
                    template_id=name,
                    manifest=manifest,
                    has_html=await ws.aexists(f"app_templates/{name}/app.html"),
                    locale=locale,
                    has_preview=await ws.aexists(f"app_templates/{name}/{FILE_PREVIEW}"),
                )
            )
        return templates

    async def list_app_templates(self, agent_id: str) -> list[dict[str, Any]]:
        """Legacy shape: the agent's workspace templates only."""
        return [
            {"template": info.template_id, "label": info.name, "has_html": info.has_html}
            for info in await self._agent_templates(agent_id, "zh")
        ]

    async def create_from_template(
        self,
        *,
        agent_id: str,
        owner_user_id: int,
        template: str,
        source: str = AGENT_SOURCE,
        name: str | None = None,
        description: str = "",
        capabilities: Iterable[str] | None = None,
    ) -> LightAppRow:
        """Create an empty app from a system or workspace shell (schema + HTML)."""
        clean = clean_template_id(template)
        schema_text: str
        html: str | None
        if source == SYSTEM_SOURCE:
            schema_text, html = read_system_template(clean)
        elif source == AGENT_SOURCE:
            ws = await self._workspace(agent_id)
            stored_schema = await ws.aread_text(f"app_templates/{clean}/schema.json")
            if stored_schema is None:
                raise _invalid(ErrorCode.APP_NOT_FOUND, f"unknown app template: {clean}")
            schema_text = stored_schema
            html = await ws.aread_text(f"app_templates/{clean}/app.html")
        else:
            raise _invalid(ErrorCode.APP_NOT_FOUND, f"unknown template source: {source!r}")
        row = await self.create(
            agent_id=agent_id,
            owner_user_id=owner_user_id,
            name=(name or clean).strip() or clean,
            description=description,
            columns=schema_text,
            capabilities=capabilities,
        )
        if html:
            try:
                await self.write_html(row, html)
            except Exception:
                # Do not leave an empty app behind when the template shell is
                # rejected by the HTML sandbox rules.
                await self.delete(row)
                raise
        preview = await self.template_preview(agent_id, clean, source)
        if preview:
            try:
                await self.write_preview(row, preview)
            except Exception:
                # A template without a usable cover still yields a usable app.
                logger.warning(
                    "failed to copy template preview for %s: %s",
                    clean,
                    exc_info=True,
                )
        return row

    # ----- create / metadata -----

    async def create(
        self,
        *,
        agent_id: str,
        owner_user_id: int,
        name: str,
        description: str = "",
        columns: object,
        capabilities: Iterable[str] | None = None,
    ) -> LightAppRow:
        clean_name = validate_name(name)
        clean_description = validate_description(description)
        schema = parse_schema_payload(columns)
        selected_capabilities = (
            app_capabilities.NEW_APP_CAPABILITIES
            if capabilities is None
            else app_capabilities.validate(capabilities)
        )
        if self._app_repo.count_for_owner(owner_user_id) >= MAX_APPS_PER_OWNER:
            raise _invalid(
                ErrorCode.APP_LIMIT_EXCEEDED,
                f"at most {MAX_APPS_PER_OWNER} apps per user",
                limit=MAX_APPS_PER_OWNER,
            )
        ws = await self._workspace(agent_id)
        app_id = self._app_repo.allocate_id()
        row = self._app_repo.create(
            app_id=app_id,
            agent_id=agent_id,
            owner_user_id=owner_user_id,
            name=clean_name,
            description=clean_description,
            storage_version=2,
            capabilities_json=app_capabilities.dump(selected_capabilities),
        )
        try:
            empty_csv = render_csv(schema, [])
            await ws.awrite_text(app_path(app_id, FILE_SCHEMA), schema.to_json())
            await ws.awrite_text(app_path(app_id, FILE_DATA), empty_csv)
            data_bytes = len(empty_csv.encode("utf-8"))
            self._app_repo.create_table(
                app_id,
                MAIN_TABLE_KEY,
                public_visible=True,
            )
            self._app_repo.set_table_schema(
                app_id,
                MAIN_TABLE_KEY,
                schema.to_json(),
                schema.version,
            )
            self._app_repo.set_table_stats(
                app_id,
                MAIN_TABLE_KEY,
                row_count=0,
                data_bytes=data_bytes,
            )
            await ws.awrite_text(
                app_path(app_id, FILE_MANIFEST),
                self._manifest_text(
                    row,
                    [
                        {
                            "key": MAIN_TABLE_KEY,
                            "label": "",
                            "row_count": 0,
                            "schema_version": schema.version,
                            "is_main": True,
                        }
                    ],
                ),
            )
            self._app_repo.set_file_stats(app_id, html_bytes=0, data_bytes=data_bytes)
        except Exception:
            self._app_repo.delete(app_id)
            try:
                await ws.adelete(app_dir(app_id))
            except Exception:
                logger.warning("failed to clean up app workspace files: %s", app_id, exc_info=True)
            raise
        return row

    # ----- tables -----

    def list_tables(self, row: AppRecord) -> list[AppTableView]:
        """Tables of an app, always including the main table.

        Apps created before multi-table support have no registry row for the
        main table, so it is synthesized from the app row.
        """
        registered = self._app_repo.list_tables(row.app_id)
        main = next((item for item in registered if item.table_key == MAIN_TABLE_KEY), None)
        views = [
            AppTableView(
                table_key=MAIN_TABLE_KEY,
                label=main.label if main is not None else "",
                row_count=main.row_count if main is not None else row.row_count,
                is_main=True,
                public_visible=main.public_visible if main is not None else True,
            )
        ]
        for item in registered:
            if item.table_key == MAIN_TABLE_KEY:
                continue
            views.append(
                AppTableView(
                    table_key=item.table_key,
                    label=item.label,
                    row_count=item.row_count,
                    is_main=False,
                    public_visible=item.public_visible,
                )
            )
        return views

    def _ensure_main_table(self, row: AppRecord) -> None:
        """Register the main table for apps created before multi-table support.

        Their ``light_apps.row_count`` is already the main table's count, so the
        backfilled registry row starts from it and the aggregate stays correct.
        """
        if self._app_repo.get_table(row.app_id, MAIN_TABLE_KEY) is not None:
            return
        self._app_repo.create_table(row.app_id, MAIN_TABLE_KEY, public_visible=True)
        self._app_repo.set_table_stats(
            row.app_id,
            MAIN_TABLE_KEY,
            row_count=row.row_count,
            data_bytes=row.data_bytes,
        )

    def _require_table(self, row: AppRecord, table_key: str) -> str:
        """Validate *table_key* exists in the app's registry."""
        key = validate_table_key(table_key)
        if key == MAIN_TABLE_KEY:
            return key
        if self._app_repo.get_table(row.app_id, key) is None:
            raise _invalid(
                ErrorCode.APP_SCHEMA_INVALID,
                f"unknown table: {key!r}",
                table=key,
            )
        return key

    async def create_table(
        self,
        row: AppRecord,
        *,
        table_key: str,
        label: str = "",
        columns: object,
        public_visible: bool = False,
    ) -> AppTableView:
        """Add a table with its own columns and an empty CSV."""
        key = validate_table_key(table_key)
        if key == MAIN_TABLE_KEY:
            raise _invalid(
                ErrorCode.APP_SCHEMA_INVALID,
                "the main table already exists",
                table=key,
            )
        schema = parse_schema_payload(columns)
        clean_label = label.strip()[:MAX_LABEL_LEN]
        async with _lock_for(row.app_id):
            self._require_live(row.app_id)
            self._ensure_main_table(row)
            if self._app_repo.get_table(row.app_id, key) is not None:
                raise _invalid(
                    ErrorCode.APP_SCHEMA_INVALID,
                    f"table already exists: {key!r}",
                    table=key,
                )
            if self._app_repo.count_tables(row.app_id) >= MAX_TABLES_PER_APP:
                raise _invalid(
                    ErrorCode.APP_LIMIT_EXCEEDED,
                    f"at most {MAX_TABLES_PER_APP} tables per app",
                    limit=MAX_TABLES_PER_APP,
                )
            ws = await self._workspace(row.agent_id)
            schema_file, data_file = table_file_names(key)
            empty_csv = render_csv(schema, [])
            await self._write_atomic(
                ws,
                row.app_id,
                {schema_file: schema.to_json(), data_file: empty_csv},
            )
            self._app_repo.create_table(
                row.app_id,
                key,
                clean_label,
                public_visible=public_visible,
            )
            self._app_repo.set_table_schema(row.app_id, key, schema.to_json(), schema.version)
            self._app_repo.set_table_stats(
                row.app_id,
                key,
                row_count=0,
                data_bytes=len(empty_csv.encode("utf-8")),
            )
            self._app_repo.refresh_app_totals(row.app_id)
        return AppTableView(
            table_key=key,
            label=clean_label,
            row_count=0,
            is_main=False,
            public_visible=public_visible,
        )

    async def set_table_visibility(
        self,
        row: AppRecord,
        *,
        table: str,
        public_visible: bool,
    ) -> AppTableView:
        """Expose or hide one table on the app's public read-only link."""
        key = self._require_table(row, table)
        async with _lock_for(row.app_id):
            self._require_live(row.app_id)
            current = self._app_repo.get(row.app_id) or row
            self._ensure_main_table(current)
            self._app_repo.set_table_visibility(
                current.app_id,
                key,
                public_visible=public_visible,
            )
            ws = await self._workspace(current.agent_id)
            await self._refresh_manifest(ws, current.app_id)
        view = next(item for item in self.list_tables(current) if item.table_key == key)
        return view

    def is_table_public(self, row: AppRecord, table: str) -> bool:
        """Return whether *table* is exposed on the public link."""
        try:
            key = validate_table_key(table)
        except OctopError:
            return False
        for view in self.list_tables(row):
            if view.table_key == key:
                return view.public_visible
        return False

    async def update_metadata(
        self,
        row: AppRecord,
        *,
        name: str | None = None,
        description: str | None = None,
    ) -> LightAppRow:
        clean_name = validate_name(name) if name is not None else None
        clean_description = validate_description(description) if description is not None else None
        async with _lock_for(row.app_id):
            self._require_live(row.app_id)
            self._app_repo.update_metadata(
                row.app_id,
                name=clean_name,
                description=clean_description,
            )
            ws = await self._workspace(row.agent_id)
            await self._refresh_manifest(ws, row.app_id)
            return self.get(row.app_id)

    async def delete(self, row: AppRecord) -> None:
        try:
            async with _lock_for(row.app_id):
                ws = await asyncio.to_thread(self._workspaces.workspace_for_agent, row.agent_id)
                self._app_repo.delete(row.app_id)
                # ON DELETE CASCADE covers this, but the explicit delete keeps
                # history cleanup working on databases without FK enforcement.
                self._app_repo.delete_assistant_messages(row.app_id)
                if self._secret_repo is not None:
                    self._secret_repo.delete(self._publication_secret_key(row.app_id))
                if ws is None:
                    return
                try:
                    await ws.adelete(app_dir(row.app_id))
                except Exception:
                    logger.warning(
                        "failed to delete app workspace files: %s",
                        row.app_id,
                        exc_info=True,
                    )
        finally:
            _forget_lock(row.app_id)

    # ----- UI / data -----

    async def read_html(self, row: AppRecord) -> str | None:
        ws = await self._workspace(row.agent_id)
        return await ws.aread_text(app_path(row.app_id, FILE_HTML))

    async def has_html(self, row: AppRecord) -> bool:
        ws = await self._workspace(row.agent_id)
        return await ws.aexists(app_path(row.app_id, FILE_HTML))

    async def list_files(self, row: AppRecord) -> list[AppFile]:
        """List the app's workspace files with their sizes.

        The known app files are listed even on backends that report no size;
        tables contribute one CSV and one schema file each.
        """
        ws = await self._workspace(row.agent_id)
        sizes = await self._dir_sizes(ws, row.app_id)
        names = [FILE_HTML, FILE_DATA, FILE_SCHEMA, FILE_MANIFEST]
        for table in self.list_tables(row):
            if table.is_main:
                continue
            schema_file, data_file = table_file_names(table.table_key)
            names.extend((schema_file, data_file))
        names.extend(f"{ASSETS_DIR}/{asset}" for asset in await self.list_assets(row))
        if await ws.aexists(app_path(row.app_id, FILE_PREVIEW)):
            names.append(FILE_PREVIEW)
        return [
            AppFile(
                name=name,
                path=app_path(row.app_id, name),
                size=sizes.get(name),
            )
            for name in names
        ]

    # ----- preview -----

    async def write_preview(self, row: AppRecord, svg: str) -> int:
        """Store the app's cover image and return its size in bytes.

        The agent draws the preview as SVG, so the markup is validated before
        it reaches the workspace: no scripts, no inline handlers, no remote
        references.
        """
        try:
            content = validate_preview_svg(svg)
        except ValueError as exc:
            raise _invalid(ErrorCode.APP_SOURCE_INVALID, str(exc)) from exc
        ws = await self._workspace(row.agent_id)
        async with _lock_for(row.app_id):
            self._require_live(row.app_id)
            await ws.awrite_text(app_path(row.app_id, FILE_PREVIEW), content)
            self._app_repo.touch(row.app_id)
        return len(content.encode("utf-8"))

    async def read_preview(self, row: AppRecord) -> str | None:
        """Return the app's cover markup, or ``None`` when it has none."""
        try:
            ws = await self._workspace(row.agent_id)
        except OctopError:
            # The owning agent is gone: the app has no cover any more.
            return None
        return await ws.aread_text(app_path(row.app_id, FILE_PREVIEW))

    async def has_preview(self, row: AppRecord) -> bool:
        """Whether the app has a cover image."""
        try:
            ws = await self._workspace(row.agent_id)
        except OctopError:
            return False
        return await ws.aexists(app_path(row.app_id, FILE_PREVIEW))

    async def template_preview(self, agent_id: str, template_id: str, source: str) -> str | None:
        """Return a template's cover markup, or ``None`` when it has none."""
        clean = clean_template_id(template_id)
        if source == SYSTEM_SOURCE:
            path = SYSTEM_TEMPLATE_ROOT / clean / FILE_PREVIEW
            try:
                return path.read_text(encoding="utf-8") if path.is_file() else None
            except OSError:
                return None
        if source == AGENT_SOURCE:
            ws = await self._workspace(agent_id)
            return await ws.aread_text(f"app_templates/{clean}/{FILE_PREVIEW}")
        raise _invalid(ErrorCode.APP_NOT_FOUND, f"unknown template source: {source!r}")

    # ----- assets -----

    async def save_asset(self, row: AppRecord, name: str, data: bytes) -> str:
        """Store an image next to the app and return its name.

        The app page may not load remote images, so covers and icons live in
        the app's own directory and are served through the app ACL.
        """
        clean = name.strip()
        if not ASSET_NAME_RE.match(clean) or ".." in clean:
            raise _invalid(
                ErrorCode.APP_SOURCE_INVALID,
                f"invalid asset name: {name!r}",
            )
        if not data:
            raise _invalid(ErrorCode.APP_SOURCE_INVALID, "asset is empty")
        if len(data) > MAX_ASSET_BYTES:
            raise _invalid(
                ErrorCode.APP_SOURCE_INVALID,
                f"asset exceeds the {MAX_ASSET_BYTES // (1024 * 1024)} MB limit",
            )
        if _sniff_asset_mime(data) is None:
            raise _invalid(
                ErrorCode.APP_SOURCE_INVALID,
                "asset must be a PNG, JPEG, WebP or GIF image",
            )
        ws = await self._workspace(row.agent_id)
        async with _lock_for(row.app_id):
            self._require_live(row.app_id)
            await ws.aupload_bytes(app_path(row.app_id, f"{ASSETS_DIR}/{clean}"), data)
            self._app_repo.touch(row.app_id)
        return clean

    async def read_asset(self, row: AppRecord, name: str) -> tuple[str, bytes]:
        """Read one asset and its media type."""
        clean = name.strip()
        if not ASSET_NAME_RE.match(clean) or ".." in clean:
            raise _invalid(ErrorCode.APP_SOURCE_INVALID, f"invalid asset name: {name!r}")
        ws = await self._workspace(row.agent_id)
        data = await ws.adownload_bytes(app_path(row.app_id, f"{ASSETS_DIR}/{clean}"))
        if data is None:
            raise _invalid(ErrorCode.APP_SOURCE_INVALID, f"asset not found: {clean}")
        mime = _sniff_asset_mime(data) or "application/octet-stream"
        return mime, data

    async def list_assets(self, row: AppRecord) -> list[str]:
        """Names of the assets stored next to the app."""
        ws = await self._workspace(row.agent_id)
        result = await ws.als(app_path(row.app_id, ASSETS_DIR))
        entries = list(getattr(result, "entries", None) or []) if result is not None else []
        names: list[str] = []
        for entry in entries:
            if _entry_field(entry, "is_dir"):
                continue
            path = str(_entry_field(entry, "path") or "").replace("\\", "/")
            name = path.rsplit("/", 1)[-1]
            if name and ASSET_NAME_RE.match(name):
                names.append(name)
        return sorted(names)

    async def write_html(self, row: AppRecord, html: str) -> int:
        content = validate_html(html)
        size = len(content.encode("utf-8"))
        async with _lock_for(row.app_id):
            self._require_live(row.app_id)
            ws = await self._workspace(row.agent_id)
            await ws.awrite_text(app_path(row.app_id, FILE_HTML), content)
            self._app_repo.touch(row.app_id)
            self._app_repo.set_file_stats(row.app_id, html_bytes=size)
            await self._refresh_manifest(ws, row.app_id)
        return size

    async def read_schema(self, row: AppRecord, table_key: str = MAIN_TABLE_KEY) -> Schema:
        key = self._require_table(row, table_key)
        async with _lock_for(row.app_id):
            self._require_live(row.app_id)
            ws = await self._workspace(row.agent_id)
            return await self._read_schema(ws, row.app_id, key)

    async def read_rows(
        self,
        row: AppRecord,
        *,
        limit: int = MAX_ROW_READ,
        offset: int = 0,
        table: str = MAIN_TABLE_KEY,
        redact_pii: bool = False,
    ) -> tuple[Schema, list[dict[str, str]], int]:
        schema, rows, _meta, total = await self.read_rows_detailed(
            row,
            limit=limit,
            offset=offset,
            table=table,
            redact_pii=redact_pii,
        )
        return schema, rows, total

    async def read_rows_detailed(
        self,
        row: AppRecord,
        *,
        limit: int = MAX_ROW_READ,
        offset: int = 0,
        table: str = MAIN_TABLE_KEY,
        redact_pii: bool = False,
    ) -> tuple[Schema, list[dict[str, str]], list[dict[str, Any]], int]:
        """Read a page plus per-row ``row_id``/``version`` metadata."""
        key = self._require_table(row, table)
        safe_limit = max(1, min(int(limit), MAX_ROW_READ))
        safe_offset = max(0, int(offset))
        await self._ensure_db_rows(row)
        async with _lock_for(row.app_id):
            self._require_live(row.app_id)
            current = self._app_repo.get(row.app_id) or row
            ws = await self._workspace(current.agent_id)
            schema = await self._read_schema(ws, current.app_id, key)
            if current.storage_version >= 2:
                data_page, total = self._app_repo.list_data_rows(
                    current.app_id,
                    key,
                    limit=safe_limit,
                    offset=safe_offset,
                )
                rows_page = _normalize_rows(schema, [item.values for item in data_page])
                schema, rows_page = _apply_pii(schema, rows_page, redact_pii=redact_pii)
                return (
                    schema,
                    rows_page,
                    [
                        {
                            "row_id": item.row_id,
                            "version": item.version,
                            "source": item.source,
                        }
                        for item in data_page
                    ],
                    total,
                )
            rows = await self._read_rows_file(ws, current.app_id, schema, key)
            total = len(rows)
            # ``row_count`` is a list-view cache; reconcile it when a file-level
            # edit (or an interrupted append) made it drift from the CSV.
            # Reads must not bump updated_at or the list re-sorts on every GET.
            cached = self._app_repo.get_table(current.app_id, key)
            if cached is not None:
                if total != cached.row_count:
                    self._app_repo.set_table_row_count(current.app_id, key, total)
                    self._app_repo.refresh_app_totals(current.app_id)
            elif key == MAIN_TABLE_KEY and total != current.row_count:
                # Legacy app: only ``light_apps.row_count`` tracks the main CSV.
                self._app_repo.set_row_count(current.app_id, total, touch=False)
            rows_page = rows[safe_offset : safe_offset + safe_limit]
            schema, rows_page = _apply_pii(schema, rows_page, redact_pii=redact_pii)
            return (
                schema,
                rows_page,
                [{"row_id": "", "version": 1, "source": "manual"} for _ in rows_page],
                total,
            )

    async def query_rows(
        self,
        row: AppRecord,
        *,
        table: str = MAIN_TABLE_KEY,
        filters: Mapping[str, object] | None = None,
        limit: int = MAX_ROW_READ,
        offset: int = 0,
        redact_pii: bool = False,
    ) -> tuple[Schema, list[dict[str, str]], int]:
        schema, rows, _meta, total = await self.query_rows_detailed(
            row,
            table=table,
            filters=filters,
            limit=limit,
            offset=offset,
            redact_pii=redact_pii,
        )
        return schema, rows, total

    async def query_rows_detailed(
        self,
        row: AppRecord,
        *,
        table: str = MAIN_TABLE_KEY,
        filters: Mapping[str, object] | None = None,
        limit: int = MAX_ROW_READ,
        offset: int = 0,
        redact_pii: bool = False,
    ) -> tuple[Schema, list[dict[str, str]], list[dict[str, Any]], int]:
        """Read filtered rows plus per-row ``row_id``/``version`` metadata.

        Filter values compare case-insensitively against the stored cell text;
        unknown filter columns raise instead of silently matching nothing.
        """
        key = self._require_table(row, table)
        clean_filters = {str(col).strip(): str(val) for col, val in (filters or {}).items()}
        safe_limit = max(1, min(int(limit), MAX_ROW_READ))
        safe_offset = max(0, int(offset))
        await self._ensure_db_rows(row)
        async with _lock_for(row.app_id):
            self._require_live(row.app_id)
            current = self._app_repo.get(row.app_id) or row
            ws = await self._workspace(current.agent_id)
            schema = await self._read_schema(ws, current.app_id, key)
            for column in clean_filters:
                if schema.column(column) is None:
                    raise _invalid(
                        ErrorCode.APP_SCHEMA_INVALID,
                        f"unknown filter column: {column!r}",
                        table=key,
                    )
            if current.storage_version >= 2:
                data_rows = self._app_repo.list_all_data_rows(current.app_id, key)
                rows = _normalize_rows(schema, [item.values for item in data_rows])
                meta = [
                    {
                        "row_id": item.row_id,
                        "version": item.version,
                        "source": item.source,
                    }
                    for item in data_rows
                ]
            else:
                rows = await self._read_rows_file(ws, current.app_id, schema, key)
                meta = [{"row_id": "", "version": 1, "source": "manual"} for _ in rows]
        if clean_filters:
            wanted = {col: val.strip().lower() for col, val in clean_filters.items()}
            keep = [
                index
                for index, item in enumerate(rows)
                if all(item.get(col, "").strip().lower() == val for col, val in wanted.items())
            ]
            rows = [rows[index] for index in keep]
            meta = [meta[index] for index in keep]
        end = safe_offset + safe_limit
        schema, page = _apply_pii(
            schema,
            rows[safe_offset:end],
            redact_pii=redact_pii,
        )
        return schema, page, meta[safe_offset:end], len(rows)

    async def append_row(
        self,
        row: AppRecord,
        values: Mapping[str, object],
        *,
        table: str = MAIN_TABLE_KEY,
        client_request_id: str | None = None,
        source: str = "manual",
        source_id: str | None = None,
        source_key: str | None = None,
        created_by: int | None = None,
    ) -> tuple[int, dict[str, str]]:
        index, stored = await self.append_row_record(
            row,
            values,
            table=table,
            client_request_id=client_request_id,
            source=source,
            source_id=source_id,
            source_key=source_key,
            created_by=created_by,
        )
        return index, stored.values

    async def append_row_record(
        self,
        row: AppRecord,
        values: Mapping[str, object],
        *,
        table: str = MAIN_TABLE_KEY,
        client_request_id: str | None = None,
        source: str = "manual",
        source_id: str | None = None,
        source_key: str | None = None,
        created_by: int | None = None,
    ) -> tuple[int, LightAppDataRow]:
        key = self._require_table(row, table)
        await self._ensure_db_rows(row)
        async with _lock_for(row.app_id):
            self._require_live(row.app_id)
            self._ensure_main_table(row)
            current = self._app_repo.get(row.app_id) or row
            ws = await self._workspace(current.agent_id)
            schema = await self._read_schema(ws, current.app_id, key)
            if current.storage_version >= 2:
                if self._app_repo.count_data_rows(current.app_id, key) >= MAX_ROWS:
                    raise _invalid(
                        ErrorCode.APP_LIMIT_EXCEEDED,
                        f"table has reached the {MAX_ROWS}-row limit",
                        limit=MAX_ROWS,
                    )
                normalized = validate_row(schema, values)
                _reject_unmarked_pii(schema, normalized, table=key)
                table_view = self._app_repo.get_table(current.app_id, key)
                current_bytes = table_view.data_bytes if table_view is not None else 0
                payload_bytes = len(json.dumps(normalized, ensure_ascii=False).encode("utf-8"))
                if current_bytes + payload_bytes > MAX_CSV_BYTES:
                    raise _invalid(
                        ErrorCode.APP_LIMIT_EXCEEDED,
                        f"table data would exceed {MAX_CSV_BYTES // (1024 * 1024)} MB",
                        limit=MAX_CSV_BYTES,
                    )
                index, stored = self._app_repo.append_data_row(
                    current.app_id,
                    key,
                    normalized,
                    source=source,
                    source_id=source_id,
                    source_key=source_key,
                    client_request_id=client_request_id,
                    created_by=created_by,
                )
                await self._refresh_manifest(ws, current.app_id)
                return index, stored
            rows = await self._read_rows_file(ws, current.app_id, schema, key)
            if len(rows) >= MAX_ROWS:
                raise _invalid(
                    ErrorCode.APP_LIMIT_EXCEEDED,
                    f"table has reached the {MAX_ROWS}-row limit",
                    limit=MAX_ROWS,
                )
            normalized = validate_row(schema, values)
            _reject_unmarked_pii(schema, normalized, table=key)
            rows.append(normalized)
            await self._persist_rows(ws, current, key, schema, rows)
            ts = int(time.time())
            return len(rows) - 1, LightAppDataRow(
                row_id="",
                app_id=current.app_id,
                table_key=key,
                values=normalized,
                version=1,
                source=source,
                created_at=ts,
                updated_at=ts,
            )

    async def _persist_rows(
        self,
        ws: BackendWorkspace,
        row: AppRecord,
        key: str,
        schema: Schema,
        rows: list[dict[str, str]],
    ) -> None:
        """Write one table's rows back to its CSV and refresh cached stats."""
        csv_text = render_csv(schema, rows)
        if len(csv_text.encode("utf-8")) > MAX_CSV_BYTES:
            raise _invalid(
                ErrorCode.APP_LIMIT_EXCEEDED,
                f"data file would exceed {MAX_CSV_BYTES // (1024 * 1024)} MB",
                limit=MAX_CSV_BYTES,
            )
        _, data_file = table_file_names(key)
        await self._write_atomic(ws, row.app_id, {data_file: csv_text})
        self._app_repo.set_table_stats(
            row.app_id,
            key,
            row_count=len(rows),
            data_bytes=len(csv_text.encode("utf-8")),
        )
        self._app_repo.refresh_app_totals(row.app_id)
        self._app_repo.touch(row.app_id)
        await self._refresh_manifest(ws, row.app_id)

    async def _persist_schema(
        self,
        ws: BackendWorkspace,
        app_id: str,
        key: str,
        schema: Schema,
    ) -> None:
        """Persist schema metadata (PII flags) without bumping its version."""
        schema_file, _data_file = table_file_names(key)
        schema_text = schema.to_json()
        await self._write_atomic(ws, app_id, {schema_file: schema_text})
        self._app_repo.set_table_schema(app_id, key, schema_text, schema.version)

    async def save_import_file(self, row: AppRecord, *, filename: str, data: bytes) -> str:
        """Store an uploaded import file under ``inbound/imports/`` in the workspace."""
        return await self.save_quick_import_file(row.agent_id, filename=filename, data=data)

    async def save_quick_import_file(self, agent_id: str, *, filename: str, data: bytes) -> str:
        """Store a file before an app exists (the quick-create upload step)."""
        ws = await self._workspace(agent_id)
        path = f"{IMPORT_DIR}/{new_short_id(8)}-{_safe_import_filename(filename)}"
        await ws.aupload_bytes(path, data)
        return path

    async def quick_inspect(
        self,
        agent_id: str,
        *,
        filename: str,
        data: bytes,
        sheet: str | None = None,
    ) -> dict[str, Any]:
        """Inspect an uploaded ledger and suggest a dashboard view."""
        report = inspect_file(filename, data)
        selected = _select_sheet_report(report, sheet)
        columns = _quick_columns(selected)
        schema = parse_schema_payload({"columns": columns})
        title = _file_stem(filename)
        upload_path = await self.save_quick_import_file(agent_id, filename=filename, data=data)
        return {
            "upload_path": upload_path,
            "sheet": selected.get("name", ""),
            "report": report,
            "suggested_view": suggest_view(selected, schema, title=title),
        }

    async def quick_create(
        self,
        *,
        agent_id: str,
        owner_user_id: int,
        path: str,
        columns: Sequence[Mapping[str, Any]],
        view: str,
        name: str,
        description: str = "",
        sheet: str | None = None,
        header_row: int | None = None,
        mapping: Mapping[str, str] | None = None,
        metric_column: str | None = None,
        group_column: str | None = None,
        include_pii: bool = False,
        upsert_key: list[str] | None = None,
    ) -> tuple[LightAppRow, ImportResult]:
        """Create, import and render a dashboard in one transaction-like step."""
        clean_path = _safe_import_path(path)
        if not clean_path.startswith(f"{IMPORT_DIR}/"):
            raise _invalid(
                ErrorCode.APP_ROW_INVALID,
                f"quick import path must live under {IMPORT_DIR}/",
            )
        schema = parse_schema_payload({"columns": list(columns)})
        clean_name = validate_name(name)
        clean_description = validate_description(description)
        row = await self.create(
            agent_id=agent_id,
            owner_user_id=owner_user_id,
            name=clean_name,
            description=clean_description,
            columns={"columns": list(columns)},
        )
        try:
            result = await self.import_from_file(
                row,
                path=clean_path,
                sheet=sheet,
                header_row=header_row,
                mapping=mapping,
                include_pii=include_pii,
                upsert_key=upsert_key,
            )
            if result.imported == 0 and result.updated == 0 and result.errors:
                raise _invalid(ErrorCode.APP_ROW_INVALID, result.errors[0])
            data = await self.read_workspace_file(agent_id, clean_path)
            report = inspect_file(clean_path, data)
            selected = _select_sheet_report(report, sheet)
            spec = suggest_view(
                selected,
                schema,
                view=view,
                title=clean_name,
                metric=metric_column,
                group=group_column,
            )
            html = render_view_html(schema, spec, title=clean_name, description=clean_description)
            await self.write_html(row, html)
        except Exception:
            try:
                await self.delete(row)
            except Exception:
                logger.warning("failed to roll back quick-create app %s", row.app_id, exc_info=True)
            raise
        return row, result

    async def quick_create_multi(
        self,
        *,
        agent_id: str,
        owner_user_id: int,
        path: str,
        sheets: Sequence[Mapping[str, Any]],
        name: str,
        description: str = "",
        include_pii: bool = False,
    ) -> tuple[LightAppRow, ImportResult]:
        """Create one app from several sheets, each landing in its own table."""
        clean_path = _safe_import_path(path)
        if not clean_path.startswith(f"{IMPORT_DIR}/"):
            raise _invalid(
                ErrorCode.APP_ROW_INVALID,
                f"quick import path must live under {IMPORT_DIR}/",
            )
        if not sheets:
            raise _invalid(ErrorCode.APP_ROW_INVALID, "at least one sheet is required")
        if len(sheets) > MAX_TABLES_PER_APP:
            raise _invalid(
                ErrorCode.APP_LIMIT_EXCEEDED,
                f"at most {MAX_TABLES_PER_APP} sheets can be imported at once",
            )
        clean_name = validate_name(name)
        clean_description = validate_description(description)
        parsed: list[dict[str, Any]] = []
        seen_sheets: set[str] = set()
        for index, item in enumerate(sheets):
            sheet_name = str(item.get("sheet") or "").strip()
            if not sheet_name:
                raise _invalid(ErrorCode.APP_ROW_INVALID, "sheet name is required")
            if sheet_name in seen_sheets:
                raise _invalid(
                    ErrorCode.APP_ROW_INVALID,
                    f"sheet selected more than once: {sheet_name!r}",
                )
            seen_sheets.add(sheet_name)
            columns = item.get("columns")
            schema = parse_schema_payload({"columns": columns})
            key = MAIN_TABLE_KEY if index == 0 else f"table_{index + 1}"
            parsed.append(
                {
                    "sheet": sheet_name,
                    "key": key,
                    "label": sheet_name[:MAX_LABEL_LEN],
                    "schema": schema,
                    "columns": list(columns or []),
                    "header_row": item.get("header_row"),
                    "mapping": item.get("mapping"),
                    "metric": item.get("metric_column"),
                    "group": item.get("group_column"),
                    "view": str(item.get("view") or "dashboard"),
                }
            )
        row = await self.create(
            agent_id=agent_id,
            owner_user_id=owner_user_id,
            name=clean_name,
            description=clean_description,
            columns={"columns": parsed[0]["columns"]},
        )
        imported = updated = skipped = 0
        errors: list[str] = []
        warnings: list[str] = []
        pii_columns: list[str] = []
        blocked_pii: list[str] = []
        failed_rows: list[dict[str, Any]] = []
        try:
            for item in parsed[1:]:
                await self.create_table(
                    row,
                    table_key=item["key"],
                    label=item["label"],
                    columns={"columns": item["columns"]},
                )
            for item in parsed:
                result = await self.import_from_file(
                    row,
                    path=clean_path,
                    table=item["key"],
                    sheet=item["sheet"],
                    header_row=item["header_row"],
                    mapping=item["mapping"],
                    include_pii=include_pii,
                )
                imported += result.imported
                updated += result.updated
                skipped += result.skipped
                errors.extend(result.errors)
                warnings.extend(result.warnings)
                pii_columns.extend(result.pii_columns)
                blocked_pii.extend(result.blocked_pii)
                failed_rows.extend(result.failed_rows)
            if imported == 0 and updated == 0 and errors:
                raise _invalid(ErrorCode.APP_ROW_INVALID, errors[0])
            data = await self.read_workspace_file(agent_id, clean_path)
            report = inspect_file(clean_path, data)
            tables: list[dict[str, Any]] = []
            for item in parsed:
                selected = _select_sheet_report(report, item["sheet"])
                schema = await self.read_schema(row, item["key"])
                spec = suggest_view(
                    selected,
                    schema,
                    view=item["view"],
                    title=clean_name,
                    metric=item["metric"],
                    group=item["group"],
                )
                tables.append(
                    {
                        "key": item["key"],
                        "label": item["label"],
                        "schema": schema,
                        "spec": spec,
                    }
                )
            html = render_multi_html(tables, title=clean_name, description=clean_description)
            await self.write_html(row, html)
        except Exception:
            try:
                await self.delete(row)
            except Exception:
                logger.warning("failed to roll back quick-create app %s", row.app_id, exc_info=True)
            raise
        return row, ImportResult(
            imported=imported,
            skipped=skipped,
            errors=errors[:MAX_IMPORT_ERRORS],
            updated=updated,
            warnings=warnings,
            pii_columns=list(dict.fromkeys(pii_columns)),
            blocked_pii=list(dict.fromkeys(blocked_pii)),
            failed_rows=failed_rows[:MAX_FAILED_ROWS],
        )

    async def read_workspace_file(self, agent_id: str, path: str) -> bytes:
        """Read a workspace file (a path outside the app's own directory)."""
        source_path = _safe_import_path(path)
        ws = await self._workspace(agent_id)
        data = await ws.adownload_bytes(source_path)
        if data is None:
            raise _invalid(
                ErrorCode.APP_ROW_INVALID,
                f"file not found: {source_path}",
            )
        return data

    async def inspect_workspace_file(self, agent_id: str, path: str) -> dict[str, Any]:
        """Inspect a spreadsheet in the agent workspace and return a report.

        The report is a compact description of the file — sheets, headers,
        column statistics and a few sample rows — so an agent can propose a
        schema without reading the whole file into its context.
        """
        source_path = _safe_import_path(path)
        data = await self.read_workspace_file(agent_id, source_path)
        return inspect_file(source_path, data)

    async def import_from_file(
        self,
        row: AppRecord,
        *,
        path: str,
        table: str = MAIN_TABLE_KEY,
        mode: str = IMPORT_MODE_APPEND,
        mapping: Mapping[str, str] | None = None,
        data: bytes | None = None,
        sheet: str | None = None,
        header_row: int | None = None,
        dry_run: bool = False,
        upsert_key: list[str] | None = None,
        include_pii: bool = False,
    ) -> ImportResult:
        """Parse a workspace file (or raw bytes) and import it into one table.

        ``sheet`` and ``header_row`` point at the part of the file to import
        (an Excel worksheet and its header line). ``dry_run`` validates and
        counts the rows without writing anything.
        """
        source_path = _safe_import_path(path)
        if data is None:
            ws = await self._workspace(row.agent_id)
            data = await ws.adownload_bytes(source_path)
            if data is None:
                raise _invalid(
                    ErrorCode.APP_ROW_INVALID,
                    f"import file not found: {source_path}",
                )
        records = parse_import_file(
            source_path,
            data,
            sheet=sheet,
            header_row=header_row,
        )
        if not records:
            raise _invalid(
                ErrorCode.APP_ROW_INVALID,
                f"import file has no data rows: {source_path}",
            )
        return await self.import_rows(
            row,
            records,
            table=table,
            mode=mode,
            mapping=mapping,
            dry_run=dry_run,
            upsert_key=upsert_key,
            include_pii=include_pii,
        )

    async def import_from_files(
        self,
        row: AppRecord,
        *,
        paths: list[str],
        table: str = MAIN_TABLE_KEY,
        mode: str = IMPORT_MODE_APPEND,
        mapping: Mapping[str, str] | None = None,
        sheet: str | None = None,
        header_row: int | None = None,
        dry_run: bool = False,
        upsert_key: list[str] | None = None,
        include_pii: bool = False,
    ) -> ImportResult:
        """Import a batch of workspace files into one table, in order."""
        if not paths:
            raise _invalid(ErrorCode.APP_ROW_INVALID, "no import files were given")
        ws = await self._workspace(row.agent_id)
        records: list[dict[str, str]] = []
        for path in paths:
            source_path = _safe_import_path(path)
            data = await ws.adownload_bytes(source_path)
            if data is None:
                raise _invalid(
                    ErrorCode.APP_ROW_INVALID,
                    f"import file not found: {source_path}",
                )
            records.extend(
                parse_import_file(
                    source_path,
                    data,
                    sheet=sheet,
                    header_row=header_row,
                )
            )
        if not records:
            raise _invalid(
                ErrorCode.APP_ROW_INVALID,
                "import files have no data rows",
            )
        return await self.import_rows(
            row,
            records,
            table=table,
            mode=mode,
            mapping=mapping,
            dry_run=dry_run,
            upsert_key=upsert_key,
            include_pii=include_pii,
        )

    async def import_rows(
        self,
        row: AppRecord,
        records: list[dict[str, str]],
        *,
        table: str = MAIN_TABLE_KEY,
        mode: str = IMPORT_MODE_APPEND,
        mapping: Mapping[str, str] | None = None,
        dry_run: bool = False,
        upsert_key: list[str] | None = None,
        include_pii: bool = False,
        source: str = "import",
    ) -> ImportResult:
        """Validate and write parsed records into one table (append or replace).

        Rows that fail validation are skipped and counted, not fatal; a
        replace that would wipe the table because *every* record failed is
        rejected instead of silently emptying it. With ``dry_run`` the rows
        are validated and counted but nothing is written.
        """
        if mode not in IMPORT_MODES:
            raise _invalid(
                ErrorCode.APP_ROW_INVALID,
                f"invalid import mode: {mode!r}",
            )
        key = self._require_table(row, table)
        clean = [dict(record) for record in records]
        if len(clean) > MAX_ROWS:
            raise _invalid(
                ErrorCode.APP_LIMIT_EXCEEDED,
                f"import file exceeds the {MAX_ROWS}-row limit",
                limit=MAX_ROWS,
            )
        await self._ensure_db_rows(row)
        async with _lock_for(row.app_id):
            self._require_live(row.app_id)
            self._ensure_main_table(row)
            current = self._app_repo.get(row.app_id) or row
            ws = await self._workspace(current.agent_id)
            schema = await self._read_schema(ws, current.app_id, key)
            source_map = (
                {str(src).strip(): str(dst).strip() for src, dst in mapping.items()}
                if mapping
                else {name: name for name in schema.names}
            )
            unknown = set(source_map.values()) - set(schema.names)
            if unknown:
                raise _invalid(
                    ErrorCode.APP_SCHEMA_INVALID,
                    f"unknown target columns: {', '.join(sorted(unknown))}",
                )
            if upsert_key:
                unknown_keys = set(upsert_key) - set(schema.names)
                if unknown_keys:
                    raise _invalid(
                        ErrorCode.APP_SCHEMA_INVALID,
                        f"unknown upsert columns: {', '.join(sorted(unknown_keys))}",
                    )
            targets = [
                {dst: str(record.get(src, "")) for src, dst in source_map.items()}
                for record in clean
            ]
            schema_with_pii = _with_detected_pii(schema, targets)
            pii_changed = schema_with_pii is not schema
            schema = schema_with_pii
            pii_columns = _pii_names(schema)
            blocked_pii = [] if include_pii else pii_columns
            required_blocked = [
                name
                for name in blocked_pii
                if (column := schema.column(name)) is not None and column.required
            ]
            if required_blocked:
                raise _invalid(
                    ErrorCode.APP_ROW_INVALID,
                    (
                        "sensitive required columns cannot be skipped: "
                        f"{', '.join(required_blocked)}; pass include_pii=true "
                        "to import them, or make them optional"
                    ),
                    blocked_pii=required_blocked,
                )
            imported_rows: list[dict[str, str]] = []
            errors: list[str] = []
            failed_rows: list[dict[str, Any]] = []
            for index, target in enumerate(targets):
                if blocked_pii:
                    target = {
                        name: ("" if name in blocked_pii else value)
                        for name, value in target.items()
                    }
                try:
                    imported_rows.append(validate_row(schema, target))
                except OctopError as exc:
                    if len(errors) < MAX_IMPORT_ERRORS:
                        errors.append(f"row {index + 1}: {exc.message}")
                    if len(failed_rows) < MAX_FAILED_ROWS:
                        failed_rows.append(
                            {"row": index + 1, "values": target, "error": exc.message}
                        )
                    continue
            warnings: list[str] = []
            for name in pii_columns:
                column = schema.column(name)
                kind = column.pii if column is not None else "sensitive"
                if name in blocked_pii:
                    warnings.append(
                        f"column {name!r} looks like {kind}; values were not imported "
                        "(pass include_pii=true to keep them)"
                    )
                else:
                    warnings.append(
                        f"column {name!r} looks like {kind}; imported as sensitive data"
                    )
            # Replace always writes the whole table, so an empty result wipes
            # it. That is never what "import zero records" means: either every
            # row failed validation, or the source returned nothing (a
            # connector can transiently do that). Refuse both instead of
            # destroying the user's data.
            if mode == IMPORT_MODE_REPLACE and not imported_rows:
                raise _invalid(
                    ErrorCode.APP_ROW_INVALID,
                    (
                        "every row failed validation; table left unchanged"
                        if clean
                        else "no records to import; table left unchanged"
                    ),
                    errors=errors,
                )
            if current.storage_version >= 2:
                existing_count = (
                    0
                    if mode == IMPORT_MODE_REPLACE
                    else self._app_repo.count_data_rows(current.app_id, key)
                )
                if existing_count + len(imported_rows) > MAX_ROWS:
                    raise _invalid(
                        ErrorCode.APP_LIMIT_EXCEEDED,
                        f"table has reached the {MAX_ROWS}-row limit",
                        limit=MAX_ROWS,
                    )
                table_view = self._app_repo.get_table(current.app_id, key)
                current_bytes = (
                    0
                    if mode == IMPORT_MODE_REPLACE
                    else (table_view.data_bytes if table_view is not None else 0)
                )
                added_bytes = sum(
                    len(json.dumps(item, ensure_ascii=False).encode("utf-8"))
                    for item in imported_rows
                )
                if current_bytes + added_bytes > MAX_CSV_BYTES:
                    raise _invalid(
                        ErrorCode.APP_LIMIT_EXCEEDED,
                        f"table data would exceed {MAX_CSV_BYTES // (1024 * 1024)} MB",
                        limit=MAX_CSV_BYTES,
                    )
                if dry_run:
                    return ImportResult(
                        imported=0,
                        skipped=len(clean) - len(imported_rows),
                        errors=errors,
                        warnings=warnings,
                        would_import=len(imported_rows),
                        dry_run=True,
                        pii_columns=pii_columns,
                        blocked_pii=blocked_pii,
                        failed_rows=failed_rows,
                    )
                if pii_changed:
                    await self._persist_schema(ws, current.app_id, key, schema)
                updated_count = 0
                if mode == IMPORT_MODE_REPLACE:
                    self._app_repo.replace_data_rows(current.app_id, key, imported_rows)
                elif upsert_key:
                    for item in imported_rows:
                        key_values = [item.get(name, "") for name in upsert_key]
                        if any(value == "" for value in key_values):
                            self._app_repo.append_data_row(
                                current.app_id,
                                key,
                                item,
                                source=source,
                            )
                            continue
                        source_key = "|".join(key_values)
                        existing_row = self._app_repo.find_data_row_by_source(
                            current.app_id,
                            key,
                            source,
                            source_key,
                        )
                        if existing_row is None:
                            self._app_repo.append_data_row(
                                current.app_id,
                                key,
                                item,
                                source=source,
                                source_id=source,
                                source_key=source_key,
                            )
                        else:
                            self._app_repo.update_data_row(
                                current.app_id,
                                existing_row.row_id,
                                item,
                            )
                            updated_count += 1
                else:
                    self._app_repo.append_data_rows(current.app_id, key, imported_rows)
                await self._refresh_manifest(ws, current.app_id)
                return ImportResult(
                    imported=len(imported_rows) - updated_count,
                    skipped=len(clean) - len(imported_rows),
                    errors=errors,
                    warnings=warnings,
                    updated=updated_count,
                    pii_columns=pii_columns,
                    blocked_pii=blocked_pii,
                    failed_rows=failed_rows,
                )
            updated_count = 0
            if mode == IMPORT_MODE_REPLACE:
                rows = list(imported_rows)
            else:
                rows = await self._read_rows_file(ws, current.app_id, schema, key)
                if upsert_key:
                    index_by_key = {
                        tuple(row.get(name, "") for name in upsert_key): position
                        for position, row in enumerate(rows)
                    }
                    for item in imported_rows:
                        item_key = tuple(item.get(name, "") for name in upsert_key)
                        if item_key in index_by_key:
                            rows[index_by_key[item_key]] = item
                            updated_count += 1
                        else:
                            index_by_key[item_key] = len(rows)
                            rows.append(item)
                else:
                    rows = rows + imported_rows
            if len(rows) > MAX_ROWS:
                raise _invalid(
                    ErrorCode.APP_LIMIT_EXCEEDED,
                    f"table has reached the {MAX_ROWS}-row limit",
                    limit=MAX_ROWS,
                )
            if dry_run:
                # Everything above (limits, validation, the replace guard) is
                # the real work; only the write is skipped.
                return ImportResult(
                    imported=0,
                    skipped=len(clean) - len(imported_rows),
                    errors=errors,
                    warnings=warnings,
                    would_import=len(imported_rows),
                    dry_run=True,
                    pii_columns=pii_columns,
                    blocked_pii=blocked_pii,
                    failed_rows=failed_rows,
                )
            if pii_changed:
                await self._persist_schema(ws, current.app_id, key, schema)
            await self._persist_rows(ws, current, key, schema, rows)
            return ImportResult(
                imported=len(imported_rows) - updated_count,
                skipped=len(clean) - len(imported_rows),
                errors=errors,
                warnings=warnings,
                updated=updated_count,
                pii_columns=pii_columns,
                blocked_pii=blocked_pii,
                failed_rows=failed_rows,
            )

    async def find_row_by_values(
        self,
        row: AppRecord,
        values: Mapping[str, object],
        *,
        table: str = MAIN_TABLE_KEY,
    ) -> LightAppDataRow | None:
        """First row whose stored values match every supplied column.

        Used by external writers (games, callbacks) that identify a row by a
        business key rather than a row id. Legacy CSV storage is migrated on
        first access, so this always reads the database when migration works.
        """
        key = self._require_table(row, table)
        if not values:
            return None
        if not await self._ensure_db_rows(row):
            return None
        current = self._app_repo.get(row.app_id) or row
        wanted = {str(name): str(value) for name, value in values.items()}
        for item in self._app_repo.list_all_data_rows(current.app_id, key):
            if all(str(item.values.get(name, "")) == value for name, value in wanted.items()):
                return item
        return None

    async def update_row(
        self,
        row: AppRecord,
        row_id: str,
        values: Mapping[str, object],
        *,
        table: str = MAIN_TABLE_KEY,
        expected_version: int | None = None,
    ) -> dict[str, Any]:
        """Update one row with an optional version check (CAS)."""
        key = self._require_table(row, table)
        await self._ensure_db_rows(row)
        async with _lock_for(row.app_id):
            self._require_live(row.app_id)
            current = self._app_repo.get(row.app_id) or row
            if current.storage_version < 2:
                raise _invalid(
                    ErrorCode.APP_ROW_NOT_FOUND,
                    f"row not found: {row_id}",
                )
            ws = await self._workspace(current.agent_id)
            schema = await self._read_schema(ws, current.app_id, key)
            existing_row = self._app_repo.get_data_row(current.app_id, row_id)
            if existing_row is None:
                raise _invalid(ErrorCode.APP_ROW_NOT_FOUND, f"row not found: {row_id}")
            normalized = validate_row(schema, values)
            _reject_unmarked_pii(schema, normalized, table=key)
            table_view = self._app_repo.get_table(current.app_id, key)
            current_bytes = table_view.data_bytes if table_view is not None else 0
            old_bytes = len(json.dumps(existing_row.values, ensure_ascii=False).encode("utf-8"))
            new_bytes = len(json.dumps(normalized, ensure_ascii=False).encode("utf-8"))
            if current_bytes - old_bytes + new_bytes > MAX_CSV_BYTES:
                raise _invalid(
                    ErrorCode.APP_LIMIT_EXCEEDED,
                    f"table data would exceed {MAX_CSV_BYTES // (1024 * 1024)} MB",
                    limit=MAX_CSV_BYTES,
                )
            updated = self._app_repo.update_data_row(
                current.app_id,
                row_id,
                normalized,
                expected_version=expected_version,
            )
            if updated is None:
                raise OctopError(
                    ErrorCode.APP_CONFLICT,
                    "row changed since it was read; refresh and retry",
                )
            await self._refresh_manifest(ws, current.app_id)
            return {
                "row_id": updated.row_id,
                "values": updated.values,
                "version": updated.version,
            }

    async def delete_row(
        self,
        row: AppRecord,
        row_id: str,
        *,
        expected_version: int | None = None,
    ) -> str:
        """Delete one row with an optional version check (CAS)."""
        await self._ensure_db_rows(row)
        async with _lock_for(row.app_id):
            self._require_live(row.app_id)
            current = self._app_repo.get(row.app_id) or row
            if current.storage_version < 2:
                raise _invalid(ErrorCode.APP_ROW_NOT_FOUND, f"row not found: {row_id}")
            if self._app_repo.get_data_row(current.app_id, row_id) is None:
                raise _invalid(ErrorCode.APP_ROW_NOT_FOUND, f"row not found: {row_id}")
            if not self._app_repo.delete_data_row(
                current.app_id,
                row_id,
                expected_version=expected_version,
            ):
                raise OctopError(
                    ErrorCode.APP_CONFLICT,
                    "row changed since it was read; refresh and retry",
                )
            ws = await self._workspace(current.agent_id)
            await self._refresh_manifest(ws, current.app_id)
            return row_id

    async def export_rows(
        self,
        row: AppRecord,
        *,
        table: str = MAIN_TABLE_KEY,
        redact_pii: bool = False,
    ) -> tuple[str, str]:
        """Render one table as CSV (a snapshot, not the live storage)."""
        key = self._require_table(row, table)
        await self._ensure_db_rows(row)
        current = self._app_repo.get(row.app_id) or row
        ws = await self._workspace(current.agent_id)
        schema = await self._read_schema(ws, current.app_id, key)
        if current.storage_version >= 2:
            rows = _normalize_rows(
                schema,
                [item.values for item in self._app_repo.list_all_data_rows(current.app_id, key)],
            )
        else:
            rows = await self._read_rows_file(ws, current.app_id, schema, key)
        schema, rows = _apply_pii(schema, rows, redact_pii=redact_pii)
        return f"{key}.csv", render_csv(schema, rows)

    # ----- backup / restore -----

    async def backup_archive(self, row: AppRecord) -> tuple[str, bytes]:
        """Bundle the app shell, tables and assets into one zip.

        The archive is a portable snapshot: the control-plane ids are not part
        of it, so restoring it creates a new app.
        """
        current = self._app_repo.get(row.app_id) or row
        entries: list[tuple[str, bytes]] = []
        tables: list[dict[str, Any]] = []
        for table_view in self.list_tables(current):
            schema = await self.read_schema(current, table_view.table_key)
            tables.append(
                {
                    "table_key": table_view.table_key,
                    "label": table_view.label,
                    "is_main": table_view.is_main,
                }
            )
            entries.append(
                (
                    f"{TABLES_DIR}/{table_view.table_key}/{FILE_SCHEMA}",
                    schema.to_json().encode("utf-8"),
                )
            )
            _filename, csv_text = await self.export_rows(current, table=table_view.table_key)
            entries.append(
                (
                    f"{TABLES_DIR}/{table_view.table_key}/{FILE_DATA}",
                    csv_text.encode("utf-8"),
                )
            )
        html = await self.read_html(current)
        if html:
            entries.append((FILE_HTML, html.encode("utf-8")))
        preview = await self.read_preview(current)
        if preview:
            entries.append((FILE_PREVIEW, preview.encode("utf-8")))
        for asset_name in await self.list_assets(current):
            _mime, asset_data = await self.read_asset(current, asset_name)
            entries.append((f"{ASSETS_DIR}/{asset_name}", asset_data))
        manifest = {
            "format": BACKUP_FORMAT,
            "version": BACKUP_VERSION,
            "created_at": int(time.time()),
            "app": {"name": current.name, "description": current.description},
            "capabilities": self.capability_list(current),
            "tables": tables,
        }
        entries.insert(
            0,
            (
                FILE_MANIFEST,
                json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"),
            ),
        )
        archive = await asyncio.to_thread(_zip_entries, entries)
        return f"miniapp-{current.app_id}.zip", archive

    async def restore_archive(
        self,
        *,
        agent_id: str,
        owner_user_id: int,
        data: bytes,
    ) -> LightAppRow:
        """Create a new app from a backup archive produced by :meth:`backup_archive`."""
        if len(data) > MAX_BACKUP_BYTES:
            raise _invalid(
                ErrorCode.APP_SOURCE_INVALID,
                f"backup exceeds the {MAX_BACKUP_BYTES // (1024 * 1024)} MB limit",
            )
        try:
            manifest, tables, html, preview, assets = await asyncio.to_thread(
                _read_backup_zip, data
            )
        except zipfile.BadZipFile:
            raise _invalid(
                ErrorCode.APP_SOURCE_INVALID, "backup is not a valid zip archive"
            ) from None
        except ValueError as exc:
            raise _invalid(ErrorCode.APP_SOURCE_INVALID, f"invalid app backup: {exc}") from None
        app_meta = manifest.get("app")
        assert isinstance(app_meta, dict)  # _read_backup_zip validated it
        name = str(app_meta.get("name") or "").strip() or "Restored app"
        description = str(app_meta.get("description") or "")
        raw_capabilities = manifest.get("capabilities")
        restored_capabilities = (
            app_capabilities.validate(raw_capabilities)
            if isinstance(raw_capabilities, list)
            else app_capabilities.LEGACY_CAPABILITIES
        )
        main = tables.get(MAIN_TABLE_KEY)
        if main is None:
            raise _invalid(ErrorCode.APP_SOURCE_INVALID, "backup has no main table")
        try:
            schemas = {
                key: json.loads(schema_bytes.decode("utf-8"))
                for key, (_label, schema_bytes, _csv) in tables.items()
            }
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise _invalid(
                ErrorCode.APP_SOURCE_INVALID,
                "backup contains an unreadable table schema",
            ) from None
        row = await self.create(
            agent_id=agent_id,
            owner_user_id=owner_user_id,
            name=name,
            description=description,
            columns=schemas[MAIN_TABLE_KEY],
            capabilities=restored_capabilities,
        )
        try:
            for key, (label, _schema_bytes, _csv) in tables.items():
                if key == MAIN_TABLE_KEY:
                    continue
                await self.create_table(
                    row,
                    table_key=key,
                    label=label,
                    columns=schemas[key],
                )
            for key, (_label, _schema_bytes, csv_bytes) in tables.items():
                records = parse_import_file(f"{key}.csv", csv_bytes) if csv_bytes.strip() else []
                if records:
                    await self.import_rows(
                        row,
                        records,
                        table=key,
                        mode=IMPORT_MODE_REPLACE,
                        source="restore",
                    )
            if html:
                await self.write_html(row, html)
            if preview:
                await self.write_preview(row, preview)
            for asset_name, asset_data in assets:
                await self.save_asset(row, asset_name, asset_data)
        except Exception:
            logger.warning("failed to restore app, cleaning up: %s", row.app_id, exc_info=True)
            await self.delete(row)
            raise
        return self.get(row.app_id)

    # ----- public links -----

    def get_publication(self, row: AppRecord) -> LightAppPublicationRow | None:
        return self._app_repo.get_publication(row.app_id)

    @staticmethod
    def _publication_secret_key(app_id: str) -> str:
        return f"light_app_publication_token:{app_id}"

    def create_publication(
        self,
        row: AppRecord,
        *,
        user_id: int,
        ttl_days: int = 0,
    ) -> tuple[str, int | None]:
        """Create (or rotate) the read-only public token; returns it once."""
        token = secrets.token_urlsafe(32)
        expires_at = int(time.time()) + ttl_days * 86400 if ttl_days > 0 else None
        self._app_repo.upsert_publication(
            row.app_id,
            token_hash=hashlib.sha256(token.encode("utf-8")).hexdigest(),
            created_by=user_id,
            expires_at=expires_at,
        )
        if self._secret_repo is not None:
            self._secret_repo.set(self._publication_secret_key(row.app_id), token.encode("utf-8"))
        return token, expires_at

    def get_or_create_publication(
        self,
        row: AppRecord,
        *,
        user_id: int,
        rotate: bool = False,
        ttl_days: int = 0,
    ) -> tuple[str, int | None]:
        """Return the active public token, reusing it unless *rotate* is set.

        The publication table stores only the token hash.  A copy of the
        plaintext token lives in the secrets table so agent tools can hand the
        same link to the user again without invalidating the previous message.
        """
        publication = self.get_publication(row)
        if (
            publication is not None
            and publication.revoked_at is None
            and (publication.expires_at is None or publication.expires_at > int(time.time()))
            and not rotate
            and self._secret_repo is not None
        ):
            stored = self._secret_repo.get(self._publication_secret_key(row.app_id))
            if stored:
                return stored.decode("utf-8"), publication.expires_at
        return self.create_publication(row, user_id=user_id, ttl_days=ttl_days)

    async def _snapshot_parts(
        self,
        row: AppRecord,
        ws: BackendWorkspace,
    ) -> tuple[str, str]:
        """Current HTML plus the public table structure, in a stable encoding."""
        html = await ws.aread_text(app_path(row.app_id, FILE_HTML)) or ""
        tables: list[dict[str, Any]] = []
        for table in self.list_tables(row):
            if not table.public_visible:
                continue
            schema = await self._read_schema(ws, row.app_id, table.table_key)
            tables.append(
                {
                    "table_key": table.table_key,
                    "label": table.label,
                    "is_main": table.is_main,
                    "public_visible": table.public_visible,
                    "columns": [column.to_dict() for column in schema.columns],
                }
            )
        return html, json.dumps(
            tables,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    async def publish(
        self,
        row: AppRecord,
        *,
        user_id: int,
        rotate: bool = False,
        ttl_days: int = 0,
    ) -> tuple[str, int | None]:
        """Create/reuse the public token and freeze the current public version.

        The token stays stable unless ``rotate`` is set; the snapshot is always
        refreshed. Rows remain live; only the HTML and public table structure
        are frozen.
        """
        async with _lock_for(row.app_id):
            self._require_live(row.app_id)
            ws = await self._workspace(row.agent_id)
            html, tables_json = await self._snapshot_parts(row, ws)
            if rotate:
                token, expires_at = self.create_publication(row, user_id=user_id, ttl_days=ttl_days)
            else:
                token, expires_at = self.get_or_create_publication(
                    row, user_id=user_id, rotate=False, ttl_days=ttl_days
                )
            now = int(time.time())
            self._app_repo.set_publication_snapshot(
                row.app_id,
                html=html,
                tables_json=tables_json,
                digest=hashlib.sha256((html + "\0" + tables_json).encode("utf-8")).hexdigest(),
                snapshot_at=now,
            )
            return token, expires_at

    async def publication_status(self, row: AppRecord) -> PublicationStatus:
        """Compare the stored snapshot with the current app version."""
        publication = self.get_publication(row)
        if publication is None:
            return PublicationStatus(published_at=None, stale=False)
        if publication.snapshot_at is None:
            return PublicationStatus(
                published_at=None,
                stale=True,
                reasons=("not_published",),
            )
        async with _lock_for(row.app_id):
            try:
                ws = await self._workspace(row.agent_id)
                html, tables_json = await self._snapshot_parts(row, ws)
            except OctopError:
                return PublicationStatus(
                    published_at=publication.snapshot_at,
                    stale=True,
                    reasons=("workspace",),
                )
        reasons: list[str] = []
        if publication.html_snapshot != html:
            reasons.append("html")
        if publication.tables_snapshot_json != tables_json:
            reasons.append("schema")
        return PublicationStatus(
            published_at=publication.snapshot_at,
            stale=bool(reasons),
            reasons=tuple(reasons),
        )

    def public_snapshot(
        self,
        row: AppRecord,
    ) -> tuple[str, list[dict[str, Any]]] | None:
        """The frozen public version, or None for a legacy publication."""
        publication = self.get_publication(row)
        if publication is None or publication.snapshot_at is None:
            return None
        try:
            items = json.loads(publication.tables_snapshot_json or "[]")
        except ValueError:
            items = []
        if not isinstance(items, list):
            items = []
        live = {table.table_key: table for table in self.list_tables(row)}
        tables: list[dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            key = item.get("table_key")
            table = live.get(key) if isinstance(key, str) else None
            # Hiding a table takes effect immediately, even with a snapshot.
            if table is None or not table.public_visible:
                continue
            tables.append({**item, "row_count": table.row_count})
        return publication.html_snapshot, tables

    def revoke_publication(self, row: AppRecord) -> bool:
        revoked = self._app_repo.revoke_publication(row.app_id)
        if self._secret_repo is not None:
            self._secret_repo.delete(self._publication_secret_key(row.app_id))
        return revoked

    def resolve_public_app(self, token: str) -> AppRecord | None:
        clean = token.strip()
        if not clean:
            return None
        publication = self._app_repo.get_publication_by_hash(
            hashlib.sha256(clean.encode("utf-8")).hexdigest()
        )
        if publication is None or publication.revoked_at is not None:
            return None
        if publication.expires_at is not None and publication.expires_at <= int(time.time()):
            return None
        return self._app_repo.get(publication.app_id)

    # ----- data sources -----

    def list_sources(self, row: AppRecord) -> list[SourceView]:
        return [self._source_view(item) for item in self._app_repo.list_sources(row.app_id)]

    def get_source(self, row: AppRecord, source_id: str) -> SourceView:
        item = self._app_repo.get_source(row.app_id, source_id)
        if item is None:
            raise OctopError.localized(ErrorCode.APP_NOT_FOUND)
        return self._source_view(item)

    def save_source(
        self,
        row: AppRecord,
        *,
        source_id: str,
        table_key: str,
        kind: str,
        mode: str,
        schedule_seconds: int = 0,
        params: Mapping[str, Any] | None = None,
        mapping: Mapping[str, str] | None = None,
        created_by: int,
    ) -> SourceView:
        """Create or update one binding. Identity is ``source_id``; a table can
        hold at most one binding."""
        self._require_table(row, table_key)
        if kind not in SOURCE_KINDS:
            raise _invalid(
                ErrorCode.APP_SOURCE_INVALID,
                f"invalid source kind: {kind!r}",
            )
        if mode not in IMPORT_MODES:
            raise _invalid(
                ErrorCode.APP_SOURCE_INVALID,
                f"invalid source mode: {mode!r}",
            )
        schedule = int(schedule_seconds)
        if schedule < 0 or schedule > MAX_SCHEDULE_SECONDS:
            raise _invalid(
                ErrorCode.APP_SOURCE_INVALID,
                f"schedule_seconds must be 0..{MAX_SCHEDULE_SECONDS}",
            )
        clean_params = self._validate_source_params(kind, params or {})
        clean_mapping = {
            str(src).strip(): str(dst).strip()
            for src, dst in (mapping or {}).items()
            if str(src).strip() and str(dst).strip()
        }
        existing = self._app_repo.get_source(row.app_id, source_id)
        if existing is not None:
            try:
                old_params = json.loads(existing.params_json)
            except json.JSONDecodeError:
                old_params = {}
            if isinstance(old_params, dict) and old_params.get("cursor"):
                clean_params.setdefault("cursor", str(old_params["cursor"]))
        params_json = json.dumps(clean_params, ensure_ascii=False)
        mapping_json = json.dumps(clean_mapping, ensure_ascii=False)
        if existing is None:
            clash = any(
                item.table_key == table_key for item in self._app_repo.list_sources(row.app_id)
            )
            if clash:
                raise _invalid(
                    ErrorCode.APP_SOURCE_INVALID,
                    f"table {table_key!r} already has a data source",
                )
            item = self._app_repo.insert_source(
                source_id=source_id,
                app_id=row.app_id,
                table_key=table_key,
                kind=kind,
                mode=mode,
                schedule_seconds=schedule,
                params_json=params_json,
                mapping_json=mapping_json,
                created_by=created_by,
            )
        else:
            item = self._app_repo.update_source(
                row.app_id,
                source_id,
                table_key=table_key,
                kind=kind,
                mode=mode,
                schedule_seconds=schedule,
                params_json=params_json,
                mapping_json=mapping_json,
            )
        return self._source_view(item)

    def delete_source(self, row: AppRecord, source_id: str) -> None:
        if not self._app_repo.delete_source(row.app_id, source_id):
            raise OctopError.localized(ErrorCode.APP_NOT_FOUND)

    async def run_source(self, row: AppRecord, source_id: str) -> ImportResult:
        """Run one binding now and record the outcome.

        Raises on failure after recording ``last_status='error'`` and
        re-scheduling (exponential backoff) so the auto-sync scheduler and
        manual triggers share the same bookkeeping.
        """
        item = self._app_repo.get_source(row.app_id, source_id)
        if item is None:
            raise OctopError.localized(ErrorCode.APP_NOT_FOUND)
        run_at = int(time.time())
        try:
            params = json.loads(item.params_json)
            if not isinstance(params, dict):
                params = {}
            raw_upsert = params.get("upsert_key")
            upsert_key = (
                [str(value) for value in raw_upsert]
                if isinstance(raw_upsert, list) and raw_upsert
                else None
            )
            include_pii = params.get("include_pii") is True
            if item.kind == SOURCE_KIND_WORKSPACE_FILE:
                result = await self.import_from_file(
                    row,
                    path=str(params.get("path") or ""),
                    table=item.table_key,
                    mode=item.mode,
                    mapping=json.loads(item.mapping_json),
                    upsert_key=upsert_key,
                    include_pii=include_pii,
                )
            elif item.kind == SOURCE_KIND_EXTERNAL:
                result = await self._run_external_source(
                    row,
                    item,
                    params,
                    upsert_key=upsert_key,
                    include_pii=include_pii,
                )
            else:
                raise _invalid(
                    ErrorCode.APP_SOURCE_INVALID,
                    f"invalid source kind: {item.kind!r}",
                )
        except OctopError as exc:
            self._record_run(item, status=SOURCE_STATUS_ERROR, run_at=run_at, error=exc.message)
            raise
        except Exception as exc:
            self._record_run(item, status=SOURCE_STATUS_ERROR, run_at=run_at, error=str(exc))
            raise _invalid(ErrorCode.APP_SOURCE_INVALID, f"source run failed: {exc}") from exc
        self._record_run(
            item,
            status=SOURCE_STATUS_OK,
            run_at=run_at,
            error="",
            row_count=result.imported,
        )
        return result

    def _record_run(
        self,
        item: LightAppSourceRow,
        *,
        status: str,
        run_at: int,
        error: str = "",
        row_count: int | None = None,
    ) -> None:
        """Record the run outcome and the next scheduled attempt.

        Success re-arms the normal interval; failure backs off exponentially
        from the interval, capped at ``MAX_BACKOFF_SECONDS``. Sources without
        a schedule get ``next_run_at`` written too, but the scan ignores them.
        """
        if status == SOURCE_STATUS_OK or item.schedule_seconds <= 0:
            fail_count = 0
            next_run_at = run_at + max(item.schedule_seconds, 0)
        else:
            fail_count = item.fail_count + 1
            delay = min(item.schedule_seconds * 2 ** (fail_count - 1), MAX_BACKOFF_SECONDS)
            next_run_at = run_at + delay
        self._app_repo.set_source_run_state(
            item.source_id,
            status=status,
            run_at=run_at,
            error=error,
            row_count=row_count,
            next_run_at=next_run_at,
            fail_count=fail_count,
        )

    async def _run_external_source(
        self,
        row: AppRecord,
        item: Any,
        params: dict[str, Any],
        *,
        upsert_key: list[str] | None = None,
        include_pii: bool = False,
    ) -> ImportResult:
        if self._external_tool_runner is None:
            raise _invalid(
                ErrorCode.APP_SOURCE_INVALID,
                "external data sources are not available on this server",
            )
        instance_id = str(params.get("instance_id") or "")
        tool = str(params.get("tool") or "")
        if not instance_id or not tool:
            raise _invalid(
                ErrorCode.APP_SOURCE_INVALID,
                "external source needs instance_id and tool",
            )
        raw_args = params.get("args")
        args: dict[str, Any] = dict(raw_args) if isinstance(raw_args, dict) else {}
        result_path = str(params.get("result_path") or "").strip()
        cursor_arg = str(params.get("cursor_arg") or "").strip()
        cursor_path = str(params.get("cursor_path") or "").strip()
        cursor = params.get("cursor")
        if cursor_arg and cursor not in (None, ""):
            args[cursor_arg] = cursor
        output = await asyncio.to_thread(
            self._external_tool_runner.run,
            instance_id=instance_id,
            tool=tool,
            args={str(k): v for k, v in args.items()},
        )
        records = _extract_records(output, result_path)
        result = await self.import_rows(
            row,
            records,
            table=item.table_key,
            mode=item.mode,
            mapping=json.loads(item.mapping_json),
            upsert_key=upsert_key,
            include_pii=include_pii,
        )
        if cursor_path:
            try:
                payload = json.loads(output)
            except json.JSONDecodeError:
                payload = None
            next_cursor = _json_path(payload, cursor_path)
            if next_cursor not in (None, ""):
                updated = dict(params)
                updated["cursor"] = str(next_cursor)[:1000]
                self._app_repo.set_source_params(
                    item.source_id,
                    json.dumps(updated, ensure_ascii=False),
                )
        return result

    def _validate_source_params(
        self,
        kind: str,
        params: Mapping[str, Any],
    ) -> dict[str, Any]:
        if kind == SOURCE_KIND_WORKSPACE_FILE:
            try:
                path = _safe_import_path(str(params.get("path") or ""))
            except OctopError as exc:
                # A bad path inside a source binding reads as bad config.
                raise _invalid(ErrorCode.APP_SOURCE_INVALID, exc.message) from None
            clean_params: dict[str, Any] = {"path": path}
            upsert_key = self._clean_source_upsert_key(params)
            if upsert_key:
                clean_params["upsert_key"] = upsert_key
            if params.get("include_pii") is True:
                clean_params["include_pii"] = True
            return clean_params
        instance_id = str(params.get("instance_id") or "").strip()
        tool = str(params.get("tool") or "").strip()
        if not instance_id or not tool:
            raise _invalid(
                ErrorCode.APP_SOURCE_INVALID,
                "external source needs instance_id and tool",
            )
        args = params.get("args")
        result_path = str(params.get("result_path") or "").strip()
        clean: dict[str, Any] = {
            "instance_id": instance_id[:200],
            "tool": tool[:200],
        }
        if isinstance(args, dict) and args:
            clean["args"] = {str(k): v for k, v in list(args.items())[:50]}
        if result_path:
            clean["result_path"] = result_path[:200]
        for key in ("cursor_arg", "cursor_path"):
            value = str(params.get(key) or "").strip()
            if value:
                clean[key] = value[:100]
        cursor = params.get("cursor")
        if isinstance(cursor, str) and cursor:
            clean["cursor"] = cursor[:1000]
        upsert_key = self._clean_source_upsert_key(params)
        if upsert_key:
            clean["upsert_key"] = upsert_key
        if params.get("include_pii") is True:
            clean["include_pii"] = True
        return clean

    @staticmethod
    def _clean_source_upsert_key(params: Mapping[str, Any]) -> list[str]:
        raw = params.get("upsert_key")
        if not isinstance(raw, list):
            return []
        return [str(value).strip()[:100] for value in raw[:20] if str(value).strip()]

    def _source_view(self, item: LightAppSourceRow) -> SourceView:
        try:
            params = json.loads(item.params_json)
            if not isinstance(params, dict):
                params = {}
        except json.JSONDecodeError:
            params = {}
        try:
            mapping = json.loads(item.mapping_json)
            if not isinstance(mapping, dict):
                mapping = {}
        except json.JSONDecodeError:
            mapping = {}
        return SourceView(
            source_id=item.source_id,
            app_id=item.app_id,
            table_key=item.table_key,
            kind=item.kind,
            mode=item.mode,
            schedule_seconds=item.schedule_seconds,
            params=params,
            mapping={str(k): str(v) for k, v in mapping.items()},
            last_run_at=item.last_run_at,
            last_status=item.last_status,
            last_error=item.last_error or None,
            last_row_count=item.last_row_count,
            created_by=item.created_by,
            created_at=item.created_at,
            updated_at=item.updated_at,
        )

    async def add_columns(
        self,
        row: AppRecord,
        additions: object,
        *,
        table: str = MAIN_TABLE_KEY,
    ) -> Schema:
        key = self._require_table(row, table)
        await self._ensure_db_rows(row)
        async with _lock_for(row.app_id):
            self._require_live(row.app_id)
            self._ensure_main_table(row)
            current = self._app_repo.get(row.app_id) or row
            ws = await self._workspace(current.agent_id)
            schema = await self._read_schema(ws, current.app_id, key)
            if current.storage_version >= 2:
                new_schema = extend_schema(schema, additions)
                added = new_schema.columns[len(schema.columns) :]
                if self._app_repo.count_data_rows(current.app_id, key) and any(
                    col.required for col in added
                ):
                    raise _invalid(
                        ErrorCode.APP_SCHEMA_INVALID,
                        "new columns cannot be required while the app already has rows",
                    )
                self._app_repo.set_table_schema(
                    current.app_id,
                    key,
                    new_schema.to_json(),
                    new_schema.version,
                )
                self._app_repo.touch(current.app_id)
                schema_file, _ = table_file_names(key)
                try:
                    await ws.awrite_text(
                        app_path(current.app_id, schema_file),
                        new_schema.to_json(),
                    )
                except Exception:
                    # The DB copy is authoritative; the workspace schema.json
                    # is only a mirror for the app directory.
                    logger.warning(
                        "failed to refresh schema mirror for %s/%s",
                        current.app_id,
                        key,
                        exc_info=True,
                    )
                await self._refresh_manifest(ws, current.app_id)
                return new_schema
            rows = await self._read_rows_file(ws, current.app_id, schema, key)
            new_schema = extend_schema(schema, additions)
            added = new_schema.columns[len(schema.columns) :]
            if rows and any(col.required for col in added):
                raise _invalid(
                    ErrorCode.APP_SCHEMA_INVALID,
                    "new columns cannot be required while the app already has rows",
                )
            csv_text = render_csv(new_schema, rows)
            schema_file, data_file = table_file_names(key)
            await self._write_atomic(
                ws,
                current.app_id,
                {
                    schema_file: new_schema.to_json(),
                    data_file: csv_text,
                },
            )
            self._app_repo.touch(current.app_id)
            data_bytes = len(csv_text.encode("utf-8"))
            self._app_repo.set_table_stats(
                current.app_id,
                key,
                row_count=len(rows),
                data_bytes=data_bytes,
            )
            self._app_repo.refresh_app_totals(current.app_id)
            await self._refresh_manifest(ws, current.app_id)
            return new_schema

    async def alter_schema(
        self,
        row: AppRecord,
        *,
        table: str = MAIN_TABLE_KEY,
        renames: Mapping[str, str] | None = None,
        drops: list[str] | None = None,
        types: Mapping[str, Mapping[str, Any]] | None = None,
        pii: Mapping[str, str | None] | None = None,
    ) -> Schema:
        """Rename, drop or retype columns, migrating every existing row.

        A change is rejected as a whole when any row cannot be represented by
        the new schema; nothing is half-written. ``pii`` marks or clears the
        sensitive-data kind of existing columns (``None`` clears it).
        """
        key = self._require_table(row, table)
        clean_renames = {str(src): str(dst) for src, dst in (renames or {}).items()}
        clean_drops = {str(name) for name in (drops or [])}
        clean_types = {str(name): dict(spec) for name, spec in (types or {}).items()}
        clean_pii = {
            str(name): (str(kind).strip() if kind not in (None, "") else None)
            for name, kind in (pii or {}).items()
        }
        if not clean_renames and not clean_drops and not clean_types and not clean_pii:
            return await self._read_schema(await self._workspace(row.agent_id), row.app_id, key)
        await self._ensure_db_rows(row)
        async with _lock_for(row.app_id):
            self._require_live(row.app_id)
            current = self._app_repo.get(row.app_id) or row
            ws = await self._workspace(current.agent_id)
            schema = await self._read_schema(ws, current.app_id, key)
            unknown = (set(clean_renames) | clean_drops | set(clean_types) | set(clean_pii)) - set(
                schema.names
            )
            if unknown:
                raise _invalid(
                    ErrorCode.APP_SCHEMA_INVALID,
                    f"unknown columns: {', '.join(sorted(unknown))}",
                )
            new_columns = []
            seen: set[str] = set()
            for column in schema.columns:
                if column.name in clean_drops:
                    continue
                name = clean_renames.get(column.name, column.name)
                spec = clean_types.get(column.name, {})
                column_type = str(spec.get("type", column.type))
                payload: dict[str, Any] = {
                    "name": name,
                    "label": spec.get("label", column.label),
                    "type": column_type,
                    "required": spec.get("required", column.required),
                }
                if column_type == "enum":
                    payload["values"] = spec.get("values", list(column.values))
                if column.name in clean_pii:
                    if clean_pii[column.name]:
                        payload["pii"] = clean_pii[column.name]
                elif column.pii:
                    payload["pii"] = column.pii
                parsed = parse_schema_payload([payload]).columns[0]
                if parsed.name in seen:
                    raise _invalid(
                        ErrorCode.APP_SCHEMA_INVALID,
                        f"duplicate column name after rename: {parsed.name!r}",
                    )
                seen.add(parsed.name)
                new_columns.append(parsed)
            if not new_columns:
                raise _invalid(
                    ErrorCode.APP_SCHEMA_INVALID,
                    "at least one column must remain",
                )
            new_schema = Schema(columns=tuple(new_columns), version=schema.version + 1)
            errors: list[str] = []
            if current.storage_version >= 2:
                rewritten: list[tuple[str, dict[str, str]]] = []
                for data_row in self._app_repo.list_all_data_rows(current.app_id, key):
                    values = {
                        clean_renames.get(name, name): value
                        for name, value in data_row.values.items()
                        if name not in clean_drops
                    }
                    try:
                        rewritten.append((data_row.row_id, validate_row(new_schema, values)))
                    except OctopError as exc:
                        if len(errors) < MAX_IMPORT_ERRORS:
                            errors.append(f"row {data_row.row_id}: {exc.message}")
                if errors:
                    raise _invalid(
                        ErrorCode.APP_ROW_INVALID,
                        "schema change rejected; existing rows do not fit the new schema",
                        errors=errors,
                    )
                self._app_repo.set_table_schema(
                    current.app_id,
                    key,
                    new_schema.to_json(),
                    new_schema.version,
                )
                self._app_repo.rewrite_data_rows(current.app_id, key, rewritten)
            else:
                rows = await self._read_rows_file(ws, current.app_id, schema, key)
                rewritten_rows: list[dict[str, str]] = []
                for index, values_item in enumerate(rows):
                    values = {
                        clean_renames.get(name, name): value
                        for name, value in values_item.items()
                        if name not in clean_drops
                    }
                    try:
                        rewritten_rows.append(validate_row(new_schema, values))
                    except OctopError as exc:
                        if len(errors) < MAX_IMPORT_ERRORS:
                            errors.append(f"row {index + 1}: {exc.message}")
                if errors:
                    raise _invalid(
                        ErrorCode.APP_ROW_INVALID,
                        "schema change rejected; existing rows do not fit the new schema",
                        errors=errors,
                    )
                await self._persist_rows(ws, current, key, new_schema, rewritten_rows)
            schema_file, _ = table_file_names(key)
            try:
                await ws.awrite_text(
                    app_path(current.app_id, schema_file),
                    new_schema.to_json(),
                )
            except Exception:
                logger.warning(
                    "failed to refresh schema mirror for %s/%s",
                    current.app_id,
                    key,
                    exc_info=True,
                )
            self._app_repo.touch(current.app_id)
            await self._refresh_manifest(ws, current.app_id)
            return new_schema

    # ----- sharing -----

    def list_collaborators(self, row: AppRecord) -> list[LightAppCollaboratorRow]:
        return self._app_repo.list_collaborators(row.app_id)

    def list_share_views(self, row: AppRecord) -> list[ShareView]:
        views: list[ShareView] = []
        for collab in self._app_repo.list_collaborators(row.app_id):
            account = self._user_repo.get(collab.user_id)
            if account is None:
                continue
            views.append(self._share_view(account, collab.permission))
        return views

    def set_shares(
        self,
        row: AppRecord,
        shares: list[tuple[str, str]],
    ) -> list[ShareView]:
        """Replace collaborators from username/permission pairs."""
        entries: list[tuple[int, str]] = []
        seen: set[int] = set()
        for username, permission in shares:
            name = username.strip()
            account = self._user_repo.get_by_username(name) if name else None
            if account is None or account.disabled:
                raise _invalid(
                    ErrorCode.APP_SHARE_INVALID,
                    f"unknown or disabled user: {name}",
                )
            if account.id in seen:
                raise _invalid(
                    ErrorCode.APP_SHARE_INVALID,
                    f"duplicate collaborator: {name}",
                )
            seen.add(account.id)
            entries.append((account.id, permission))
        self.set_collaborators(row, entries)
        return self.list_share_views(row)

    def set_collaborators(
        self,
        row: AppRecord,
        entries: list[tuple[int, str]],
    ) -> list[LightAppCollaboratorRow]:
        if len(entries) > MAX_COLLABORATORS:
            raise _invalid(
                ErrorCode.APP_SHARE_INVALID,
                f"at most {MAX_COLLABORATORS} collaborators",
            )
        cleaned: list[tuple[int, str]] = []
        seen: set[int] = set()
        for user_id, permission in entries:
            if user_id == row.owner_user_id:
                raise _invalid(
                    ErrorCode.APP_SHARE_INVALID,
                    "the owner always has full access and cannot be a collaborator",
                )
            if user_id in seen:
                raise _invalid(
                    ErrorCode.APP_SHARE_INVALID,
                    "each collaborator can appear only once",
                )
            seen.add(user_id)
            cleaned.append((user_id, validate_permission(permission)))
        self._app_repo.set_collaborators(row.app_id, cleaned)
        self._app_repo.touch(row.app_id)
        return self._app_repo.list_collaborators(row.app_id)

    # ----- presentation helpers -----

    @staticmethod
    def _share_view(account: UserRow, permission: str) -> ShareView:
        return ShareView(
            user_id=account.id,
            username=account.username,
            display_name=account.display_name or account.username,
            permission=permission,
        )
