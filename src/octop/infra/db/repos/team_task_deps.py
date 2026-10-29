"""team_task_deps — task dependency edges (DAG). SQL only.

Edges only; cycle and cross-team validation belong to the caller that writes
plans (docs/team-drive-v2.md §4.5). Team-scoped reads join ``team_tasks``.
"""

from __future__ import annotations

from dataclasses import dataclass

from octop.infra.db.pool import DatabasePool
from octop.infra.db.repos._base import DbRow, map_rows, now_ts


@dataclass(frozen=True)
class TeamTaskDepRow:
    task_id: str
    depends_on_task_id: str
    created_at: int

    @classmethod
    def from_row(cls, r: DbRow) -> TeamTaskDepRow:
        return cls(
            task_id=str(r["task_id"]),
            depends_on_task_id=str(r["depends_on_task_id"]),
            created_at=int(r["created_at"]),
        )


class TeamTaskDepsRepo:
    def __init__(self, db: DatabasePool) -> None:
        self._db = db

    def add(self, task_id: str, depends_on_task_id: str) -> bool:
        """Add one edge; ``False`` on self-dependency or duplicate edge."""
        if task_id == depends_on_task_id:
            return False
        with self._db.transaction() as conn:
            changed = conn.execute(
                "INSERT INTO team_task_deps(task_id, depends_on_task_id, created_at) "
                "VALUES (?, ?, ?) ON CONFLICT DO NOTHING",
                (task_id, depends_on_task_id, now_ts()),
            ).rowcount
        return bool(changed)

    def remove(self, task_id: str, depends_on_task_id: str) -> None:
        with self._db.transaction() as conn:
            conn.execute(
                "DELETE FROM team_task_deps WHERE task_id = ? AND depends_on_task_id = ?",
                (task_id, depends_on_task_id),
            )

    def clear_for_task(self, task_id: str) -> None:
        """Drop edges in both directions (task being cancelled/replanned)."""
        with self._db.transaction() as conn:
            conn.execute(
                "DELETE FROM team_task_deps WHERE task_id = ? OR depends_on_task_id = ?",
                (task_id, task_id),
            )

    def list_for_task(self, task_id: str) -> list[str]:
        """Ids this task depends on."""
        with self._db.connect() as conn:
            rows = conn.execute(
                "SELECT depends_on_task_id FROM team_task_deps WHERE task_id = ? "
                "ORDER BY depends_on_task_id",
                (task_id,),
            ).fetchall()
        return [str(r["depends_on_task_id"]) for r in rows]

    def list_for_team(self, team_id: str) -> list[TeamTaskDepRow]:
        """All edges of a team's tasks (through the owning task's team_id)."""
        with self._db.connect() as conn:
            rows = conn.execute(
                "SELECT d.* FROM team_task_deps d "
                "JOIN team_tasks t ON t.task_id = d.task_id "
                "WHERE t.team_id = ? ORDER BY d.task_id, d.depends_on_task_id",
                (team_id,),
            ).fetchall()
        return map_rows(rows, TeamTaskDepRow)
