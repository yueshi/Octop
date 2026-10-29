"""Goal deliverables — expected outputs plus attached artifacts. SQL only."""

from __future__ import annotations

from dataclasses import dataclass

from octop.infra.db.pool import DatabasePool
from octop.infra.db.repos._base import DbRow, map_rows, now_ts
from octop.infra.utils.ulid import new_short_id

DELIVERABLE_STATUSES = frozenset({"pending", "produced"})
ARTIFACT_TYPES = frozenset({"none", "file", "link", "text"})
_UPDATABLE_FIELDS = frozenset(
    {
        "title",
        "description",
        "format",
        "required",
        "status",
        "task_id",
        "artifact_type",
        "artifact_ref",
        "artifact_name",
        "artifact_agent_id",
    }
)


@dataclass(frozen=True)
class GoalDeliverableRow:
    deliverable_id: str
    pk: int
    goal_id: str
    title: str
    description: str
    format: str
    required: bool
    status: str
    task_id: str | None
    artifact_type: str
    artifact_ref: str
    artifact_name: str
    artifact_agent_id: str | None
    created_by: int
    created_at: int
    updated_at: int

    @classmethod
    def from_row(cls, r: DbRow) -> GoalDeliverableRow:
        return cls(
            deliverable_id=str(r["deliverable_id"]),
            pk=int(r["id"]),
            goal_id=str(r["goal_id"]),
            title=str(r["title"]),
            description=str(r["description"] or ""),
            format=str(r["format"] or ""),
            required=bool(r["required"]),
            status=str(r["status"] or "pending"),
            task_id=str(r["task_id"]) if r["task_id"] is not None else None,
            artifact_type=str(r["artifact_type"] or "none"),
            artifact_ref=str(r["artifact_ref"] or ""),
            artifact_name=str(r["artifact_name"] or ""),
            artifact_agent_id=(
                str(r["artifact_agent_id"]) if r["artifact_agent_id"] is not None else None
            ),
            created_by=int(r["created_by"]),
            created_at=int(r["created_at"]),
            updated_at=int(r["updated_at"]),
        )


class GoalDeliverableRepo:
    def __init__(self, db: DatabasePool) -> None:
        self._db = db

    def create(
        self,
        *,
        goal_id: str,
        title: str,
        created_by: int,
        description: str = "",
        format: str = "",
        required: bool = True,
        task_id: str | None = None,
    ) -> GoalDeliverableRow:
        deliverable_id = new_short_id()
        ts = now_ts()
        with self._db.transaction() as conn:
            conn.execute(
                "INSERT INTO goal_deliverables("
                "deliverable_id, goal_id, title, description, format, required, "
                "status, task_id, artifact_type, artifact_ref, artifact_name, "
                "artifact_agent_id, created_by, created_at, updated_at"
                ") VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, 'none', '', '', NULL, ?, ?, ?)",
                (
                    deliverable_id,
                    goal_id,
                    title,
                    description,
                    format,
                    1 if required else 0,
                    task_id,
                    created_by,
                    ts,
                    ts,
                ),
            )
        row = self.get(deliverable_id)
        if row is None:
            raise RuntimeError(f"goal deliverable insert failed: {deliverable_id}")
        return row

    def get(self, deliverable_id: str) -> GoalDeliverableRow | None:
        with self._db.connect() as conn:
            r = conn.execute(
                "SELECT * FROM goal_deliverables WHERE deliverable_id = ?",
                (deliverable_id,),
            ).fetchone()
        return GoalDeliverableRow.from_row(r) if r else None

    def list_for_goal(self, goal_id: str) -> list[GoalDeliverableRow]:
        with self._db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM goal_deliverables WHERE goal_id = ? ORDER BY id",
                (goal_id,),
            ).fetchall()
        return map_rows(rows, GoalDeliverableRow)

    def update(
        self,
        deliverable_id: str,
        *,
        fields: dict[str, object],
    ) -> GoalDeliverableRow | None:
        clean = {key: value for key, value in fields.items() if key in _UPDATABLE_FIELDS}
        if not clean:
            return self.get(deliverable_id)
        cols = list(clean)
        sets = ", ".join(f"{col} = ?" for col in cols)
        params: list[object] = [clean[col] for col in cols]
        with self._db.transaction() as conn:
            changed = conn.execute(
                f"UPDATE goal_deliverables SET {sets}, updated_at = ? WHERE deliverable_id = ?",
                (*params, now_ts(), deliverable_id),
            ).rowcount
            if not changed:
                return None
        return self.get(deliverable_id)

    def delete(self, deliverable_id: str) -> bool:
        with self._db.transaction() as conn:
            changed = conn.execute(
                "DELETE FROM goal_deliverables WHERE deliverable_id = ?",
                (deliverable_id,),
            ).rowcount
        return bool(changed)
