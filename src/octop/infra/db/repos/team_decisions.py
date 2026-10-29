"""team_decisions — one budgeted decision-unit run per row. SQL only.

Status flow is ``pending → running → committed | failed | abandoned``; callers
enforce the single-active-decision invariant via :meth:`get_active`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from octop.infra.db.pool import DatabasePool
from octop.infra.db.repos._base import DbRow, map_rows, now_ts
from octop.infra.utils.ulid import new_short_id

DECISION_STATUS_PENDING = "pending"
DECISION_STATUS_RUNNING = "running"
DECISION_OPEN_STATUSES = (DECISION_STATUS_PENDING, DECISION_STATUS_RUNNING)


@dataclass(frozen=True)
class TeamDecisionRow:
    pk: int
    decision_id: str
    team_id: str
    trigger_event_ids: list[str]
    mode: str
    status: str
    reason: str
    snapshot_hash: str
    plan: dict[str, Any] | None
    budget_reserved: int
    cost_tokens: int
    thread_id: str | None
    error: str | None
    started_at: int | None
    finished_at: int | None
    created_at: int

    @classmethod
    def from_row(cls, r: DbRow) -> TeamDecisionRow:
        try:
            trigger = json.loads(str(r["trigger_event_ids_json"] or "[]"))
        except ValueError:
            trigger = []
        plan_raw = r["plan_json"]
        plan: dict[str, Any] | None = None
        if plan_raw:
            try:
                loaded = json.loads(str(plan_raw))
                plan = loaded if isinstance(loaded, dict) else None
            except ValueError:
                plan = None
        return cls(
            pk=int(r["id"]),
            decision_id=str(r["decision_id"]),
            team_id=str(r["team_id"]),
            trigger_event_ids=[str(x) for x in trigger] if isinstance(trigger, list) else [],
            mode=str(r["mode"] or "autonomous"),
            status=str(r["status"] or DECISION_STATUS_PENDING),
            reason=str(r["reason"] or ""),
            snapshot_hash=str(r["snapshot_hash"] or ""),
            plan=plan,
            budget_reserved=int(r["budget_reserved"] or 0),
            cost_tokens=int(r["cost_tokens"] or 0),
            thread_id=str(r["thread_id"]) if r["thread_id"] is not None else None,
            error=str(r["error"]) if r["error"] is not None else None,
            started_at=int(r["started_at"]) if r["started_at"] is not None else None,
            finished_at=int(r["finished_at"]) if r["finished_at"] is not None else None,
            created_at=int(r["created_at"]),
        )


class TeamDecisionRepo:
    def __init__(self, db: DatabasePool) -> None:
        self._db = db

    def create(
        self,
        team_id: str,
        *,
        trigger_event_ids: list[str] | None = None,
        mode: str = "autonomous",
        reason: str = "",
    ) -> TeamDecisionRow:
        decision_id = new_short_id()
        with self._db.transaction() as conn:
            conn.execute(
                "INSERT INTO team_decisions("
                "decision_id, team_id, trigger_event_ids_json, mode, reason, created_at"
                ") VALUES (?, ?, ?, ?, ?, ?)",
                (
                    decision_id,
                    team_id,
                    json.dumps(trigger_event_ids or [], ensure_ascii=False),
                    mode,
                    reason,
                    now_ts(),
                ),
            )
        row = self.get(decision_id)
        if row is None:
            raise RuntimeError(f"team decision insert failed: {decision_id}")
        return row

    def get(self, decision_id: str) -> TeamDecisionRow | None:
        with self._db.connect() as conn:
            r = conn.execute(
                "SELECT * FROM team_decisions WHERE decision_id = ?",
                (decision_id,),
            ).fetchone()
        return TeamDecisionRow.from_row(r) if r else None

    def get_active(self, team_id: str) -> TeamDecisionRow | None:
        placeholders = ", ".join("?" for _ in DECISION_OPEN_STATUSES)
        with self._db.connect() as conn:
            r = conn.execute(
                f"SELECT * FROM team_decisions WHERE team_id = ? AND status IN ({placeholders}) "
                "ORDER BY created_at DESC, id DESC LIMIT 1",
                (team_id, *DECISION_OPEN_STATUSES),
            ).fetchone()
        return TeamDecisionRow.from_row(r) if r else None

    def mark_running(
        self,
        decision_id: str,
        *,
        thread_id: str | None = None,
        budget_reserved: int | None = None,
    ) -> None:
        sets = ["status = ?", "started_at = ?"]
        params: list[object] = [DECISION_STATUS_RUNNING, now_ts()]
        if thread_id is not None:
            sets.append("thread_id = ?")
            params.append(thread_id)
        if budget_reserved is not None:
            sets.append("budget_reserved = ?")
            params.append(int(budget_reserved))
        with self._db.transaction() as conn:
            conn.execute(
                f"UPDATE team_decisions SET {', '.join(sets)} WHERE decision_id = ?",
                (*params, decision_id),
            )

    def finish(
        self,
        decision_id: str,
        *,
        status: str,
        plan: dict[str, Any] | None = None,
        snapshot_hash: str | None = None,
        cost_tokens: int | None = None,
        error: str | None = None,
    ) -> None:
        sets = ["status = ?", "finished_at = ?"]
        params: list[object] = [status, now_ts()]
        if plan is not None:
            sets.append("plan_json = ?")
            params.append(json.dumps(plan, ensure_ascii=False))
        if snapshot_hash is not None:
            sets.append("snapshot_hash = ?")
            params.append(snapshot_hash)
        if cost_tokens is not None:
            sets.append("cost_tokens = ?")
            params.append(int(cost_tokens))
        if error is not None:
            sets.append("error = ?")
            params.append(error)
        with self._db.transaction() as conn:
            conn.execute(
                f"UPDATE team_decisions SET {', '.join(sets)} WHERE decision_id = ?",
                (*params, decision_id),
            )

    def list_by_team(
        self,
        team_id: str,
        *,
        limit: int = 50,
    ) -> list[TeamDecisionRow]:
        with self._db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM team_decisions WHERE team_id = ? "
                "ORDER BY created_at DESC, id DESC LIMIT ?",
                (team_id, limit),
            ).fetchall()
        return map_rows(rows, TeamDecisionRow)

    def list_stale(self, *, older_than: int, limit: int = 100) -> list[TeamDecisionRow]:
        """Open decisions whose last activity predates ``older_than`` (sweeper)."""
        placeholders = ", ".join("?" for _ in DECISION_OPEN_STATUSES)
        with self._db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM team_decisions "
                f"WHERE status IN ({placeholders}) "
                "AND COALESCE(started_at, created_at) <= ? "
                "ORDER BY created_at LIMIT ?",
                (*DECISION_OPEN_STATUSES, int(older_than), limit),
            ).fetchall()
        return map_rows(rows, TeamDecisionRow)
