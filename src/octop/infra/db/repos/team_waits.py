"""team_waits — sparse wait/blocking-point ledger. SQL only.

One row per wait (member reply, approval, timer, budget), not per action;
settlement is idempotent via the ``waiting`` status guard plus the
``idempotency_key`` UNIQUE (docs/team-drive-v2.md §4.4).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from octop.infra.db.pool import DatabasePool
from octop.infra.db.repos._base import DbRow, map_rows, now_ts
from octop.infra.utils.ulid import new_short_id

WAIT_KIND_MEMBER_REPLY = "member_reply"
WAIT_KIND_APPROVAL = "approval"
WAIT_KIND_TIMER = "timer"
WAIT_KIND_BUDGET = "budget"
WAIT_STATUS_WAITING = "waiting"
WAIT_STATUS_SETTLED = "settled"
WAIT_STATUS_TIMED_OUT = "timed_out"
WAIT_STATUS_CANCELLED = "cancelled"
WAIT_STATUS_DEFERRED = "deferred"


@dataclass(frozen=True)
class TeamWaitRow:
    pk: int
    wait_id: str
    team_id: str
    task_id: str | None
    kind: str
    status: str
    due_at: int | None
    timeout_at: int | None
    deadline_at: int | None
    escalation_policy: dict[str, Any]
    idempotency_key: str
    external_ref: str | None
    payload: dict[str, Any]
    attempts: int
    last_attempt_at: int | None
    created_at: int
    settled_at: int | None

    @classmethod
    def from_row(cls, r: DbRow) -> TeamWaitRow:
        def _load(raw: Any) -> dict[str, Any]:
            try:
                loaded = json.loads(str(raw or "{}"))
            except ValueError:
                return {}
            return loaded if isinstance(loaded, dict) else {}

        return cls(
            pk=int(r["id"]),
            wait_id=str(r["wait_id"]),
            team_id=str(r["team_id"]),
            task_id=str(r["task_id"]) if r["task_id"] is not None else None,
            kind=str(r["kind"] or ""),
            status=str(r["status"] or WAIT_STATUS_WAITING),
            due_at=int(r["due_at"]) if r["due_at"] is not None else None,
            timeout_at=int(r["timeout_at"]) if r["timeout_at"] is not None else None,
            deadline_at=int(r["deadline_at"]) if r["deadline_at"] is not None else None,
            escalation_policy=_load(r["escalation_policy_json"]),
            idempotency_key=str(r["idempotency_key"]),
            external_ref=str(r["external_ref"]) if r["external_ref"] is not None else None,
            payload=_load(r["payload_json"]),
            attempts=int(r["attempts"] or 0),
            last_attempt_at=(
                int(r["last_attempt_at"]) if r["last_attempt_at"] is not None else None
            ),
            created_at=int(r["created_at"]),
            settled_at=int(r["settled_at"]) if r["settled_at"] is not None else None,
        )


class TeamWaitRepo:
    def __init__(self, db: DatabasePool) -> None:
        self._db = db

    def create(
        self,
        team_id: str,
        *,
        kind: str,
        idempotency_key: str,
        task_id: str | None = None,
        due_at: int | None = None,
        timeout_at: int | None = None,
        deadline_at: int | None = None,
        escalation_policy: dict[str, Any] | None = None,
        external_ref: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> TeamWaitRow | None:
        """Create one wait, or return ``None`` when the idempotency key exists."""
        wait_id = new_short_id()
        ts = now_ts()
        with self._db.transaction() as conn:
            seen = conn.execute(
                "SELECT 1 FROM team_waits WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if seen:
                return None
            conn.execute(
                "INSERT INTO team_waits("
                "wait_id, team_id, task_id, kind, due_at, timeout_at, deadline_at, "
                "escalation_policy_json, idempotency_key, external_ref, payload_json, "
                "created_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    wait_id,
                    team_id,
                    task_id,
                    kind,
                    due_at,
                    timeout_at,
                    deadline_at,
                    json.dumps(escalation_policy or {}, ensure_ascii=False),
                    idempotency_key,
                    external_ref,
                    json.dumps(payload or {}, ensure_ascii=False),
                    ts,
                ),
            )
        return self.get(wait_id)

    def get(self, wait_id: str) -> TeamWaitRow | None:
        with self._db.connect() as conn:
            r = conn.execute(
                "SELECT * FROM team_waits WHERE wait_id = ?",
                (wait_id,),
            ).fetchone()
        return TeamWaitRow.from_row(r) if r else None

    def get_by_idempotency_key(self, key: str) -> TeamWaitRow | None:
        with self._db.connect() as conn:
            r = conn.execute(
                "SELECT * FROM team_waits WHERE idempotency_key = ?",
                (key,),
            ).fetchone()
        return TeamWaitRow.from_row(r) if r else None

    def list_open_for_team(self, team_id: str) -> list[TeamWaitRow]:
        with self._db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM team_waits WHERE team_id = ? AND status = ? ORDER BY created_at, id",
                (team_id, WAIT_STATUS_WAITING),
            ).fetchall()
        return map_rows(rows, TeamWaitRow)

    def list_by_team(
        self,
        team_id: str,
        *,
        status: str | None = None,
        limit: int = 100,
    ) -> list[TeamWaitRow]:
        sql = "SELECT * FROM team_waits WHERE team_id = ?"
        params: list[object] = [team_id]
        if status is not None:
            sql += " AND status = ?"
            params.append(status)
        sql += " ORDER BY created_at DESC, id DESC LIMIT ?"
        params.append(limit)
        with self._db.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return map_rows(rows, TeamWaitRow)

    def get_open_by_external_ref(
        self,
        external_ref: str,
        *,
        kind: str | None = None,
    ) -> TeamWaitRow | None:
        """Find the open wait parked on an inbox job / HITL pause."""
        sql = "SELECT * FROM team_waits WHERE external_ref = ? AND status = ?"
        params: list[object] = [external_ref, WAIT_STATUS_WAITING]
        if kind is not None:
            sql += " AND kind = ?"
            params.append(kind)
        sql += " ORDER BY created_at DESC, id DESC LIMIT 1"
        with self._db.connect() as conn:
            r = conn.execute(sql, params).fetchone()
        return TeamWaitRow.from_row(r) if r else None

    def get_by_external_ref(
        self,
        external_ref: str,
        *,
        kind: str | None = None,
    ) -> TeamWaitRow | None:
        """Latest wait for a ref in any status (late replies after a timeout)."""
        sql = "SELECT * FROM team_waits WHERE external_ref = ?"
        params: list[object] = [external_ref]
        if kind is not None:
            sql += " AND kind = ?"
            params.append(kind)
        sql += " ORDER BY created_at DESC, id DESC LIMIT 1"
        with self._db.connect() as conn:
            r = conn.execute(sql, params).fetchone()
        return TeamWaitRow.from_row(r) if r else None

    def list_open_by_kind(self, kind: str) -> list[TeamWaitRow]:
        """Open waits of one kind across teams (startup recovery)."""
        with self._db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM team_waits WHERE status = ? AND kind = ? ORDER BY created_at, id",
                (WAIT_STATUS_WAITING, kind),
            ).fetchall()
        return map_rows(rows, TeamWaitRow)

    def list_open_for_task(self, task_id: str, *, kind: str | None = None) -> list[TeamWaitRow]:
        sql = "SELECT * FROM team_waits WHERE task_id = ? AND status = ?"
        params: list[object] = [task_id, WAIT_STATUS_WAITING]
        if kind is not None:
            sql += " AND kind = ?"
            params.append(kind)
        sql += " ORDER BY created_at, id"
        with self._db.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return map_rows(rows, TeamWaitRow)

    def set_timeout(self, wait_id: str, timeout_at: int | None) -> None:
        """Move an open wait's operation timeout (does not count as a retry)."""
        with self._db.transaction() as conn:
            conn.execute(
                "UPDATE team_waits SET timeout_at = ? WHERE wait_id = ? AND status = ?",
                (timeout_at, wait_id, WAIT_STATUS_WAITING),
            )

    def set_external_ref(self, wait_id: str, external_ref: str) -> None:
        with self._db.transaction() as conn:
            conn.execute(
                "UPDATE team_waits SET external_ref = ? WHERE wait_id = ? AND status = ?",
                (external_ref, wait_id, WAIT_STATUS_WAITING),
            )

    def count_for_task(self, task_id: str, *, kind: str | None = None) -> int:
        """Number of waits ever written for a task (dispatch attempt numbering)."""
        sql = "SELECT COUNT(*) AS c FROM team_waits WHERE task_id = ?"
        params: list[object] = [task_id]
        if kind is not None:
            sql += " AND kind = ?"
            params.append(kind)
        with self._db.connect() as conn:
            r = conn.execute(sql, params).fetchone()
        return int(r["c"]) if r else 0

    def reactivate_deferred(self, team_id: str) -> int:
        """Resume pause-deferred waits; the sweeper re-evaluates due timers."""
        with self._db.transaction() as conn:
            changed = conn.execute(
                "UPDATE team_waits SET status = ?, settled_at = NULL "
                "WHERE team_id = ? AND status = ?",
                (WAIT_STATUS_WAITING, team_id, WAIT_STATUS_DEFERRED),
            ).rowcount
        return int(changed)

    def list_due(self, *, now: int | None = None) -> list[TeamWaitRow]:
        """Waiting timer/due rows whose ``due_at`` has passed (sweeper input)."""
        ts = now_ts() if now is None else int(now)
        with self._db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM team_waits WHERE status = ? "
                "AND due_at IS NOT NULL AND due_at <= ? ORDER BY due_at, id",
                (WAIT_STATUS_WAITING, ts),
            ).fetchall()
        return map_rows(rows, TeamWaitRow)

    def list_timed_out(self, *, now: int | None = None) -> list[TeamWaitRow]:
        """Waiting rows whose operation ``timeout_at`` has passed (sweeper input)."""
        ts = now_ts() if now is None else int(now)
        with self._db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM team_waits WHERE status = ? "
                "AND timeout_at IS NOT NULL AND timeout_at <= ? ORDER BY timeout_at, id",
                (WAIT_STATUS_WAITING, ts),
            ).fetchall()
        return map_rows(rows, TeamWaitRow)

    def settle(
        self,
        wait_id: str,
        *,
        status: str,
        external_ref: str | None = None,
    ) -> bool:
        """Settle a waiting row; ``False`` means it was already settled."""
        if status == WAIT_STATUS_WAITING:
            raise ValueError("settle requires a terminal or paused status")
        sets = ["status = ?", "settled_at = ?"]
        params: list[object] = [status, now_ts()]
        if external_ref is not None:
            sets.append("external_ref = ?")
            params.append(external_ref)
        with self._db.transaction() as conn:
            changed = conn.execute(
                f"UPDATE team_waits SET {', '.join(sets)} WHERE wait_id = ? AND status = ?",
                (*params, wait_id, WAIT_STATUS_WAITING),
            ).rowcount
        return bool(changed)

    def bump_attempt(
        self,
        wait_id: str,
        *,
        timeout_at: int | None = None,
        external_ref: str | None = None,
    ) -> None:
        sets = ["attempts = attempts + 1", "last_attempt_at = ?"]
        params: list[object] = [now_ts()]
        if timeout_at is not None:
            sets.append("timeout_at = ?")
            params.append(int(timeout_at))
        if external_ref is not None:
            sets.append("external_ref = ?")
            params.append(external_ref)
        with self._db.transaction() as conn:
            conn.execute(
                f"UPDATE team_waits SET {', '.join(sets)} WHERE wait_id = ?",
                (*params, wait_id),
            )

    def cancel_open_for_team(self, team_id: str) -> int:
        """Kill-switch helper: cancel every waiting or pause-deferred row."""
        ts = now_ts()
        with self._db.transaction() as conn:
            changed = conn.execute(
                "UPDATE team_waits SET status = ?, settled_at = ? "
                "WHERE team_id = ? AND status IN (?, ?)",
                (WAIT_STATUS_CANCELLED, ts, team_id, WAIT_STATUS_WAITING, WAIT_STATUS_DEFERRED),
            ).rowcount
        return int(changed)
