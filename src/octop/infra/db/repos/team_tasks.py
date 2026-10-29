"""Team tasks — SQL only. Status transitions are validated by callers
(``octop.infra.agents.teams.task_state``); updates use a version CAS so host
tools and dashboard PATCHes cannot silently overwrite each other."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from octop.infra.db.pool import DatabasePool
from octop.infra.db.repos._base import DbRow, map_rows, now_ts
from octop.infra.utils.ulid import new_short_id

TASK_STATUS_PROPOSED = "proposed"


@dataclass(frozen=True)
class TeamTaskRow:
    task_id: str
    pk: int
    team_id: str
    goal_id: str | None
    title: str
    spec: str
    assignee: str | None
    status: str
    priority: int
    result_summary: str
    blocked_reason: str
    version: int
    created_at: int
    updated_at: int
    due_at: int | None
    # Coordination columns (docs/team-drive-v2.md §4.6).
    window_start: int | None = None
    window_end: int | None = None
    budget_tokens: int | None = None
    concurrency_key: str | None = None
    active_wait_id: str | None = None

    @classmethod
    def from_row(cls, r: DbRow) -> TeamTaskRow:
        return cls(
            task_id=str(r["task_id"]),
            pk=int(r["id"]),
            team_id=str(r["team_id"]),
            goal_id=str(r["goal_id"]) if r["goal_id"] is not None else None,
            title=str(r["title"]),
            spec=str(r["spec"] or ""),
            assignee=str(r["assignee"]) if r["assignee"] is not None else None,
            status=str(r["status"] or TASK_STATUS_PROPOSED),
            priority=int(r["priority"] or 0),
            result_summary=str(r["result_summary"] or ""),
            blocked_reason=str(r["blocked_reason"] or ""),
            version=int(r["version"] or 1),
            created_at=int(r["created_at"]),
            updated_at=int(r["updated_at"]),
            due_at=int(r["due_at"]) if r["due_at"] is not None else None,
            window_start=int(r["window_start"]) if r["window_start"] is not None else None,
            window_end=int(r["window_end"]) if r["window_end"] is not None else None,
            budget_tokens=int(r["budget_tokens"]) if r["budget_tokens"] is not None else None,
            concurrency_key=(
                str(r["concurrency_key"]) if r["concurrency_key"] is not None else None
            ),
            active_wait_id=(str(r["active_wait_id"]) if r["active_wait_id"] is not None else None),
        )


class TeamTaskRepo:
    def __init__(self, db: DatabasePool) -> None:
        self._db = db

    def create(
        self,
        *,
        team_id: str,
        title: str,
        goal_id: str | None = None,
        spec: str = "",
        assignee: str | None = None,
        status: str = TASK_STATUS_PROPOSED,
        priority: int = 0,
        due_at: int | None = None,
        window_start: int | None = None,
        window_end: int | None = None,
        budget_tokens: int | None = None,
        concurrency_key: str | None = None,
        idempotency_key: str | None = None,
    ) -> TeamTaskRow:
        task_id = new_short_id()
        ts = now_ts()
        with self._db.transaction() as conn:
            conn.execute(
                "INSERT INTO team_tasks("
                "task_id, team_id, goal_id, title, spec, assignee, status, "
                "priority, created_at, updated_at, due_at, window_start, "
                "window_end, budget_tokens, concurrency_key, idempotency_key"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    task_id,
                    team_id,
                    goal_id,
                    title,
                    spec,
                    assignee,
                    status,
                    priority,
                    ts,
                    ts,
                    due_at,
                    window_start,
                    window_end,
                    budget_tokens,
                    concurrency_key,
                    idempotency_key,
                ),
            )
        row = self.get(task_id)
        if row is None:
            raise RuntimeError(f"team task insert failed: {task_id}")
        return row

    def get(self, task_id: str) -> TeamTaskRow | None:
        with self._db.connect() as conn:
            r = conn.execute(
                "SELECT * FROM team_tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
        return TeamTaskRow.from_row(r) if r else None

    def get_by_idempotency_key(self, key: str) -> TeamTaskRow | None:
        with self._db.connect() as conn:
            r = conn.execute(
                "SELECT * FROM team_tasks WHERE idempotency_key = ?",
                (key,),
            ).fetchone()
        return TeamTaskRow.from_row(r) if r else None

    def list_by_team(
        self,
        team_id: str,
        *,
        status: str | None = None,
        goal_id: str | None = None,
        limit: int = 200,
    ) -> list[TeamTaskRow]:
        sql = "SELECT * FROM team_tasks WHERE team_id = ?"
        params: list[Any] = [team_id]
        if status is not None:
            sql += " AND status = ?"
            params.append(status)
        if goal_id is not None:
            sql += " AND goal_id = ?"
            params.append(goal_id)
        sql += " ORDER BY priority DESC, created_at ASC LIMIT ?"
        params.append(limit)
        with self._db.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return map_rows(rows, TeamTaskRow)

    def count_open(self, team_id: str) -> int:
        with self._db.connect() as conn:
            r = conn.execute(
                "SELECT COUNT(*) AS c FROM team_tasks WHERE team_id = ? "
                "AND status NOT IN ('done', 'failed', 'cancelled')",
                (team_id,),
            ).fetchone()
        return int(r["c"]) if r else 0

    def update(
        self,
        task_id: str,
        *,
        expected_version: int,
        fields: dict[str, object],
    ) -> TeamTaskRow | None:
        """CAS update: bumps ``version`` only when ``expected_version`` matches.

        Returns the updated row, or ``None`` when the task does not exist or
        was modified concurrently (version mismatch).
        """
        if not fields:
            return self.get(task_id)
        cols = list(fields)
        sets = ", ".join(f"{col} = ?" for col in cols)
        params: list[object] = [fields[col] for col in cols]
        ts = now_ts()
        with self._db.transaction() as conn:
            r = conn.execute(
                f"UPDATE team_tasks SET {sets}, version = version + 1, updated_at = ? "
                "WHERE task_id = ? AND version = ?",
                (*params, ts, task_id, expected_version),
            ).rowcount
            if not r:
                return None
        return self.get(task_id)
