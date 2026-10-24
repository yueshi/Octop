"""Light app metadata and sharing ACL — SQL only.

App content files live in the owning agent's workspace and are handled by
``octop.infra.apps``; this repo owns the control-plane index so lists, sharing
and permission checks do not need to touch every agent workspace.
"""

from __future__ import annotations

from dataclasses import dataclass

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
    ) -> LightAppRow:
        app_id = app_id or self.allocate_id()
        ts = now_ts()
        with self._db.transaction() as conn:
            conn.execute(
                "INSERT INTO light_apps("
                "app_id, agent_id, owner_user_id, name, description, row_count, "
                "created_at, updated_at"
                ") VALUES (?, ?, ?, ?, ?, 0, ?, ?)",
                (app_id, agent_id, owner_user_id, name, description, ts, ts),
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
    ) -> LightAppTableRow:
        ts = now_ts()
        with self._db.transaction() as conn:
            conn.execute(
                "INSERT INTO light_app_tables("
                "app_id, table_key, label, row_count, created_at, updated_at"
                ") VALUES (?, ?, ?, 0, ?, ?)",
                (app_id, table_key, label, ts, ts),
            )
        row = self.get_table(app_id, table_key)
        if row is None:
            raise RuntimeError(f"light app table insert failed: {app_id}/{table_key}")
        return row

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
