"""team_events — append-only team event ledger. SQL only.

Every control-flow-relevant fact lands here before it drives anything; the
``dedupe_key`` UNIQUE makes ingestion idempotent and ``seq`` gives per-team
ordering for watermarks (docs/team-drive-v2.md §4.2).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from octop.infra.db.pool import DatabasePool
from octop.infra.db.repos._base import DbRow, map_rows, now_ts
from octop.infra.utils.ulid import new_short_id


@dataclass(frozen=True)
class TeamEventRow:
    pk: int
    event_id: str
    team_id: str
    seq: int
    type: str
    actor: str
    payload: dict[str, Any]
    dedupe_key: str
    decision_id: str | None
    created_at: int
    consumed_at: int | None

    @classmethod
    def from_row(cls, r: DbRow) -> TeamEventRow:
        try:
            payload = json.loads(str(r["payload_json"] or "{}"))
        except ValueError:
            payload = {}
        return cls(
            pk=int(r["id"]),
            event_id=str(r["event_id"]),
            team_id=str(r["team_id"]),
            seq=int(r["seq"] or 0),
            type=str(r["type"] or ""),
            actor=str(r["actor"] or ""),
            payload=payload if isinstance(payload, dict) else {},
            dedupe_key=str(r["dedupe_key"]),
            decision_id=str(r["decision_id"]) if r["decision_id"] is not None else None,
            created_at=int(r["created_at"]),
            consumed_at=int(r["consumed_at"]) if r["consumed_at"] is not None else None,
        )


class TeamEventRepo:
    def __init__(self, db: DatabasePool) -> None:
        self._db = db

    def append(
        self,
        team_id: str,
        *,
        type: str,
        dedupe_key: str,
        actor: str = "",
        payload: dict[str, Any] | None = None,
        decision_id: str | None = None,
    ) -> TeamEventRow | None:
        """Append one event, or return ``None`` when ``dedupe_key`` was seen.

        The seq is allocated inside the same transaction as the insert so a
        restart cannot produce gaps.
        """
        event_id = new_short_id()
        ts = now_ts()
        with self._db.transaction() as conn:
            seen = conn.execute(
                "SELECT 1 FROM team_events WHERE dedupe_key = ?",
                (dedupe_key,),
            ).fetchone()
            if seen:
                return None
            conn.execute(
                "INSERT INTO team_events("
                "event_id, team_id, seq, type, actor, payload_json, dedupe_key, "
                "decision_id, created_at"
                ") VALUES (?, ?, "
                "(SELECT COALESCE(MAX(seq), 0) + 1 FROM team_events WHERE team_id = ?), "
                "?, ?, ?, ?, ?, ?)",
                (
                    event_id,
                    team_id,
                    team_id,
                    type,
                    actor,
                    json.dumps(payload or {}, ensure_ascii=False),
                    dedupe_key,
                    decision_id,
                    ts,
                ),
            )
        return self.get(event_id)

    def get(self, event_id: str) -> TeamEventRow | None:
        with self._db.connect() as conn:
            r = conn.execute(
                "SELECT * FROM team_events WHERE event_id = ?",
                (event_id,),
            ).fetchone()
        return TeamEventRow.from_row(r) if r else None

    def list_after(
        self,
        team_id: str,
        *,
        after_seq: int = 0,
        limit: int = 100,
    ) -> list[TeamEventRow]:
        with self._db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM team_events WHERE team_id = ? AND seq > ? ORDER BY seq LIMIT ?",
                (team_id, after_seq, limit),
            ).fetchall()
        return map_rows(rows, TeamEventRow)

    def list_unconsumed(
        self,
        team_id: str,
        *,
        older_than: int | None = None,
        limit: int = 100,
    ) -> list[TeamEventRow]:
        """Events not yet routed; ``older_than`` leaves an aggregation window."""
        sql = "SELECT * FROM team_events WHERE team_id = ? AND consumed_at IS NULL"
        params: list[object] = [team_id]
        if older_than is not None:
            sql += " AND created_at <= ?"
            params.append(int(older_than))
        sql += " ORDER BY seq LIMIT ?"
        params.append(limit)
        with self._db.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return map_rows(rows, TeamEventRow)

    def mark_consumed(self, event_id: str, *, decision_id: str | None = None) -> None:
        with self._db.transaction() as conn:
            conn.execute(
                "UPDATE team_events SET consumed_at = ?, decision_id = ? "
                "WHERE event_id = ? AND consumed_at IS NULL",
                (now_ts(), decision_id, event_id),
            )

    def mark_consumed_many(self, event_ids: list[str], *, decision_id: str | None = None) -> None:
        if not event_ids:
            return
        placeholders = ", ".join("?" for _ in event_ids)
        with self._db.transaction() as conn:
            conn.execute(
                f"UPDATE team_events SET consumed_at = ?, decision_id = ? "
                f"WHERE event_id IN ({placeholders}) AND consumed_at IS NULL",
                (now_ts(), decision_id, *event_ids),
            )
