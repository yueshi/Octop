"""Acceptance records for board tasks — SQL only.

``review`` used to be a bare task status: no reviewer, no opinion, no evidence
of what was checked. One row per decision, so "approved" has a who/why/when
behind it (team-l3-roadmap A2).
"""

from __future__ import annotations

from dataclasses import dataclass

from octop.infra.db.pool import DatabasePool
from octop.infra.db.repos._base import DbRow, map_rows, now_ts
from octop.infra.utils.ulid import new_short_id

REVIEW_APPROVED = "approved"
REVIEW_REJECTED = "rejected"
REVIEW_DECISIONS = frozenset({REVIEW_APPROVED, REVIEW_REJECTED})


@dataclass(frozen=True)
class TaskReviewRow:
    review_id: str
    pk: int
    task_id: str
    decision: str
    comment: str
    reviewer: str
    created_at: int

    @classmethod
    def from_row(cls, r: DbRow) -> TaskReviewRow:
        return cls(
            review_id=str(r["review_id"]),
            pk=int(r["id"]),
            task_id=str(r["task_id"]),
            decision=str(r["decision"]),
            comment=str(r["comment"] or ""),
            reviewer=str(r["reviewer"] or ""),
            created_at=int(r["created_at"]),
        )


class TeamTaskReviewRepo:
    def __init__(self, db: DatabasePool) -> None:
        self._db = db

    def create(
        self,
        *,
        task_id: str,
        decision: str,
        comment: str = "",
        reviewer: str = "",
    ) -> TaskReviewRow:
        if decision not in REVIEW_DECISIONS:
            raise ValueError(f"invalid review decision: {decision!r}")
        review_id = new_short_id()
        with self._db.transaction() as conn:
            conn.execute(
                "INSERT INTO team_task_reviews("
                "review_id, task_id, decision, comment, reviewer, created_at"
                ") VALUES (?, ?, ?, ?, ?, ?)",
                (review_id, task_id, decision, comment, reviewer, now_ts()),
            )
        row = self.get(review_id)
        if row is None:
            raise RuntimeError(f"task review insert failed: {review_id}")
        return row

    def get(self, review_id: str) -> TaskReviewRow | None:
        with self._db.connect() as conn:
            r = conn.execute(
                "SELECT * FROM team_task_reviews WHERE review_id = ?", (review_id,)
            ).fetchone()
        return TaskReviewRow.from_row(r) if r else None

    def list_for_task(self, task_id: str) -> list[TaskReviewRow]:
        with self._db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM team_task_reviews WHERE task_id = ? "
                "ORDER BY created_at DESC, id DESC",
                (task_id,),
            ).fetchall()
        return map_rows(rows, TaskReviewRow)


__all__ = [
    "REVIEW_APPROVED",
    "REVIEW_DECISIONS",
    "REVIEW_REJECTED",
    "TaskReviewRow",
    "TeamTaskReviewRepo",
]
