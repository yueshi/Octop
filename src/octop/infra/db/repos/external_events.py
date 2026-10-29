"""external_events — durable inbound event inbox.

``ingest`` writes the event and its deliveries in one transaction so a crash
cannot leave an inbox row that never fans out. ``dedupe_key`` is UNIQUE; a
repeat returns the existing row instead of enqueuing duplicate work.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from octop.infra.db.pool import DatabasePool
from octop.infra.db.repos._base import DbRow, now_ts
from octop.infra.utils.ulid import new_short_id


@dataclass(frozen=True)
class ExternalEventRow:
    pk: int
    event_id: str
    source_id: str
    external_id: str
    event_type: str
    occurred_at: int
    received_at: int
    summary: str
    payload: dict[str, Any]
    payload_json: str
    payload_bytes: int
    dedupe_key: str
    status: str
    created_at: int

    @classmethod
    def from_row(cls, r: DbRow) -> ExternalEventRow:
        raw = str(r["payload_json"] or "{}")
        try:
            payload = json.loads(raw)
        except ValueError:
            payload = {}
        return cls(
            pk=int(r["id"]),
            event_id=str(r["event_id"]),
            source_id=str(r["source_id"]),
            external_id=str(r["external_id"] or ""),
            event_type=str(r["event_type"]),
            occurred_at=int(r["occurred_at"]),
            received_at=int(r["received_at"]),
            summary=str(r["summary"] or ""),
            payload=payload if isinstance(payload, dict) else {},
            payload_json=raw,
            payload_bytes=int(r["payload_bytes"] or 0),
            dedupe_key=str(r["dedupe_key"]),
            status=str(r["status"] or "received"),
            created_at=int(r["created_at"]),
        )


class ExternalEventRepo:
    def __init__(self, db: DatabasePool) -> None:
        self._db = db

    def ingest(
        self,
        *,
        source_id: str,
        event_type: str,
        external_id: str,
        occurred_at: int,
        summary: str,
        payload_json: str,
        payload_bytes: int,
        dedupe_key: str,
        subscriptions: list[tuple[str, str, str]],
    ) -> tuple[ExternalEventRow, bool, int]:
        """Insert one event (or return the deduped one) and enqueue deliveries.

        ``subscriptions`` is ``(subscription_id, target_type, target_id)``.
        Returns ``(row, duplicate, delivery_count)``.
        """
        event_id = new_short_id()
        ts = now_ts()
        with self._db.transaction() as conn:
            seen = conn.execute(
                "SELECT * FROM external_events WHERE dedupe_key = ?", (dedupe_key,)
            ).fetchone()
            if seen is not None:
                return ExternalEventRow.from_row(seen), True, 0
            conn.execute(
                "INSERT INTO external_events("
                "event_id, source_id, external_id, event_type, occurred_at, "
                "received_at, summary, payload_json, payload_bytes, dedupe_key, "
                "status, created_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'received', ?)",
                (
                    event_id,
                    source_id,
                    external_id,
                    event_type,
                    occurred_at,
                    ts,
                    summary,
                    payload_json,
                    payload_bytes,
                    dedupe_key,
                    ts,
                ),
            )
            created = 0
            for subscription_id, target_type, target_id in subscriptions:
                cur = conn.execute(
                    "INSERT INTO external_deliveries("
                    "delivery_id, event_id, subscription_id, target_type, target_id, "
                    "status, attempt, created_at"
                    ") VALUES (?, ?, ?, ?, ?, 'pending', 0, ?) "
                    "ON CONFLICT(event_id, subscription_id) DO NOTHING",
                    (
                        new_short_id(),
                        event_id,
                        subscription_id,
                        target_type,
                        target_id,
                        ts,
                    ),
                )
                created += int(cur.rowcount or 0)
            status = "dispatched" if created else "no_subscribers"
            conn.execute(
                "UPDATE external_events SET status = ? WHERE event_id = ?",
                (status, event_id),
            )
            row = conn.execute(
                "SELECT * FROM external_events WHERE event_id = ?", (event_id,)
            ).fetchone()
        if row is None:  # pragma: no cover - insert + select in one transaction
            raise RuntimeError("external event insert returned no row")
        return ExternalEventRow.from_row(row), False, created

    def get(self, event_id: str) -> ExternalEventRow | None:
        with self._db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM external_events WHERE event_id = ?", (event_id,)
            ).fetchone()
        return ExternalEventRow.from_row(row) if row is not None else None

    def get_by_dedupe(self, dedupe_key: str) -> ExternalEventRow | None:
        with self._db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM external_events WHERE dedupe_key = ?", (dedupe_key,)
            ).fetchone()
        return ExternalEventRow.from_row(row) if row is not None else None

    def list_for_owner(
        self,
        owner_user_id: int | None,
        *,
        source_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[ExternalEventRow]:
        sql = (
            "SELECT e.* FROM external_events e "
            "JOIN external_sources s ON s.source_id = e.source_id WHERE 1 = 1"
        )
        params: list[object] = []
        if owner_user_id is not None:
            sql += " AND s.owner_user_id = ?"
            params.append(owner_user_id)
        if source_id is not None:
            sql += " AND e.source_id = ?"
            params.append(source_id)
        sql += " ORDER BY e.received_at DESC, e.id DESC LIMIT ? OFFSET ?"
        params.extend((limit, offset))
        with self._db.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [ExternalEventRow.from_row(row) for row in rows]

    def delete_older_than(self, cutoff: int) -> int:
        with self._db.transaction() as conn:
            cur = conn.execute("DELETE FROM external_events WHERE received_at < ?", (cutoff,))
        return int(cur.rowcount or 0)
