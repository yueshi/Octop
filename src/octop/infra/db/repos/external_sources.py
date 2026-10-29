"""external_sources — inbound event sources owned by one user."""

from __future__ import annotations

from dataclasses import dataclass

from octop.infra.db.pool import DatabasePool
from octop.infra.db.repos._base import (
    UNSET,
    DbRow,
    bool_int,
    map_rows,
    now_ts,
    optional_updates,
)
from octop.infra.utils.ulid import new_short_id


@dataclass(frozen=True)
class ExternalSourceRow:
    pk: int
    source_id: str
    owner_user_id: int
    name: str
    kind: str
    config_json: str
    secret_blob: bytes | None
    enabled: bool
    cursor_json: str
    next_run_at: int | None
    fail_count: int
    last_poll_at: int | None
    last_error: str
    created_at: int
    updated_at: int

    @classmethod
    def from_row(cls, r: DbRow) -> ExternalSourceRow:
        blob = r["secret_blob"]
        return cls(
            pk=int(r["id"]),
            source_id=str(r["source_id"]),
            owner_user_id=int(r["owner_user_id"]),
            name=str(r["name"]),
            kind=str(r["kind"]),
            config_json=str(r["config_json"] or "{}"),
            secret_blob=bytes(blob) if blob is not None else None,
            enabled=bool(int(r["enabled"] or 0)),
            cursor_json=str(r["cursor_json"] or "{}"),
            next_run_at=int(r["next_run_at"]) if r["next_run_at"] is not None else None,
            fail_count=int(r["fail_count"] or 0),
            last_poll_at=int(r["last_poll_at"]) if r["last_poll_at"] is not None else None,
            last_error=str(r["last_error"] or ""),
            created_at=int(r["created_at"]),
            updated_at=int(r["updated_at"]),
        )


class ExternalSourceRepo:
    def __init__(self, db: DatabasePool) -> None:
        self._db = db

    def create(
        self,
        *,
        owner_user_id: int,
        name: str,
        kind: str,
        config_json: str = "{}",
        secret_blob: bytes | None = None,
    ) -> ExternalSourceRow:
        source_id = new_short_id()
        ts = now_ts()
        with self._db.transaction() as conn:
            conn.execute(
                "INSERT INTO external_sources("
                "source_id, owner_user_id, name, kind, config_json, secret_blob, "
                "enabled, cursor_json, fail_count, last_error, created_at, updated_at"
                ") VALUES (?, ?, ?, ?, ?, ?, 1, '{}', 0, '', ?, ?)",
                (source_id, owner_user_id, name, kind, config_json, secret_blob, ts, ts),
            )
            row = conn.execute(
                "SELECT * FROM external_sources WHERE source_id = ?", (source_id,)
            ).fetchone()
        if row is None:  # pragma: no cover - insert + select in one transaction
            raise RuntimeError("external source insert returned no row")
        return ExternalSourceRow.from_row(row)

    def get(self, source_id: str) -> ExternalSourceRow | None:
        with self._db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM external_sources WHERE source_id = ?", (source_id,)
            ).fetchone()
        return ExternalSourceRow.from_row(row) if row is not None else None

    def list_for_owner(self, owner_user_id: int | None) -> list[ExternalSourceRow]:
        sql = "SELECT * FROM external_sources"
        params: tuple[object, ...] = ()
        if owner_user_id is not None:
            sql += " WHERE owner_user_id = ?"
            params = (owner_user_id,)
        sql += " ORDER BY id"
        with self._db.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return map_rows(rows, ExternalSourceRow)

    def update(
        self,
        source_id: str,
        *,
        name: object = UNSET,
        enabled: object = UNSET,
        config_json: object = UNSET,
        secret_blob: object = UNSET,
        cursor_json: object = UNSET,
        next_run_at: object = UNSET,
        fail_count: object = UNSET,
        last_poll_at: object = UNSET,
        last_error: object = UNSET,
    ) -> ExternalSourceRow | None:
        pairs = [
            ("name", name),
            ("enabled", bool_int(enabled) if isinstance(enabled, bool) else enabled),
            ("config_json", config_json),
            ("secret_blob", secret_blob),
            ("cursor_json", cursor_json),
            ("next_run_at", next_run_at),
            ("fail_count", fail_count),
            ("last_poll_at", last_poll_at),
            ("last_error", last_error),
        ]
        clauses, params = optional_updates(pairs)
        clauses.append("updated_at = ?")
        params.append(now_ts())
        with self._db.transaction() as conn:
            conn.execute(
                f"UPDATE external_sources SET {', '.join(clauses)} WHERE source_id = ?",
                (*params, source_id),
            )
            row = conn.execute(
                "SELECT * FROM external_sources WHERE source_id = ?", (source_id,)
            ).fetchone()
        return ExternalSourceRow.from_row(row) if row is not None else None

    def list_due_polls(self, now: int, *, limit: int = 10) -> list[ExternalSourceRow]:
        with self._db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM external_sources "
                "WHERE kind = 'connector_poll' AND enabled = 1 "
                "AND COALESCE(next_run_at, 0) <= ? ORDER BY id LIMIT ?",
                (now, limit),
            ).fetchall()
        return map_rows(rows, ExternalSourceRow)

    def claim_poll(self, source_id: str, *, now: int, lease_seconds: int) -> bool:
        with self._db.transaction() as conn:
            cur = conn.execute(
                "UPDATE external_sources SET next_run_at = ?, updated_at = ? "
                "WHERE source_id = ? AND kind = 'connector_poll' AND enabled = 1 "
                "AND COALESCE(next_run_at, 0) <= ?",
                (now + lease_seconds, now_ts(), source_id, now),
            )
        return bool(cur.rowcount)

    def record_poll_success(self, source_id: str, *, cursor_json: str, next_run_at: int) -> None:
        now = now_ts()
        with self._db.transaction() as conn:
            conn.execute(
                "UPDATE external_sources SET cursor_json = ?, next_run_at = ?, "
                "fail_count = 0, last_poll_at = ?, last_error = '', updated_at = ? "
                "WHERE source_id = ?",
                (cursor_json, next_run_at, now, now, source_id),
            )

    def record_poll_error(self, source_id: str, *, error: str, next_run_at: int) -> None:
        now = now_ts()
        with self._db.transaction() as conn:
            conn.execute(
                "UPDATE external_sources SET fail_count = fail_count + 1, "
                "next_run_at = ?, last_poll_at = ?, last_error = ?, updated_at = ? "
                "WHERE source_id = ?",
                (next_run_at, now, error[:2000], now, source_id),
            )

    def delete(self, source_id: str) -> bool:
        with self._db.transaction() as conn:
            cur = conn.execute("DELETE FROM external_sources WHERE source_id = ?", (source_id,))
        return bool(cur.rowcount)
