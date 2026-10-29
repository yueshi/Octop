"""external_deliveries — outbox rows for event fan-out.

A delivery is claimed with a conditional UPDATE (the lease); a crashed worker
leaves ``status='delivering'`` until :meth:`requeue_expired` puts it back.
"""

from __future__ import annotations

from dataclasses import dataclass

from octop.infra.db.pool import DatabasePool
from octop.infra.db.repos._base import DbRow, now_ts


@dataclass(frozen=True)
class ExternalDeliveryRow:
    pk: int
    delivery_id: str
    event_id: str
    subscription_id: str
    target_type: str
    target_id: str
    status: str
    attempt: int
    next_retry_at: int | None
    lease_until: int | None
    last_error: str
    delivered_at: int | None
    created_at: int

    @classmethod
    def from_row(cls, r: DbRow) -> ExternalDeliveryRow:
        return cls(
            pk=int(r["id"]),
            delivery_id=str(r["delivery_id"]),
            event_id=str(r["event_id"]),
            subscription_id=str(r["subscription_id"]),
            target_type=str(r["target_type"]),
            target_id=str(r["target_id"]),
            status=str(r["status"] or "pending"),
            attempt=int(r["attempt"] or 0),
            next_retry_at=int(r["next_retry_at"]) if r["next_retry_at"] is not None else None,
            lease_until=int(r["lease_until"]) if r["lease_until"] is not None else None,
            last_error=str(r["last_error"] or ""),
            delivered_at=int(r["delivered_at"]) if r["delivered_at"] is not None else None,
            created_at=int(r["created_at"]),
        )


class ExternalDeliveryRepo:
    def __init__(self, db: DatabasePool) -> None:
        self._db = db

    def get(self, delivery_id: str) -> ExternalDeliveryRow | None:
        with self._db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM external_deliveries WHERE delivery_id = ?", (delivery_id,)
            ).fetchone()
        return ExternalDeliveryRow.from_row(row) if row is not None else None

    def list_by_event(self, event_id: str) -> list[ExternalDeliveryRow]:
        with self._db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM external_deliveries WHERE event_id = ? ORDER BY id",
                (event_id,),
            ).fetchall()
        return [ExternalDeliveryRow.from_row(row) for row in rows]

    def list_for_owner(
        self,
        owner_user_id: int | None,
        *,
        source_id: str | None = None,
        status: str | None = None,
        limit: int = 100,
    ) -> list[ExternalDeliveryRow]:
        sql = (
            "SELECT d.* FROM external_deliveries d "
            "JOIN external_events e ON e.event_id = d.event_id "
            "JOIN external_sources s ON s.source_id = e.source_id WHERE 1 = 1"
        )
        params: list[object] = []
        if owner_user_id is not None:
            sql += " AND s.owner_user_id = ?"
            params.append(owner_user_id)
        if source_id is not None:
            sql += " AND e.source_id = ?"
            params.append(source_id)
        if status is not None:
            sql += " AND d.status = ?"
            params.append(status)
        sql += " ORDER BY d.id DESC LIMIT ?"
        params.append(limit)
        with self._db.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [ExternalDeliveryRow.from_row(row) for row in rows]

    def list_due(self, now: int, *, limit: int = 20) -> list[ExternalDeliveryRow]:
        with self._db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM external_deliveries "
                "WHERE status IN ('pending', 'failed') "
                "AND COALESCE(next_retry_at, 0) <= ? "
                "AND (lease_until IS NULL OR lease_until < ?) "
                "ORDER BY id LIMIT ?",
                (now, now, limit),
            ).fetchall()
        return [ExternalDeliveryRow.from_row(row) for row in rows]

    def claim(
        self, delivery_id: str, *, now: int, lease_seconds: int
    ) -> ExternalDeliveryRow | None:
        with self._db.transaction() as conn:
            cur = conn.execute(
                "UPDATE external_deliveries SET status = 'delivering', lease_until = ? "
                "WHERE delivery_id = ? AND status IN ('pending', 'failed') "
                "AND COALESCE(next_retry_at, 0) <= ? "
                "AND (lease_until IS NULL OR lease_until < ?)",
                (now + lease_seconds, delivery_id, now, now),
            )
            if not cur.rowcount:
                return None
            row = conn.execute(
                "SELECT * FROM external_deliveries WHERE delivery_id = ?", (delivery_id,)
            ).fetchone()
        return ExternalDeliveryRow.from_row(row) if row is not None else None

    def finish(
        self,
        delivery_id: str,
        *,
        status: str,
        attempt: int,
        last_error: str = "",
        next_retry_at: int | None = None,
    ) -> None:
        delivered_at = now_ts() if status == "delivered" else None
        with self._db.transaction() as conn:
            conn.execute(
                "UPDATE external_deliveries SET status = ?, attempt = ?, "
                "next_retry_at = ?, lease_until = NULL, last_error = ?, delivered_at = ? "
                "WHERE delivery_id = ?",
                (status, attempt, next_retry_at, last_error[:2000], delivered_at, delivery_id),
            )

    def requeue_expired(self, now: int) -> int:
        with self._db.transaction() as conn:
            cur = conn.execute(
                "UPDATE external_deliveries SET status = 'failed', lease_until = NULL, "
                "next_retry_at = ? "
                "WHERE status = 'delivering' AND lease_until IS NOT NULL AND lease_until < ?",
                (now, now),
            )
        return int(cur.rowcount or 0)

    def retry(self, delivery_id: str) -> bool:
        with self._db.transaction() as conn:
            cur = conn.execute(
                "UPDATE external_deliveries SET status = 'pending', attempt = 0, "
                "next_retry_at = ?, lease_until = NULL, last_error = '', delivered_at = NULL "
                "WHERE delivery_id = ? AND status IN ('failed', 'dead')",
                (now_ts(), delivery_id),
            )
        return bool(cur.rowcount)
