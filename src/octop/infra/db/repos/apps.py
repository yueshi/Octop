"""Light app metadata and sharing ACL — SQL only.

App content files live in the owning agent's workspace and are handled by
``octop.infra.apps``; this repo owns the control-plane index so lists, sharing
and permission checks do not need to touch every agent workspace.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from octop.infra.db.pool import DatabasePool
from octop.infra.db.repos._base import DbRow, map_rows, now_ts, partial_updates
from octop.infra.utils.ulid import new_short_id

PERMISSION_VIEW = "view"
PERMISSION_EDIT_DATA = "edit_data"
PERMISSION_EDIT_STRUCTURE = "edit_structure"
PERMISSION_RANK = {
    PERMISSION_VIEW: 1,
    PERMISSION_EDIT_DATA: 2,
    PERMISSION_EDIT_STRUCTURE: 3,
}


@dataclass(frozen=True)
class LightAppRow:
    app_id: str
    pk: int
    agent_id: str
    owner_user_id: int
    name: str
    description: str
    row_count: int
    html_bytes: int
    data_bytes: int
    storage_version: int
    capabilities_json: str
    previous_html: str
    previous_html_at: int | None
    created_at: int
    updated_at: int

    @classmethod
    def from_row(cls, r: DbRow) -> LightAppRow:
        return cls(
            app_id=str(r["app_id"]),
            pk=int(r["id"]),
            agent_id=str(r["agent_id"]),
            owner_user_id=int(r["owner_user_id"]),
            name=str(r["name"]),
            description=str(r["description"] or ""),
            row_count=int(r["row_count"]),
            html_bytes=int(r["html_bytes"] or 0),
            data_bytes=int(r["data_bytes"] or 0),
            storage_version=int(r["storage_version"] or 1),
            capabilities_json=str(r["capabilities_json"] or ""),
            previous_html=str(r["previous_html"] or ""),
            previous_html_at=(
                int(r["previous_html_at"]) if r["previous_html_at"] is not None else None
            ),
            created_at=int(r["created_at"]),
            updated_at=int(r["updated_at"]),
        )


@dataclass(frozen=True)
class LightAppCollaboratorRow:
    app_id: str
    user_id: int
    permission: str
    created_at: int
    updated_at: int

    @classmethod
    def from_row(cls, r: DbRow) -> LightAppCollaboratorRow:
        return cls(
            app_id=str(r["app_id"]),
            user_id=int(r["user_id"]),
            permission=str(r["permission"]),
            created_at=int(r["created_at"]),
            updated_at=int(r["updated_at"]),
        )


@dataclass(frozen=True)
class LightAppTableRow:
    """One table of an app: the registry entry the dashboard lists."""

    app_id: str
    table_key: str
    label: str
    row_count: int
    data_bytes: int
    schema_json: str
    schema_version: int
    data_version: int
    public_visible: bool
    created_at: int
    updated_at: int

    @classmethod
    def from_row(cls, r: DbRow) -> LightAppTableRow:
        return cls(
            app_id=str(r["app_id"]),
            table_key=str(r["table_key"]),
            label=str(r["label"] or ""),
            row_count=int(r["row_count"] or 0),
            data_bytes=int(r["data_bytes"] or 0),
            schema_json=str(r["schema_json"] or ""),
            schema_version=int(r["schema_version"] or 1),
            data_version=int(r["data_version"] or 0),
            public_visible=bool(int(r["public_visible"] or 0)),
            created_at=int(r["created_at"]),
            updated_at=int(r["updated_at"]),
        )


@dataclass(frozen=True)
class LightAppPublicationRow:
    """One read-only public link for an app; the token is only stored hashed."""

    app_id: str
    token_hash: str
    created_by: int
    created_at: int
    revoked_at: int | None
    expires_at: int | None
    html_snapshot: str
    tables_snapshot_json: str
    snapshot_digest: str
    snapshot_at: int | None

    @classmethod
    def from_row(cls, r: DbRow) -> LightAppPublicationRow:
        return cls(
            app_id=str(r["app_id"]),
            token_hash=str(r["token_hash"]),
            created_by=int(r["created_by"]),
            created_at=int(r["created_at"]),
            revoked_at=int(r["revoked_at"]) if r["revoked_at"] is not None else None,
            expires_at=int(r["expires_at"]) if r["expires_at"] is not None else None,
            html_snapshot=str(r["html_snapshot"] or ""),
            tables_snapshot_json=str(r["tables_snapshot_json"] or ""),
            snapshot_digest=str(r["snapshot_digest"] or ""),
            snapshot_at=int(r["snapshot_at"]) if r["snapshot_at"] is not None else None,
        )


@dataclass(frozen=True)
class LightAppDataRow:
    """One data row of an app table (v2 storage)."""

    row_id: str
    app_id: str
    table_key: str
    values: dict[str, str]
    version: int
    source: str
    created_at: int
    updated_at: int

    @classmethod
    def from_row(cls, r: DbRow) -> LightAppDataRow:
        raw = r["values_json"]
        try:
            parsed = json.loads(raw) if isinstance(raw, str) else raw
        except ValueError:
            parsed = {}
        values = {str(k): str(v) for k, v in parsed.items()} if isinstance(parsed, dict) else {}
        return cls(
            row_id=str(r["row_id"]),
            app_id=str(r["app_id"]),
            table_key=str(r["table_key"]),
            values=values,
            version=int(r["version"] or 1),
            source=str(r["source"] or "manual"),
            created_at=int(r["created_at"]),
            updated_at=int(r["updated_at"]),
        )


ASSISTANT_HISTORY_KEEP = 200
"""Assistant messages kept per (app, user); older rows are trimmed on insert."""

ASSISTANT_ROLE_USER = "user"
ASSISTANT_ROLE_ASSISTANT = "assistant"


@dataclass(frozen=True)
class LightAppAssistantMessageRow:
    """One turn of an app assistant conversation."""

    role: str
    content: str
    created_at: int

    @classmethod
    def from_row(cls, r: DbRow) -> LightAppAssistantMessageRow:
        return cls(
            role=str(r["role"]),
            content=str(r["content"]),
            created_at=int(r["created_at"]),
        )


@dataclass(frozen=True)
class LightAppSourceRow:
    """One data-source binding of an app table."""

    source_id: str
    app_id: str
    table_key: str
    kind: str
    mode: str
    schedule_seconds: int
    params_json: str
    mapping_json: str
    next_run_at: int | None
    fail_count: int
    last_run_at: int | None
    last_status: str | None
    last_error: str | None
    last_row_count: int | None
    created_by: int
    created_at: int
    updated_at: int

    @classmethod
    def from_row(cls, r: DbRow) -> LightAppSourceRow:
        return cls(
            source_id=str(r["source_id"]),
            app_id=str(r["app_id"]),
            table_key=str(r["table_key"]),
            kind=str(r["kind"]),
            mode=str(r["mode"]),
            schedule_seconds=int(r["schedule_seconds"]),
            params_json=str(r["params_json"] or "{}"),
            mapping_json=str(r["mapping_json"] or "{}"),
            next_run_at=(int(r["next_run_at"]) if r["next_run_at"] is not None else None),
            fail_count=int(r["fail_count"] or 0),
            last_run_at=(int(r["last_run_at"]) if r["last_run_at"] is not None else None),
            last_status=(str(r["last_status"]) if r["last_status"] is not None else None),
            last_error=str(r["last_error"]) if r["last_error"] is not None else None,
            last_row_count=(int(r["last_row_count"]) if r["last_row_count"] is not None else None),
            created_by=int(r["created_by"]),
            created_at=int(r["created_at"]),
            updated_at=int(r["updated_at"]),
        )


class LightAppRepo:
    def __init__(self, db: DatabasePool) -> None:
        self._db = db

    def allocate_id(self) -> str:
        for _ in range(16):
            app_id = f"app_{new_short_id(10)}"
            if self.get(app_id) is None:
                return app_id
        raise RuntimeError("failed to allocate unique app id")

    def create(
        self,
        *,
        agent_id: str,
        owner_user_id: int,
        name: str,
        description: str = "",
        app_id: str | None = None,
        storage_version: int = 1,
        capabilities_json: str = "",
    ) -> LightAppRow:
        app_id = app_id or self.allocate_id()
        ts = now_ts()
        with self._db.transaction() as conn:
            conn.execute(
                "INSERT INTO light_apps("
                "app_id, agent_id, owner_user_id, name, description, row_count, "
                "storage_version, capabilities_json, created_at, updated_at"
                ") VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?, ?)",
                (
                    app_id,
                    agent_id,
                    owner_user_id,
                    name,
                    description,
                    storage_version,
                    capabilities_json,
                    ts,
                    ts,
                ),
            )
        row = self.get(app_id)
        if row is None:
            raise RuntimeError(f"light app insert failed: {app_id}")
        return row

    def get(self, app_id: str) -> LightAppRow | None:
        with self._db.connect() as conn:
            r = conn.execute(
                "SELECT * FROM light_apps WHERE app_id = ?",
                (app_id,),
            ).fetchone()
        return LightAppRow.from_row(r) if r else None

    def list_owned(self, owner_user_id: int) -> list[LightAppRow]:
        with self._db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM light_apps WHERE owner_user_id = ? ORDER BY updated_at DESC",
                (owner_user_id,),
            ).fetchall()
        return map_rows(rows, LightAppRow)

    def list_shared(self, user_id: int) -> list[LightAppRow]:
        with self._db.connect() as conn:
            rows = conn.execute(
                "SELECT a.* FROM light_apps a "
                "JOIN light_app_collaborators c ON c.app_id = a.app_id "
                "WHERE c.user_id = ? AND a.owner_user_id <> ? "
                "ORDER BY a.updated_at DESC",
                (user_id, user_id),
            ).fetchall()
        return map_rows(rows, LightAppRow)

    def list_all(self) -> list[LightAppRow]:
        with self._db.connect() as conn:
            rows = conn.execute("SELECT * FROM light_apps ORDER BY updated_at DESC").fetchall()
        return map_rows(rows, LightAppRow)

    def list_for_agent(self, agent_id: str, owner_user_id: int) -> list[LightAppRow]:
        with self._db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM light_apps WHERE agent_id = ? AND owner_user_id = ? "
                "ORDER BY updated_at DESC",
                (agent_id, owner_user_id),
            ).fetchall()
        return map_rows(rows, LightAppRow)

    def list_by_agent(self, agent_id: str) -> list[LightAppRow]:
        """Every app hosted in an agent's workspace, whoever created it."""
        with self._db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM light_apps WHERE agent_id = ? ORDER BY updated_at DESC",
                (agent_id,),
            ).fetchall()
        return map_rows(rows, LightAppRow)

    def set_agent_id(self, app_id: str, agent_id: str) -> None:
        """Move an app to another agent's workspace."""
        with self._db.transaction() as conn:
            conn.execute(
                "UPDATE light_apps SET agent_id = ?, updated_at = ? WHERE app_id = ?",
                (agent_id, now_ts(), app_id),
            )

    # ----- data rows (v2 storage) -----

    def append_data_row(
        self,
        app_id: str,
        table_key: str,
        values: dict[str, str],
        *,
        source: str = "manual",
        source_id: str | None = None,
        source_key: str | None = None,
        client_request_id: str | None = None,
        created_by: int | None = None,
        row_id: str | None = None,
    ) -> tuple[int, LightAppDataRow]:
        """Insert one row, or return the existing row for an idempotent retry."""
        ts = now_ts()
        payload = json.dumps(values, ensure_ascii=False)
        rid = row_id or f"row_{new_short_id(12)}"
        with self._db.transaction() as conn:
            if client_request_id is not None:
                existing = conn.execute(
                    "SELECT * FROM light_app_rows WHERE app_id = ? AND client_request_id = ?",
                    (app_id, client_request_id),
                ).fetchone()
                if existing is not None:
                    return self._row_index(conn, app_id, table_key, int(existing["id"])), (
                        LightAppDataRow.from_row(existing)
                    )
            if source_id is not None and source_key is not None:
                existing = conn.execute(
                    "SELECT * FROM light_app_rows WHERE app_id = ? AND table_key = ? "
                    "AND source_id = ? AND source_key = ?",
                    (app_id, table_key, source_id, source_key),
                ).fetchone()
                if existing is not None:
                    old_len = len(str(existing["values_json"]))
                    conn.execute(
                        "UPDATE light_app_rows SET values_json = ?, version = version + 1, "
                        "updated_at = ? WHERE id = ?",
                        (payload, ts, int(existing["id"])),
                    )
                    self._adjust_table_stats(
                        conn,
                        app_id,
                        table_key,
                        row_delta=0,
                        bytes_delta=len(payload) - old_len,
                    )
                    updated = conn.execute(
                        "SELECT * FROM light_app_rows WHERE id = ?",
                        (int(existing["id"]),),
                    ).fetchone()
                    index = self._row_index(conn, app_id, table_key, int(existing["id"]))
                    return index, LightAppDataRow.from_row(updated)
            index = self._row_index(conn, app_id, table_key, None)
            conn.execute(
                "INSERT INTO light_app_rows("
                "row_id, app_id, table_key, values_json, version, source, source_id, "
                "source_key, client_request_id, created_by, created_at, updated_at"
                ") VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?)",
                (
                    rid,
                    app_id,
                    table_key,
                    payload,
                    source,
                    source_id,
                    source_key,
                    client_request_id,
                    created_by,
                    ts,
                    ts,
                ),
            )
            self._adjust_table_stats(
                conn,
                app_id,
                table_key,
                row_delta=1,
                bytes_delta=len(payload),
            )
        row = self.get_data_row(app_id, rid)
        if row is None:
            raise RuntimeError(f"light app row insert failed: {rid}")
        return index, row

    def append_data_rows(
        self,
        app_id: str,
        table_key: str,
        rows: list[dict[str, str]],
        *,
        source: str = "manual",
        created_by: int | None = None,
    ) -> int:
        """Bulk-insert rows and update the table stats once."""
        if not rows:
            return 0
        ts = now_ts()
        payloads = [json.dumps(values, ensure_ascii=False) for values in rows]
        total_bytes = sum(len(payload.encode("utf-8")) for payload in payloads)
        with self._db.transaction() as conn:
            for payload in payloads:
                conn.execute(
                    "INSERT INTO light_app_rows("
                    "row_id, app_id, table_key, values_json, version, source, "
                    "created_by, created_at, updated_at"
                    ") VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?)",
                    (
                        f"row_{new_short_id(12)}",
                        app_id,
                        table_key,
                        payload,
                        source,
                        created_by,
                        ts,
                        ts,
                    ),
                )
            self._adjust_table_stats(
                conn,
                app_id,
                table_key,
                row_delta=len(rows),
                bytes_delta=total_bytes,
            )
        return len(rows)

    def list_data_rows(
        self,
        app_id: str,
        table_key: str,
        *,
        limit: int,
        offset: int,
    ) -> tuple[list[LightAppDataRow], int]:
        with self._db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM light_app_rows WHERE app_id = ? AND table_key = ? "
                "ORDER BY id LIMIT ? OFFSET ?",
                (app_id, table_key, limit, offset),
            ).fetchall()
            total = conn.execute(
                "SELECT COUNT(*) AS c FROM light_app_rows WHERE app_id = ? AND table_key = ?",
                (app_id, table_key),
            ).fetchone()
        return map_rows(rows, LightAppDataRow), int(total["c"]) if total else 0

    def list_all_data_rows(self, app_id: str, table_key: str) -> list[LightAppDataRow]:
        with self._db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM light_app_rows WHERE app_id = ? AND table_key = ? ORDER BY id",
                (app_id, table_key),
            ).fetchall()
        return map_rows(rows, LightAppDataRow)

    def find_data_row_by_source(
        self,
        app_id: str,
        table_key: str,
        source_id: str,
        source_key: str,
    ) -> LightAppDataRow | None:
        with self._db.connect() as conn:
            r = conn.execute(
                "SELECT * FROM light_app_rows WHERE app_id = ? AND table_key = ? "
                "AND source_id = ? AND source_key = ?",
                (app_id, table_key, source_id, source_key),
            ).fetchone()
        return LightAppDataRow.from_row(r) if r else None

    def get_data_row(self, app_id: str, row_id: str) -> LightAppDataRow | None:
        with self._db.connect() as conn:
            r = conn.execute(
                "SELECT * FROM light_app_rows WHERE app_id = ? AND row_id = ?",
                (app_id, row_id),
            ).fetchone()
        return LightAppDataRow.from_row(r) if r else None

    def update_data_row(
        self,
        app_id: str,
        row_id: str,
        values: dict[str, str],
        *,
        expected_version: int | None = None,
    ) -> LightAppDataRow | None:
        """Update one row with an optional CAS check; None means stale/missing."""
        ts = now_ts()
        payload = json.dumps(values, ensure_ascii=False)
        with self._db.transaction() as conn:
            existing = conn.execute(
                "SELECT * FROM light_app_rows WHERE app_id = ? AND row_id = ?",
                (app_id, row_id),
            ).fetchone()
            if existing is None:
                return None
            if expected_version is not None and int(existing["version"]) != expected_version:
                return None
            old_len = len(str(existing["values_json"]))
            cursor = conn.execute(
                "UPDATE light_app_rows SET values_json = ?, version = version + 1, updated_at = ? "
                "WHERE app_id = ? AND row_id = ? AND version = ?",
                (payload, ts, app_id, row_id, int(existing["version"])),
            )
            if cursor.rowcount == 0:
                return None
            self._adjust_table_stats(
                conn,
                app_id,
                str(existing["table_key"]),
                row_delta=0,
                bytes_delta=len(payload) - old_len,
            )
            updated = conn.execute(
                "SELECT * FROM light_app_rows WHERE app_id = ? AND row_id = ?",
                (app_id, row_id),
            ).fetchone()
        return LightAppDataRow.from_row(updated)

    def delete_data_row(
        self,
        app_id: str,
        row_id: str,
        *,
        expected_version: int | None = None,
    ) -> bool:
        """Delete one row with an optional CAS check."""
        with self._db.transaction() as conn:
            existing = conn.execute(
                "SELECT * FROM light_app_rows WHERE app_id = ? AND row_id = ?",
                (app_id, row_id),
            ).fetchone()
            if existing is None:
                return False
            if expected_version is not None and int(existing["version"]) != expected_version:
                return False
            cursor = conn.execute(
                "DELETE FROM light_app_rows WHERE app_id = ? AND row_id = ? AND version = ?",
                (app_id, row_id, int(existing["version"])),
            )
            if cursor.rowcount == 0:
                return False
            self._adjust_table_stats(
                conn,
                app_id,
                str(existing["table_key"]),
                row_delta=-1,
                bytes_delta=-len(str(existing["values_json"])),
            )
        return True

    def replace_data_rows(
        self,
        app_id: str,
        table_key: str,
        rows: list[dict[str, str]],
        *,
        source: str = "manual",
        created_by: int | None = None,
    ) -> int:
        """Replace every row of one table in a single transaction."""
        ts = now_ts()
        payloads = [json.dumps(values, ensure_ascii=False) for values in rows]
        total_bytes = sum(len(payload.encode("utf-8")) for payload in payloads)
        with self._db.transaction() as conn:
            conn.execute(
                "DELETE FROM light_app_rows WHERE app_id = ? AND table_key = ?",
                (app_id, table_key),
            )
            for payload in payloads:
                conn.execute(
                    "INSERT INTO light_app_rows("
                    "row_id, app_id, table_key, values_json, version, source, "
                    "created_by, created_at, updated_at"
                    ") VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?)",
                    (
                        f"row_{new_short_id(12)}",
                        app_id,
                        table_key,
                        payload,
                        source,
                        created_by,
                        ts,
                        ts,
                    ),
                )
            conn.execute(
                "UPDATE light_app_tables SET row_count = ?, data_bytes = ?, "
                "data_version = data_version + 1, updated_at = ? "
                "WHERE app_id = ? AND table_key = ?",
                (len(rows), total_bytes, ts, app_id, table_key),
            )
            self._refresh_app_totals_conn(conn, app_id)
        return len(rows)

    def rewrite_data_rows(
        self,
        app_id: str,
        table_key: str,
        rows: list[tuple[str, dict[str, str]]],
    ) -> None:
        """Rewrite the values of existing rows, keeping ids and versions."""
        if not rows:
            return
        ts = now_ts()
        payloads = [(json.dumps(values, ensure_ascii=False), row_id) for row_id, values in rows]
        total_bytes = sum(len(payload.encode("utf-8")) for payload, _ in payloads)
        with self._db.transaction() as conn:
            for payload, row_id in payloads:
                conn.execute(
                    "UPDATE light_app_rows SET values_json = ?, version = version + 1, "
                    "updated_at = ? WHERE app_id = ? AND row_id = ?",
                    (payload, ts, app_id, row_id),
                )
            conn.execute(
                "UPDATE light_app_tables SET data_bytes = ?, data_version = data_version + 1, "
                "updated_at = ? WHERE app_id = ? AND table_key = ?",
                (total_bytes, ts, app_id, table_key),
            )
            self._refresh_app_totals_conn(conn, app_id)

    def count_data_rows(self, app_id: str, table_key: str) -> int:
        with self._db.connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM light_app_rows WHERE app_id = ? AND table_key = ?",
                (app_id, table_key),
            ).fetchone()
        return int(row["c"]) if row else 0

    def set_storage_version(self, app_id: str, version: int) -> None:
        with self._db.transaction() as conn:
            conn.execute(
                "UPDATE light_apps SET storage_version = ?, updated_at = ? WHERE app_id = ?",
                (version, now_ts(), app_id),
            )

    def set_table_schema(
        self,
        app_id: str,
        table_key: str,
        schema_json: str,
        schema_version: int,
    ) -> None:
        with self._db.transaction() as conn:
            conn.execute(
                "UPDATE light_app_tables SET schema_json = ?, schema_version = ?, updated_at = ? "
                "WHERE app_id = ? AND table_key = ?",
                (schema_json, schema_version, now_ts(), app_id, table_key),
            )

    def get_table_schema(self, app_id: str, table_key: str) -> tuple[str, int] | None:
        with self._db.connect() as conn:
            row = conn.execute(
                "SELECT schema_json, schema_version FROM light_app_tables "
                "WHERE app_id = ? AND table_key = ?",
                (app_id, table_key),
            ).fetchone()
        if row is None or not str(row["schema_json"] or "").strip():
            return None
        return str(row["schema_json"]), int(row["schema_version"] or 1)

    @staticmethod
    def _row_index(conn: Any, app_id: str, table_key: str, row_pk: int | None) -> int:
        if row_pk is None:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM light_app_rows WHERE app_id = ? AND table_key = ?",
                (app_id, table_key),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM light_app_rows WHERE app_id = ? AND table_key = ? "
                "AND id < ?",
                (app_id, table_key, row_pk),
            ).fetchone()
        return int(row["c"]) if row else 0

    @staticmethod
    def _adjust_table_stats(
        conn: Any,
        app_id: str,
        table_key: str,
        *,
        row_delta: int,
        bytes_delta: int,
    ) -> None:
        current = conn.execute(
            "SELECT row_count, data_bytes FROM light_app_tables WHERE app_id = ? AND table_key = ?",
            (app_id, table_key),
        ).fetchone()
        if current is None:
            return
        row_count = max(0, int(current["row_count"] or 0) + row_delta)
        data_bytes = max(0, int(current["data_bytes"] or 0) + bytes_delta)
        conn.execute(
            "UPDATE light_app_tables SET row_count = ?, data_bytes = ?, "
            "data_version = data_version + 1, updated_at = ? WHERE app_id = ? AND table_key = ?",
            (row_count, data_bytes, now_ts(), app_id, table_key),
        )
        LightAppRepo._refresh_app_totals_conn(conn, app_id)

    @staticmethod
    def _refresh_app_totals_conn(conn: Any, app_id: str) -> None:
        conn.execute(
            "UPDATE light_apps SET "
            "row_count = (SELECT COALESCE(SUM(row_count), 0) FROM light_app_tables "
            "WHERE app_id = ?), "
            "data_bytes = (SELECT COALESCE(SUM(data_bytes), 0) FROM light_app_tables "
            "WHERE app_id = ?), updated_at = ? WHERE app_id = ?",
            (app_id, app_id, now_ts(), app_id),
        )

    def count_for_owner(self, owner_user_id: int) -> int:
        with self._db.connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM light_apps WHERE owner_user_id = ?",
                (owner_user_id,),
            ).fetchone()
        return int(row["c"]) if row else 0

    def update_metadata(
        self,
        app_id: str,
        *,
        name: str | None = None,
        description: str | None = None,
    ) -> None:
        fields, params = partial_updates([("name", name), ("description", description)])
        if not fields:
            return
        fields.append("updated_at = ?")
        params.append(now_ts())
        params.append(app_id)
        with self._db.transaction() as conn:
            conn.execute(
                f"UPDATE light_apps SET {', '.join(fields)} WHERE app_id = ?",
                params,
            )

    def set_row_count(self, app_id: str, row_count: int, *, touch: bool = True) -> None:
        with self._db.transaction() as conn:
            if touch:
                conn.execute(
                    "UPDATE light_apps SET row_count = ?, updated_at = ? WHERE app_id = ?",
                    (row_count, now_ts(), app_id),
                )
            else:
                conn.execute(
                    "UPDATE light_apps SET row_count = ? WHERE app_id = ?",
                    (row_count, app_id),
                )

    def set_capabilities(self, app_id: str, capabilities_json: str) -> None:
        with self._db.transaction() as conn:
            conn.execute(
                "UPDATE light_apps SET capabilities_json = ?, updated_at = ? WHERE app_id = ?",
                (capabilities_json, now_ts(), app_id),
            )

    def set_previous_html(self, app_id: str, html: str, saved_at: int) -> None:
        with self._db.transaction() as conn:
            conn.execute(
                "UPDATE light_apps SET previous_html = ?, previous_html_at = ? WHERE app_id = ?",
                (html, saved_at, app_id),
            )

    def set_file_stats(
        self,
        app_id: str,
        *,
        html_bytes: int | None = None,
        data_bytes: int | None = None,
    ) -> None:
        """Cache the on-disk size of the app files the dashboard shows."""
        fields, params = partial_updates([("html_bytes", html_bytes), ("data_bytes", data_bytes)])
        if not fields:
            return
        params.append(app_id)
        with self._db.transaction() as conn:
            conn.execute(
                f"UPDATE light_apps SET {', '.join(fields)} WHERE app_id = ?",
                params,
            )

    def touch(self, app_id: str) -> None:
        with self._db.transaction() as conn:
            conn.execute(
                "UPDATE light_apps SET updated_at = ? WHERE app_id = ?",
                (now_ts(), app_id),
            )

    def delete(self, app_id: str) -> None:
        with self._db.transaction() as conn:
            conn.execute("DELETE FROM light_apps WHERE app_id = ?", (app_id,))

    def list_collaborators(self, app_id: str) -> list[LightAppCollaboratorRow]:
        with self._db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM light_app_collaborators WHERE app_id = ? ORDER BY created_at",
                (app_id,),
            ).fetchall()
        return map_rows(rows, LightAppCollaboratorRow)

    def permission_for(self, app_id: str, user_id: int) -> str | None:
        with self._db.connect() as conn:
            r = conn.execute(
                "SELECT permission FROM light_app_collaborators WHERE app_id = ? AND user_id = ?",
                (app_id, user_id),
            ).fetchone()
        return str(r["permission"]) if r else None

    def set_collaborators(
        self,
        app_id: str,
        entries: list[tuple[int, str]],
    ) -> None:
        """Replace the collaborator list for one app."""
        ts = now_ts()
        with self._db.transaction() as conn:
            conn.execute(
                "DELETE FROM light_app_collaborators WHERE app_id = ?",
                (app_id,),
            )
            for user_id, permission in entries:
                conn.execute(
                    "INSERT INTO light_app_collaborators("
                    "app_id, user_id, permission, created_at, updated_at"
                    ") VALUES (?, ?, ?, ?, ?)",
                    (app_id, user_id, permission, ts, ts),
                )

    # ----- tables -----

    def list_tables(self, app_id: str) -> list[LightAppTableRow]:
        with self._db.connect() as conn:
            rows = conn.execute(
                # Registration order; ``created_at`` has second resolution and
                # can tie for tables created in the same request.
                "SELECT * FROM light_app_tables WHERE app_id = ? ORDER BY id",
                (app_id,),
            ).fetchall()
        return map_rows(rows, LightAppTableRow)

    def get_table(self, app_id: str, table_key: str) -> LightAppTableRow | None:
        with self._db.connect() as conn:
            r = conn.execute(
                "SELECT * FROM light_app_tables WHERE app_id = ? AND table_key = ?",
                (app_id, table_key),
            ).fetchone()
        return LightAppTableRow.from_row(r) if r else None

    def count_tables(self, app_id: str) -> int:
        with self._db.connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM light_app_tables WHERE app_id = ?",
                (app_id,),
            ).fetchone()
        return int(row["c"]) if row else 0

    def create_table(
        self,
        app_id: str,
        table_key: str,
        label: str = "",
        *,
        public_visible: bool = False,
    ) -> LightAppTableRow:
        ts = now_ts()
        with self._db.transaction() as conn:
            conn.execute(
                "INSERT INTO light_app_tables("
                "app_id, table_key, label, row_count, public_visible, created_at, updated_at"
                ") VALUES (?, ?, ?, 0, ?, ?, ?)",
                (app_id, table_key, label, int(public_visible), ts, ts),
            )
        row = self.get_table(app_id, table_key)
        if row is None:
            raise RuntimeError(f"light app table insert failed: {app_id}/{table_key}")
        return row

    def set_table_visibility(
        self,
        app_id: str,
        table_key: str,
        *,
        public_visible: bool,
    ) -> None:
        with self._db.transaction() as conn:
            conn.execute(
                "UPDATE light_app_tables SET public_visible = ?, updated_at = ? "
                "WHERE app_id = ? AND table_key = ?",
                (int(public_visible), now_ts(), app_id, table_key),
            )

    def set_table_row_count(self, app_id: str, table_key: str, row_count: int) -> None:
        with self._db.transaction() as conn:
            conn.execute(
                "UPDATE light_app_tables SET row_count = ?, updated_at = ? "
                "WHERE app_id = ? AND table_key = ?",
                (row_count, now_ts(), app_id, table_key),
            )

    def set_table_stats(
        self,
        app_id: str,
        table_key: str,
        *,
        row_count: int,
        data_bytes: int,
    ) -> None:
        with self._db.transaction() as conn:
            conn.execute(
                "UPDATE light_app_tables SET row_count = ?, data_bytes = ?, updated_at = ? "
                "WHERE app_id = ? AND table_key = ?",
                (row_count, data_bytes, now_ts(), app_id, table_key),
            )

    def refresh_app_totals(self, app_id: str) -> None:
        """Recompute the denormalized ``light_apps`` totals from the table registry."""
        with self._db.transaction() as conn:
            conn.execute(
                "UPDATE light_apps SET "
                "row_count = (SELECT COALESCE(SUM(row_count), 0) "
                "FROM light_app_tables WHERE app_id = ?), "
                "data_bytes = (SELECT COALESCE(SUM(data_bytes), 0) "
                "FROM light_app_tables WHERE app_id = ?) "
                "WHERE app_id = ?",
                (app_id, app_id, app_id),
            )

    # ----- public links -----

    def get_publication(self, app_id: str) -> LightAppPublicationRow | None:
        with self._db.connect() as conn:
            r = conn.execute(
                "SELECT * FROM light_app_publications WHERE app_id = ?",
                (app_id,),
            ).fetchone()
        return LightAppPublicationRow.from_row(r) if r else None

    def get_publication_by_hash(self, token_hash: str) -> LightAppPublicationRow | None:
        with self._db.connect() as conn:
            r = conn.execute(
                "SELECT * FROM light_app_publications WHERE token_hash = ?",
                (token_hash,),
            ).fetchone()
        return LightAppPublicationRow.from_row(r) if r else None

    def upsert_publication(
        self,
        app_id: str,
        *,
        token_hash: str,
        created_by: int,
        expires_at: int | None = None,
    ) -> LightAppPublicationRow:
        ts = now_ts()
        with self._db.transaction() as conn:
            existing = conn.execute(
                "SELECT id FROM light_app_publications WHERE app_id = ?",
                (app_id,),
            ).fetchone()
            if existing is None:
                conn.execute(
                    "INSERT INTO light_app_publications("
                    "app_id, token_hash, created_by, created_at, revoked_at, expires_at"
                    ") VALUES (?, ?, ?, ?, NULL, ?)",
                    (app_id, token_hash, created_by, ts, expires_at),
                )
            else:
                conn.execute(
                    "UPDATE light_app_publications SET token_hash = ?, created_by = ?, "
                    "created_at = ?, revoked_at = NULL, expires_at = ? WHERE app_id = ?",
                    (token_hash, created_by, ts, expires_at, app_id),
                )
        row = self.get_publication(app_id)
        if row is None:
            raise RuntimeError(f"light app publication upsert failed: {app_id}")
        return row

    def set_publication_snapshot(
        self,
        app_id: str,
        *,
        html: str,
        tables_json: str,
        digest: str,
        snapshot_at: int,
    ) -> None:
        """Freeze the published HTML and public table structure of one app."""
        with self._db.transaction() as conn:
            conn.execute(
                "UPDATE light_app_publications SET html_snapshot = ?, "
                "tables_snapshot_json = ?, snapshot_digest = ?, snapshot_at = ? "
                "WHERE app_id = ?",
                (html, tables_json, digest, snapshot_at, app_id),
            )

    def revoke_publication(self, app_id: str) -> bool:
        with self._db.transaction() as conn:
            cursor = conn.execute(
                "UPDATE light_app_publications SET revoked_at = ? "
                "WHERE app_id = ? AND revoked_at IS NULL",
                (now_ts(), app_id),
            )
        return int(cursor.rowcount or 0) > 0

    # ----- assistant history -----

    def list_assistant_messages(
        self,
        app_id: str,
        user_id: int,
        *,
        limit: int = ASSISTANT_HISTORY_KEEP,
    ) -> list[LightAppAssistantMessageRow]:
        """The user's latest assistant messages for one app, oldest first."""
        with self._db.connect() as conn:
            rows = conn.execute(
                "SELECT role, content, created_at FROM ("
                "SELECT id, role, content, created_at "
                "FROM light_app_assistant_messages "
                "WHERE app_id = ? AND user_id = ? ORDER BY id DESC LIMIT ?"
                ") ORDER BY id",
                (app_id, user_id, max(1, int(limit))),
            ).fetchall()
        return map_rows(rows, LightAppAssistantMessageRow)

    def add_assistant_message(
        self,
        app_id: str,
        user_id: int,
        *,
        role: str,
        content: str,
    ) -> None:
        ts = now_ts()
        with self._db.transaction() as conn:
            conn.execute(
                "INSERT INTO light_app_assistant_messages("
                "app_id, user_id, role, content, created_at"
                ") VALUES (?, ?, ?, ?, ?)",
                (app_id, user_id, role, content, ts),
            )
            # Keep only the most recent window per conversation.
            conn.execute(
                "DELETE FROM light_app_assistant_messages "
                "WHERE app_id = ? AND user_id = ? AND id NOT IN ("
                "SELECT id FROM light_app_assistant_messages "
                "WHERE app_id = ? AND user_id = ? ORDER BY id DESC LIMIT ?"
                ")",
                (app_id, user_id, app_id, user_id, ASSISTANT_HISTORY_KEEP),
            )

    def delete_assistant_messages(self, app_id: str) -> None:
        with self._db.transaction() as conn:
            conn.execute(
                "DELETE FROM light_app_assistant_messages WHERE app_id = ?",
                (app_id,),
            )

    # ----- data sources -----

    def list_sources(self, app_id: str) -> list[LightAppSourceRow]:
        with self._db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM light_app_sources WHERE app_id = ? ORDER BY id",
                (app_id,),
            ).fetchall()
        return map_rows(rows, LightAppSourceRow)

    def list_due_sources(self, now: int) -> list[LightAppSourceRow]:
        """Scheduled sources whose next run is due (NULL next_run_at = due now)."""
        with self._db.connect() as conn:
            rows = conn.execute(
                "SELECT s.* FROM light_app_sources s "
                "JOIN light_apps a ON a.app_id = s.app_id "
                "WHERE s.schedule_seconds > 0 AND COALESCE(s.next_run_at, 0) <= ?",
                (now,),
            ).fetchall()
        return map_rows(rows, LightAppSourceRow)

    def claim_source(self, source_id: str, *, now: int, schedule_seconds: int) -> bool:
        """Atomically claim a due source for one run.

        Pushes ``next_run_at`` forward one interval; the conditional UPDATE
        doubles as the lease — only one caller can claim a due source, and a
        crash mid-run just delays the next attempt to the next interval.
        """
        with self._db.transaction() as conn:
            cur = conn.execute(
                "UPDATE light_app_sources SET next_run_at = ?, updated_at = ? "
                "WHERE source_id = ? AND schedule_seconds > 0 "
                "AND COALESCE(next_run_at, 0) <= ?",
                (now + schedule_seconds, now, source_id, now),
            )
        return int(cur.rowcount) > 0

    def get_source(self, app_id: str, source_id: str) -> LightAppSourceRow | None:
        with self._db.connect() as conn:
            r = conn.execute(
                "SELECT * FROM light_app_sources WHERE app_id = ? AND source_id = ?",
                (app_id, source_id),
            ).fetchone()
        return LightAppSourceRow.from_row(r) if r else None

    def insert_source(
        self,
        *,
        source_id: str,
        app_id: str,
        table_key: str,
        kind: str,
        mode: str,
        schedule_seconds: int,
        params_json: str,
        mapping_json: str,
        created_by: int,
    ) -> LightAppSourceRow:
        ts = now_ts()
        with self._db.transaction() as conn:
            conn.execute(
                "INSERT INTO light_app_sources("
                "source_id, app_id, table_key, kind, mode, schedule_seconds, "
                "params_json, mapping_json, created_by, created_at, updated_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    source_id,
                    app_id,
                    table_key,
                    kind,
                    mode,
                    schedule_seconds,
                    params_json,
                    mapping_json,
                    created_by,
                    ts,
                    ts,
                ),
            )
        row = self.get_source(app_id, source_id)
        if row is None:
            raise RuntimeError(f"light app source insert failed: {app_id}/{source_id}")
        return row

    def update_source(
        self,
        app_id: str,
        source_id: str,
        *,
        table_key: str,
        kind: str,
        mode: str,
        schedule_seconds: int,
        params_json: str,
        mapping_json: str,
    ) -> LightAppSourceRow:
        with self._db.transaction() as conn:
            conn.execute(
                "UPDATE light_app_sources SET table_key = ?, kind = ?, mode = ?, "
                "schedule_seconds = ?, params_json = ?, mapping_json = ?, "
                "next_run_at = NULL, fail_count = 0, "
                "last_run_at = NULL, last_status = NULL, last_error = NULL, "
                "last_row_count = NULL, updated_at = ? "
                "WHERE app_id = ? AND source_id = ?",
                (
                    table_key,
                    kind,
                    mode,
                    schedule_seconds,
                    params_json,
                    mapping_json,
                    now_ts(),
                    app_id,
                    source_id,
                ),
            )
        row = self.get_source(app_id, source_id)
        if row is None:
            raise RuntimeError(f"light app source update failed: {app_id}/{source_id}")
        return row

    def set_source_params(self, source_id: str, params_json: str) -> None:
        """Replace a binding's config (used for incremental sync cursors)."""
        with self._db.transaction() as conn:
            conn.execute(
                "UPDATE light_app_sources SET params_json = ?, updated_at = ? WHERE source_id = ?",
                (params_json, now_ts(), source_id),
            )

    def delete_source(self, app_id: str, source_id: str) -> bool:
        with self._db.transaction() as conn:
            cur = conn.execute(
                "DELETE FROM light_app_sources WHERE app_id = ? AND source_id = ?",
                (app_id, source_id),
            )
        return int(cur.rowcount) > 0

    def set_source_run_state(
        self,
        source_id: str,
        *,
        status: str,
        run_at: int,
        error: str = "",
        row_count: int | None = None,
        next_run_at: int,
        fail_count: int,
    ) -> None:
        with self._db.transaction() as conn:
            conn.execute(
                "UPDATE light_app_sources SET last_run_at = ?, last_status = ?, "
                "last_error = ?, last_row_count = ?, next_run_at = ?, fail_count = ?, "
                "updated_at = ? WHERE source_id = ?",
                (run_at, status, error, row_count, next_run_at, fail_count, now_ts(), source_id),
            )
