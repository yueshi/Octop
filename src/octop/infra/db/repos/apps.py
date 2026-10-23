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
