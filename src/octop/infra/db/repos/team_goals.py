"""Team goals — SQL only. One team keeps a single active goal; setting a new
one archives the previous active row (archive step lives in the callers)."""

from __future__ import annotations

from dataclasses import dataclass

from octop.infra.db.pool import DatabasePool
from octop.infra.db.repos._base import DbRow, map_rows, now_ts
from octop.infra.utils.ulid import new_short_id

GOAL_STATUS_ACTIVE = "active"
GOAL_STATUS_ARCHIVED = "archived"


@dataclass(frozen=True)
class TeamGoalRow:
    goal_id: str
    pk: int
    team_id: str
    title: str
    description: str
    status: str
    success_criteria: str
    created_by: int
    created_at: int
    updated_at: int

    @classmethod
    def from_row(cls, r: DbRow) -> TeamGoalRow:
        return cls(
            goal_id=str(r["goal_id"]),
            pk=int(r["id"]),
            team_id=str(r["team_id"]),
            title=str(r["title"]),
            description=str(r["description"] or ""),
            status=str(r["status"] or GOAL_STATUS_ACTIVE),
            success_criteria=str(r["success_criteria"] or ""),
            created_by=int(r["created_by"]),
            created_at=int(r["created_at"]),
            updated_at=int(r["updated_at"]),
        )


class TeamGoalRepo:
    def __init__(self, db: DatabasePool) -> None:
        self._db = db

    def create(
        self,
        *,
        team_id: str,
        title: str,
        created_by: int,
        description: str = "",
        success_criteria: str = "",
    ) -> TeamGoalRow:
        goal_id = new_short_id()
        ts = now_ts()
        with self._db.transaction() as conn:
            conn.execute(
                "INSERT INTO team_goals("
                "goal_id, team_id, title, description, status, success_criteria, "
                "created_by, created_at, updated_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    goal_id,
                    team_id,
                    title,
                    description,
                    GOAL_STATUS_ACTIVE,
                    success_criteria,
                    created_by,
                    ts,
                    ts,
                ),
            )
        row = self.get(goal_id)
        if row is None:
            raise RuntimeError(f"team goal insert failed: {goal_id}")
        return row

    def get(self, goal_id: str) -> TeamGoalRow | None:
        with self._db.connect() as conn:
            r = conn.execute(
                "SELECT * FROM team_goals WHERE goal_id = ?",
                (goal_id,),
            ).fetchone()
        return TeamGoalRow.from_row(r) if r else None

    def active(self, team_id: str) -> TeamGoalRow | None:
        with self._db.connect() as conn:
            r = conn.execute(
                "SELECT * FROM team_goals WHERE team_id = ? AND status = ? "
                "ORDER BY created_at DESC LIMIT 1",
                (team_id, GOAL_STATUS_ACTIVE),
            ).fetchone()
        return TeamGoalRow.from_row(r) if r else None

    def update(self, goal_id: str, *, fields: dict[str, object]) -> TeamGoalRow | None:
        """Patch goal fields; unknown columns are rejected by SQLite/PG."""
        if not fields:
            return self.get(goal_id)
        cols = list(fields)
        sets = ", ".join(f"{col} = ?" for col in cols)
        params: list[object] = [fields[col] for col in cols]
        with self._db.transaction() as conn:
            changed = conn.execute(
                f"UPDATE team_goals SET {sets}, updated_at = ? WHERE goal_id = ?",
                (*params, now_ts(), goal_id),
            ).rowcount
            if not changed:
                return None
        return self.get(goal_id)

    def archive_active(self, team_id: str) -> None:
        with self._db.transaction() as conn:
            conn.execute(
                "UPDATE team_goals SET status = ?, updated_at = ? WHERE team_id = ? AND status = ?",
                (GOAL_STATUS_ARCHIVED, now_ts(), team_id, GOAL_STATUS_ACTIVE),
            )

    def list_by_team(self, team_id: str, *, status: str | None = None) -> list[TeamGoalRow]:
        sql = "SELECT * FROM team_goals WHERE team_id = ?"
        params: list[object] = [team_id]
        if status is not None:
            sql += " AND status = ?"
            params.append(status)
        sql += " ORDER BY created_at DESC"
        with self._db.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return map_rows(rows, TeamGoalRow)
