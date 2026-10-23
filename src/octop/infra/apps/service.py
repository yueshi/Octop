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
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any, Protocol

from octop_harness.backends import BackendWorkspace

from octop.infra.apps.models import (
    MAX_APPS_PER_OWNER,
    MAX_COLLABORATORS,
    MAX_CSV_BYTES,
    MAX_ROW_READ,
    MAX_ROWS,
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
)
from octop.infra.db.repos.agents import AgentRepo
from octop.infra.db.repos.apps import (
    PERMISSION_RANK,
    LightAppCollaboratorRow,
    LightAppRepo,
    LightAppRow,
)
from octop.infra.db.repos.users import UserRepo, UserRow
from octop.infra.errors import ErrorCode, OctopError

logger = logging.getLogger(__name__)

APPS_DIR = "apps"
FILE_HTML = "app.html"
FILE_DATA = "data.csv"
FILE_SCHEMA = "schema.json"
FILE_MANIFEST = "manifest.json"
FILE_PENDING = ".pending.json"
STAGED_SUFFIX = ".new"
_ATOMIC_APP_FILES = frozenset({FILE_DATA, FILE_SCHEMA})

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


class WorkspaceSource(Protocol):
    def workspace_for_agent(self, agent_id: str) -> BackendWorkspace | None: ...


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
    ) -> None:
        self._app_repo = app_repo
        self._agent_repo = agent_repo
        self._user_repo = user_repo
        self._workspaces = workspaces

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
        """Cached (html_bytes, data_bytes), refreshed from the workspace when unknown."""
        ws = await self._workspace(row.agent_id)
        sizes = await self._dir_sizes(ws, row.app_id)
        html_bytes = sizes.get(FILE_HTML, 0)
        data_bytes = sizes.get(FILE_DATA, 0)
        if html_bytes != row.html_bytes or data_bytes != row.data_bytes:
            self._app_repo.set_file_stats(
                row.app_id,
                html_bytes=html_bytes,
                data_bytes=data_bytes,
            )
        return html_bytes, data_bytes

    async def _dir_sizes(self, ws: BackendWorkspace, app_id: str) -> dict[str, int]:
        """File sizes of the app directory, keyed by file name."""
        result = await ws.als(app_dir(app_id))
        entries = list(getattr(result, "entries", None) or []) if result is not None else []
        sizes: dict[str, int] = {}
        for entry in entries:
            if _entry_field(entry, "is_dir"):
                continue
            path = str(_entry_field(entry, "path") or "").replace("\\", "/")
            name = path.rsplit("/", 1)[-1]
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

    async def _read_schema(self, ws: BackendWorkspace, app_id: str) -> Schema:
        await self._recover(ws, app_id)
        text = await ws.aread_text(app_path(app_id, FILE_SCHEMA))
        if text is None:
            raise _invalid(ErrorCode.APP_SCHEMA_INVALID, "schema.json is missing")
        return parse_schema_payload(text)

    async def _read_rows(
        self, ws: BackendWorkspace, app_id: str, schema: Schema
    ) -> list[dict[str, str]]:
        text = await ws.aread_text(app_path(app_id, FILE_DATA))
        if text is None:
            # aread_text is fail-soft: None also covers transient backend
            # errors. Treating that as an empty file would overwrite every
            # existing row on the next append.
            raise OctopError(ErrorCode.INTERNAL_ERROR, "failed to read app data.csv")
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
        if not files or not set(files) <= _ATOMIC_APP_FILES:
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
        if not names or not set(names) <= _ATOMIC_APP_FILES:
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
    def _manifest_text(row: AppRecord, schema_version: int) -> str:
        manifest = {
            "app_id": row.app_id,
            "name": row.name,
            "description": row.description,
            "agent_id": row.agent_id,
            "owner_user_id": row.owner_user_id,
            "row_count": row.row_count,
            "schema_version": schema_version,
            "created_at": row.created_at,
            "updated_at": row.updated_at,
        }
        return json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"

    async def _refresh_manifest(
        self, ws: BackendWorkspace, app_id: str, schema_version: int
    ) -> None:
        row = self._app_repo.get(app_id)
        if row is None:
            return
        try:
            await ws.awrite_text(
                app_path(app_id, FILE_MANIFEST),
                self._manifest_text(row, schema_version),
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
            await ws.awrite_text(app_path(app_id, FILE_SCHEMA), schema.to_json())
            empty_csv = render_csv(schema, [])
            await ws.awrite_text(app_path(app_id, FILE_DATA), empty_csv)
            await ws.awrite_text(
                app_path(app_id, FILE_MANIFEST),
                self._manifest_text(row, schema.version),
            )
            self._app_repo.set_file_stats(
                app_id,
                html_bytes=0,
                data_bytes=len(empty_csv.encode("utf-8")),
            )
        except Exception:
            self._app_repo.delete(app_id)
            try:
                await ws.adelete(app_dir(app_id))
            except Exception:
                logger.warning("failed to clean up app workspace files: %s", app_id, exc_info=True)
            raise
        return row

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
            try:
                schema = await self._read_schema(ws, row.app_id)
            except OctopError:
                schema_version = 1
            else:
                schema_version = schema.version
            await self._refresh_manifest(ws, row.app_id, schema_version)
            return self.get(row.app_id)

    async def delete(self, row: AppRecord) -> None:
        try:
            async with _lock_for(row.app_id):
                ws = await asyncio.to_thread(self._workspaces.workspace_for_agent, row.agent_id)
                self._app_repo.delete(row.app_id)
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

        The four app files are known, so the listing stays stable even on
        backends that report no size.
        """
        ws = await self._workspace(row.agent_id)
        sizes = await self._dir_sizes(ws, row.app_id)
        return [
            AppFile(
                name=name,
                path=app_path(row.app_id, name),
                size=sizes.get(name),
            )
            for name in (FILE_HTML, FILE_DATA, FILE_SCHEMA, FILE_MANIFEST)
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
            try:
                schema_version = (await self._read_schema(ws, row.app_id)).version
            except OctopError:
                schema_version = 1
            await self._refresh_manifest(ws, row.app_id, schema_version)
        return size

    async def read_schema(self, row: AppRecord) -> Schema:
        async with _lock_for(row.app_id):
            self._require_live(row.app_id)
            ws = await self._workspace(row.agent_id)
            return await self._read_schema(ws, row.app_id)

    async def read_rows(
        self,
        row: AppRecord,
        *,
        limit: int = MAX_ROW_READ,
        offset: int = 0,
    ) -> tuple[Schema, list[dict[str, str]], int]:
        safe_limit = max(1, min(int(limit), MAX_ROW_READ))
        safe_offset = max(0, int(offset))
        async with _lock_for(row.app_id):
            self._require_live(row.app_id)
            ws = await self._workspace(row.agent_id)
            schema = await self._read_schema(ws, row.app_id)
            rows = await self._read_rows(ws, row.app_id, schema)
            total = len(rows)
            # ``row_count`` is a list-view cache; reconcile it when a file-level
            # edit (or an interrupted append) made it drift from data.csv.
            # Reads must not bump updated_at or the list re-sorts on every GET.
            if total != row.row_count:
                self._app_repo.set_row_count(row.app_id, total, touch=False)
            return schema, rows[safe_offset : safe_offset + safe_limit], total

    async def append_row(
        self,
        row: AppRecord,
        values: Mapping[str, object],
    ) -> tuple[int, dict[str, str]]:
        async with _lock_for(row.app_id):
            self._require_live(row.app_id)
            ws = await self._workspace(row.agent_id)
            schema = await self._read_schema(ws, row.app_id)
            rows = await self._read_rows(ws, row.app_id, schema)
            if len(rows) >= MAX_ROWS:
                raise _invalid(
                    ErrorCode.APP_LIMIT_EXCEEDED,
                    f"app has reached the {MAX_ROWS}-row limit",
                    limit=MAX_ROWS,
                )
            normalized = validate_row(schema, values)
            rows.append(normalized)
            csv_text = render_csv(schema, rows)
            if len(csv_text.encode("utf-8")) > MAX_CSV_BYTES:
                raise _invalid(
                    ErrorCode.APP_LIMIT_EXCEEDED,
                    f"data.csv would exceed {MAX_CSV_BYTES // (1024 * 1024)} MB",
                    limit=MAX_CSV_BYTES,
                )
            await self._write_atomic(ws, row.app_id, {FILE_DATA: csv_text})
            self._app_repo.set_row_count(row.app_id, len(rows))
            self._app_repo.set_file_stats(
                row.app_id,
                data_bytes=len(csv_text.encode("utf-8")),
            )
            await self._refresh_manifest(ws, row.app_id, schema.version)
            return len(rows) - 1, normalized

    async def add_columns(self, row: AppRecord, additions: object) -> Schema:
        async with _lock_for(row.app_id):
            self._require_live(row.app_id)
            ws = await self._workspace(row.agent_id)
            schema = await self._read_schema(ws, row.app_id)
            rows = await self._read_rows(ws, row.app_id, schema)
            new_schema = extend_schema(schema, additions)
            added = new_schema.columns[len(schema.columns) :]
            if rows and any(col.required for col in added):
                raise _invalid(
                    ErrorCode.APP_SCHEMA_INVALID,
                    "new columns cannot be required while the app already has rows",
                )
            csv_text = render_csv(new_schema, rows)
            await self._write_atomic(
                ws,
                row.app_id,
                {
                    FILE_SCHEMA: new_schema.to_json(),
                    FILE_DATA: csv_text,
                },
            )
            self._app_repo.touch(row.app_id)
            self._app_repo.set_file_stats(
                row.app_id,
                data_bytes=len(csv_text.encode("utf-8")),
            )
            await self._refresh_manifest(ws, row.app_id, new_schema.version)
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
