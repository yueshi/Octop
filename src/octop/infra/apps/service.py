"""Light app lifecycle: workspace files + control-plane metadata.

Workspace layout (workspace-relative, resolved through ``BackendWorkspace``)::

    apps/<app_id>/app.html      rendered UI (written by the agent)
    apps/<app_id>/data.csv      rows; header == schema column names
    apps/<app_id>/schema.json   column definitions
    apps/<app_id>/manifest.json human-readable snapshot

Schema and data updates are staged to ``*.new`` files and committed through
``.pending.json`` so an interrupted write is finished by the next read instead
of leaving the pair truncated or inconsistent.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any, Protocol

from octop_harness.backends import BackendWorkspace

from octop.infra.apps.importer import parse_import_file
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
    Schema,
    extend_schema,
    parse_csv,
    parse_schema_payload,
    render_csv,
    validate_app_id,
    validate_description,
    validate_html,
    validate_name,
    validate_permission,
    validate_row,
    validate_table_key,
)
from octop.infra.db.repos.agents import AgentRepo
from octop.infra.db.repos.apps import (
    PERMISSION_RANK,
    LightAppCollaboratorRow,
    LightAppRepo,
    LightAppRow,
    LightAppSourceRow,
)
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
TABLES_DIR = "tables"
STAGED_SUFFIX = ".new"

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


class AppService:
    def __init__(
        self,
        *,
        app_repo: LightAppRepo,
        agent_repo: AgentRepo,
        user_repo: UserRepo,
        workspaces: WorkspaceSource,
        external_tool_runner: ExternalToolRunner | None = None,
    ) -> None:
        self._app_repo = app_repo
        self._agent_repo = agent_repo
        self._user_repo = user_repo
        self._workspaces = workspaces
        self._external_tool_runner = external_tool_runner

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
        )

    def list_views(self, user_id: int, *, is_admin: bool) -> list[AppView]:
        owners: dict[int, str] = {}
        return [
            self.view(row, permission, owners=owners)
            for row, permission in self.list_for_user(user_id, is_admin=is_admin)
        ]

    async def list_views_with_stats(self, user_id: int, *, is_admin: bool) -> list[AppView]:
        """List views, backfilling the cached file sizes for apps that predate them.

        Apps written before the file-size columns existed carry cached sizes of
        zero even though their files are on disk; listing the app directory
        once repairs the cache instead of showing them as "no interface yet".
        """
        out: list[AppView] = []
        for view in self.list_views(user_id, is_admin=is_admin):
            if view.html_bytes and view.data_bytes:
                out.append(view)
                continue
            row = self.get(view.app_id)
            html_bytes, data_bytes = await self._file_stats(row)
            out.append(
                replace(view, html_bytes=html_bytes, data_bytes=data_bytes)
                if (html_bytes, data_bytes) != (view.html_bytes, view.data_bytes)
                else view,
            )
        return out

    async def _file_stats(self, row: AppRecord) -> tuple[int, int]:
        """Cached (html_bytes, data_bytes), refreshed from the workspace when unknown.

        ``data_bytes`` covers every table's CSV, matching the aggregate the
        registry maintains on writes.
        """
        ws = await self._workspace(row.agent_id)
        sizes = await self._dir_sizes(ws, row.app_id)
        html_bytes = sizes.get(FILE_HTML, 0)
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
        await self._recover(ws, app_id)
        schema_file, _ = table_file_names(table_key)
        text = await ws.aread_text(app_path(app_id, schema_file))
        if text is None:
            raise _invalid(
                ErrorCode.APP_SCHEMA_INVALID,
                f"schema file for table {table_key!r} is missing",
            )
        return parse_schema_payload(text)

    async def _read_rows(
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
                }
            )
        try:
            await ws.awrite_text(
                app_path(app_id, FILE_MANIFEST),
                self._manifest_text(row, tables),
            )
        except Exception:
            logger.warning("failed to refresh app manifest: %s", app_id, exc_info=True)

    # ----- create / metadata -----

    async def create(
        self,
        *,
        agent_id: str,
        owner_user_id: int,
        name: str,
        description: str = "",
        columns: object,
    ) -> LightAppRow:
        clean_name = validate_name(name)
        clean_description = validate_description(description)
        schema = parse_schema_payload(columns)
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
        )
        try:
            empty_csv = render_csv(schema, [])
            await ws.awrite_text(app_path(app_id, FILE_SCHEMA), schema.to_json())
            await ws.awrite_text(app_path(app_id, FILE_DATA), empty_csv)
            data_bytes = len(empty_csv.encode("utf-8"))
            self._app_repo.create_table(app_id, MAIN_TABLE_KEY)
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
        self._app_repo.create_table(row.app_id, MAIN_TABLE_KEY)
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
            self._app_repo.create_table(row.app_id, key, clean_label)
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
        )

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
        return [
            AppFile(
                name=name,
                path=app_path(row.app_id, name),
                size=sizes.get(name),
            )
            for name in names
        ]

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
    ) -> tuple[Schema, list[dict[str, str]], int]:
        key = self._require_table(row, table)
        safe_limit = max(1, min(int(limit), MAX_ROW_READ))
        safe_offset = max(0, int(offset))
        async with _lock_for(row.app_id):
            self._require_live(row.app_id)
            ws = await self._workspace(row.agent_id)
            schema = await self._read_schema(ws, row.app_id, key)
            rows = await self._read_rows(ws, row.app_id, schema, key)
            total = len(rows)
            # ``row_count`` is a list-view cache; reconcile it when a file-level
            # edit (or an interrupted append) made it drift from the CSV.
            # Reads must not bump updated_at or the list re-sorts on every GET.
            cached = self._app_repo.get_table(row.app_id, key)
            if cached is not None:
                if total != cached.row_count:
                    self._app_repo.set_table_row_count(row.app_id, key, total)
                    self._app_repo.refresh_app_totals(row.app_id)
            elif key == MAIN_TABLE_KEY and total != row.row_count:
                # Legacy app: only ``light_apps.row_count`` tracks the main CSV.
                self._app_repo.set_row_count(row.app_id, total, touch=False)
            return schema, rows[safe_offset : safe_offset + safe_limit], total

    async def query_rows(
        self,
        row: AppRecord,
        *,
        table: str = MAIN_TABLE_KEY,
        filters: Mapping[str, object] | None = None,
        limit: int = MAX_ROW_READ,
        offset: int = 0,
    ) -> tuple[Schema, list[dict[str, str]], int]:
        """Read rows of one table, keeping only rows matching *filters*.

        Filter values compare case-insensitively against the stored cell text;
        unknown filter columns raise instead of silently matching nothing.
        """
        key = self._require_table(row, table)
        clean_filters = {str(col).strip(): str(val) for col, val in (filters or {}).items()}
        safe_limit = max(1, min(int(limit), MAX_ROW_READ))
        safe_offset = max(0, int(offset))
        async with _lock_for(row.app_id):
            self._require_live(row.app_id)
            ws = await self._workspace(row.agent_id)
            schema = await self._read_schema(ws, row.app_id, key)
            for column in clean_filters:
                if schema.column(column) is None:
                    raise _invalid(
                        ErrorCode.APP_SCHEMA_INVALID,
                        f"unknown filter column: {column!r}",
                        table=key,
                    )
            rows = await self._read_rows(ws, row.app_id, schema, key)
        if clean_filters:
            wanted = {col: val.strip().lower() for col, val in clean_filters.items()}
            rows = [
                item
                for item in rows
                if all(item.get(col, "").strip().lower() == val for col, val in wanted.items())
            ]
        return schema, rows[safe_offset : safe_offset + safe_limit], len(rows)

    async def append_row(
        self,
        row: AppRecord,
        values: Mapping[str, object],
        *,
        table: str = MAIN_TABLE_KEY,
    ) -> tuple[int, dict[str, str]]:
        key = self._require_table(row, table)
        async with _lock_for(row.app_id):
            self._require_live(row.app_id)
            self._ensure_main_table(row)
            ws = await self._workspace(row.agent_id)
            schema = await self._read_schema(ws, row.app_id, key)
            rows = await self._read_rows(ws, row.app_id, schema, key)
            if len(rows) >= MAX_ROWS:
                raise _invalid(
                    ErrorCode.APP_LIMIT_EXCEEDED,
                    f"table has reached the {MAX_ROWS}-row limit",
                    limit=MAX_ROWS,
                )
            normalized = validate_row(schema, values)
            rows.append(normalized)
            await self._persist_rows(ws, row, key, schema, rows)
            return len(rows) - 1, normalized

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

    async def save_import_file(self, row: AppRecord, *, filename: str, data: bytes) -> str:
        """Store an uploaded import file under ``inbound/imports/`` in the workspace."""
        ws = await self._workspace(row.agent_id)
        path = f"{IMPORT_DIR}/{new_short_id(8)}-{_safe_import_filename(filename)}"
        await ws.aupload_bytes(path, data)
        return path

    async def import_from_file(
        self,
        row: AppRecord,
        *,
        path: str,
        table: str = MAIN_TABLE_KEY,
        mode: str = IMPORT_MODE_APPEND,
        mapping: Mapping[str, str] | None = None,
        data: bytes | None = None,
    ) -> ImportResult:
        """Parse a workspace file (or raw bytes) and import it into one table."""
        source_path = _safe_import_path(path)
        if data is None:
            ws = await self._workspace(row.agent_id)
            data = await ws.adownload_bytes(source_path)
            if data is None:
                raise _invalid(
                    ErrorCode.APP_ROW_INVALID,
                    f"import file not found: {source_path}",
                )
        records = parse_import_file(source_path, data)
        if not records:
            raise _invalid(
                ErrorCode.APP_ROW_INVALID,
                f"import file has no data rows: {source_path}",
            )
        return await self.import_rows(row, records, table=table, mode=mode, mapping=mapping)

    async def import_rows(
        self,
        row: AppRecord,
        records: list[dict[str, str]],
        *,
        table: str = MAIN_TABLE_KEY,
        mode: str = IMPORT_MODE_APPEND,
        mapping: Mapping[str, str] | None = None,
    ) -> ImportResult:
        """Validate and write parsed records into one table (append or replace).

        Rows that fail validation are skipped and counted, not fatal; a
        replace that would wipe the table because *every* record failed is
        rejected instead of silently emptying it.
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
        async with _lock_for(row.app_id):
            self._require_live(row.app_id)
            self._ensure_main_table(row)
            ws = await self._workspace(row.agent_id)
            schema = await self._read_schema(ws, row.app_id, key)
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
            imported_rows: list[dict[str, str]] = []
            errors: list[str] = []
            for index, record in enumerate(clean):
                target = {dst: str(record.get(src, "")) for src, dst in source_map.items()}
                try:
                    imported_rows.append(validate_row(schema, target))
                except OctopError as exc:
                    if len(errors) < MAX_IMPORT_ERRORS:
                        errors.append(f"row {index + 1}: {exc.message}")
                    continue
            if mode == IMPORT_MODE_REPLACE:
                if clean and not imported_rows:
                    raise _invalid(
                        ErrorCode.APP_ROW_INVALID,
                        "every row failed validation; table left unchanged",
                        errors=errors,
                    )
                rows = imported_rows
            else:
                rows = await self._read_rows(ws, row.app_id, schema, key)
                rows = rows + imported_rows
            if len(rows) > MAX_ROWS:
                raise _invalid(
                    ErrorCode.APP_LIMIT_EXCEEDED,
                    f"table has reached the {MAX_ROWS}-row limit",
                    limit=MAX_ROWS,
                )
            await self._persist_rows(ws, row, key, schema, rows)
            return ImportResult(
                imported=len(imported_rows),
                skipped=len(clean) - len(imported_rows),
                errors=errors,
            )

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
        params_json = json.dumps(clean_params, ensure_ascii=False)
        mapping_json = json.dumps(clean_mapping, ensure_ascii=False)
        existing = self._app_repo.get_source(row.app_id, source_id)
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
            if item.kind == SOURCE_KIND_WORKSPACE_FILE:
                result = await self.import_from_file(
                    row,
                    path=str(params.get("path") or ""),
                    table=item.table_key,
                    mode=item.mode,
                    mapping=json.loads(item.mapping_json),
                )
            elif item.kind == SOURCE_KIND_EXTERNAL:
                result = await self._run_external_source(row, item, params)
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
        output = await asyncio.to_thread(
            self._external_tool_runner.run,
            instance_id=instance_id,
            tool=tool,
            args={str(k): v for k, v in args.items()},
        )
        records = _extract_records(output, result_path)
        return await self.import_rows(
            row,
            records,
            table=item.table_key,
            mode=item.mode,
            mapping=json.loads(item.mapping_json),
        )

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
            return {"path": path}
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
        return clean

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
        async with _lock_for(row.app_id):
            self._require_live(row.app_id)
            self._ensure_main_table(row)
            ws = await self._workspace(row.agent_id)
            schema = await self._read_schema(ws, row.app_id, key)
            rows = await self._read_rows(ws, row.app_id, schema, key)
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
                row.app_id,
                {
                    schema_file: new_schema.to_json(),
                    data_file: csv_text,
                },
            )
            self._app_repo.touch(row.app_id)
            data_bytes = len(csv_text.encode("utf-8"))
            self._app_repo.set_table_stats(
                row.app_id,
                key,
                row_count=len(rows),
                data_bytes=data_bytes,
            )
            self._app_repo.refresh_app_totals(row.app_id)
            await self._refresh_manifest(ws, row.app_id)
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
