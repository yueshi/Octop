"""team_runtime — per-team coordinator state. SQL only.

One row per team host; the authoritative state machine status lives here so a
restart resumes instead of re-deriving (docs/team-drive-v2.md §4.1). Updates
use a version CAS like ``team_tasks`` so concurrent writers cannot clobber.
"""

from __future__ import annotations

from dataclasses import dataclass

from octop.infra.db.pool import DatabasePool
from octop.infra.db.repos._base import DbRow, map_rows, now_ts

RUNTIME_STATUS_IDLE = "idle"
RUNTIME_OPEN_STATUSES = ("running", "deciding", "dispatching", "waiting")


@dataclass(frozen=True)
class TeamRuntimeRow:
    team_id: str
    status: str
    mode: str
    goal_id: str | None
    active_decision_id: str | None
    last_event_seq: int
    last_decision_at: int | None
    next_heartbeat_at: int | None
    heartbeat_interval_sec: int
    budget_day: str
    budget_used: int
    budget_reserved: int
    paused_reason: str | None
    version: int
    created_at: int
    updated_at: int

    @classmethod
    def from_row(cls, r: DbRow) -> TeamRuntimeRow:
        return cls(
            team_id=str(r["team_id"]),
            status=str(r["status"] or RUNTIME_STATUS_IDLE),
            mode=str(r["mode"] or "manual"),
            goal_id=str(r["goal_id"]) if r["goal_id"] is not None else None,
            active_decision_id=(
                str(r["active_decision_id"]) if r["active_decision_id"] is not None else None
            ),
            last_event_seq=int(r["last_event_seq"] or 0),
            last_decision_at=(
                int(r["last_decision_at"]) if r["last_decision_at"] is not None else None
            ),
            next_heartbeat_at=(
                int(r["next_heartbeat_at"]) if r["next_heartbeat_at"] is not None else None
            ),
            heartbeat_interval_sec=int(r["heartbeat_interval_sec"] or 1800),
            budget_day=str(r["budget_day"] or ""),
            budget_used=int(r["budget_used"] or 0),
            budget_reserved=int(r["budget_reserved"] or 0),
            paused_reason=str(r["paused_reason"]) if r["paused_reason"] is not None else None,
            version=int(r["version"] or 1),
            created_at=int(r["created_at"]),
            updated_at=int(r["updated_at"]),
        )


class TeamRuntimeRepo:
    def __init__(self, db: DatabasePool) -> None:
        self._db = db

    def get(self, team_id: str) -> TeamRuntimeRow | None:
        with self._db.connect() as conn:
            r = conn.execute(
                "SELECT * FROM team_runtime WHERE team_id = ?",
                (team_id,),
            ).fetchone()
        return TeamRuntimeRow.from_row(r) if r else None

    def ensure(self, team_id: str) -> TeamRuntimeRow:
        """Return the row, creating an idle default when missing."""
        row = self.get(team_id)
        if row is not None:
            return row
        ts = now_ts()
        with self._db.transaction() as conn:
            conn.execute(
                "INSERT INTO team_runtime(team_id, created_at, updated_at) "
                "VALUES (?, ?, ?) ON CONFLICT(team_id) DO NOTHING",
                (team_id, ts, ts),
            )
        row = self.get(team_id)
        if row is None:
            raise RuntimeError(f"team_runtime insert failed: {team_id}")
        return row

    def update(
        self,
        team_id: str,
        *,
        expected_version: int,
        fields: dict[str, object],
    ) -> TeamRuntimeRow | None:
        """CAS update: bumps ``version`` only when ``expected_version`` matches."""
        if not fields:
            return self.get(team_id)
        cols = list(fields)
        sets = ", ".join(f"{col} = ?" for col in cols)
        params: list[object] = [fields[col] for col in cols]
        ts = now_ts()
        with self._db.transaction() as conn:
            r = conn.execute(
                f"UPDATE team_runtime SET {sets}, version = version + 1, updated_at = ? "
                "WHERE team_id = ? AND version = ?",
                (*params, ts, team_id, expected_version),
            ).rowcount
            if not r:
                return None
        return self.get(team_id)

    def list_open(self) -> list[TeamRuntimeRow]:
        """Rows in an active coordination status (sweeper/heartbeat inputs)."""
        placeholders = ", ".join("?" for _ in RUNTIME_OPEN_STATUSES)
        with self._db.connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM team_runtime WHERE status IN ({placeholders})",
                RUNTIME_OPEN_STATUSES,
            ).fetchall()
        return map_rows(rows, TeamRuntimeRow)

    def list_autopilot(self) -> list[TeamRuntimeRow]:
        """Teams whose coordinator owns the loop (idle included for event replay)."""
        with self._db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM team_runtime WHERE mode = 'autopilot' "
                "AND status NOT IN ('paused_user', 'paused_budget', 'stopped', 'finishing')",
            ).fetchall()
        return map_rows(rows, TeamRuntimeRow)

    def add_budget_used(self, team_id: str, tokens: int) -> int:
        """Atomically add one turn's usage to the day counter (INV-3).

        ``SET budget_used = budget_used + ?`` is the whole point: a
        read-modify-write would lose turns whenever two agents report
        concurrently, and a lost turn is money spent with no record of it.
        Returns the new total, or 0 when the row is missing.
        """
        if tokens <= 0:
            return 0
        ts = now_ts()
        with self._db.transaction() as conn:
            conn.execute(
                "UPDATE team_runtime SET budget_used = budget_used + ?, updated_at = ? "
                "WHERE team_id = ?",
                (int(tokens), ts, team_id),
            )
            row = conn.execute(
                "SELECT budget_used FROM team_runtime WHERE team_id = ?", (team_id,)
            ).fetchone()
        return int(row[0]) if row else 0

    def reset_budget(self, *, day: str, month: str | None = None) -> None:
        """Roll the budget counters at the period boundary (server timezone).

        ``budget_day`` holds the *current period key*: ``YYYY-MM-DD`` for a
        daily budget, ``YYYY-MM`` for a monthly one. Both live in one column
        so a single ``budget_used`` counter serves either window — the policy
        row decides which key applies, and a window change re-keys on the next
        roll.
        """
        ts = now_ts()
        month_key = month if month is not None else day[:7]
        window_sql = (
            "COALESCE((SELECT budget_window FROM proactive_care_config c "
            "WHERE c.agent_id = team_runtime.team_id), 'day')"
        )
        with self._db.transaction() as conn:
            conn.execute(
                f"UPDATE team_runtime SET budget_day = ?, budget_used = 0, "
                f"budget_reserved = 0, updated_at = ? "
                f"WHERE budget_day <> ? AND {window_sql} <> 'month'",
                (day, ts, day),
            )
            conn.execute(
                f"UPDATE team_runtime SET budget_day = ?, budget_used = 0, "
                f"budget_reserved = 0, updated_at = ? "
                f"WHERE budget_day <> ? AND {window_sql} = 'month'",
                (month_key, ts, month_key),
            )
