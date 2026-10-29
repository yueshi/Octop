"""hitl_pending — durable HITL approvals/questions. SQL only.

Mirrors the in-process ``HitlPendingStore``: the store stays the runtime
source of truth, each register/resolve also lands here so a restart can
recover still-open pauses (boot recovery lives in ``store.load_persisted``).
The ephemeral ``pending_id`` rides inside ``payload_json`` — it is a slot
nonce, not a durable key; identity for lookups is (session_key, status).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from octop.infra.db.pool import DatabasePool
from octop.infra.db.repos._base import DbRow, insert_returning_id, map_rows, now_ts

HITL_STATUS_PENDING = "pending"
HITL_TERMINAL_STATUSES = ("approved", "rejected", "expired")


@dataclass(frozen=True)
class HitlPendingRow:
    pk: int
    session_key: str
    thread_id: str | None
    kind: str
    payload: dict[str, Any]
    created_by_agent: str | None
    task_id: str | None
    status: str
    expires_at: int | None
    resolved_by: str | None
    resolved_at: int | None
    resume_token: str | None
    created_at: int

    @classmethod
    def from_row(cls, r: DbRow) -> HitlPendingRow:
        try:
            payload = json.loads(str(r["payload_json"] or "{}"))
        except ValueError:
            payload = {}
        return cls(
            pk=int(r["id"]),
            session_key=str(r["session_key"]),
            thread_id=r["thread_id"],
            kind=str(r["kind"] or "approval"),
            payload=payload if isinstance(payload, dict) else {},
            created_by_agent=r["created_by_agent"],
            task_id=r["task_id"],
            status=str(r["status"] or HITL_STATUS_PENDING),
            expires_at=r["expires_at"],
            resolved_by=r["resolved_by"],
            resolved_at=r["resolved_at"],
            resume_token=r["resume_token"],
            created_at=int(r["created_at"]),
        )


class HitlPendingRepo:
    def __init__(self, db: DatabasePool) -> None:
        self._db = db

    def create(
        self,
        *,
        session_key: str,
        thread_id: str | None,
        kind: str,
        payload: dict[str, Any],
        created_by_agent: str | None = None,
        task_id: str | None = None,
        expires_at: int | None = None,
        resume_token: str | None = None,
    ) -> HitlPendingRow:
        with self._db.transaction() as conn:
            # ``RETURNING id`` binds this row to its own primary key. Reading
            # MAX(id) afterwards races with any concurrent insert and can
            # attach this record to another row's pk — resolving the wrong
            # pause and leaving this one pending forever.
            pk = insert_returning_id(
                conn,
                "INSERT INTO hitl_pending("
                "session_key, thread_id, kind, payload_json, created_by_agent, "
                "task_id, status, expires_at, resume_token, created_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    session_key,
                    thread_id,
                    kind,
                    json.dumps(payload, ensure_ascii=False),
                    created_by_agent,
                    task_id,
                    HITL_STATUS_PENDING,
                    expires_at,
                    resume_token,
                    now_ts(),
                ),
            )
        row = self.get(pk)
        if row is None:
            raise RuntimeError("hitl_pending insert failed")
        return row

    def get(self, pk: int) -> HitlPendingRow | None:
        with self._db.connect() as conn:
            r = conn.execute("SELECT * FROM hitl_pending WHERE id = ?", (pk,)).fetchone()
        return HitlPendingRow.from_row(r) if r else None

    def list_pending(self) -> list[HitlPendingRow]:
        with self._db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM hitl_pending WHERE status = ? ORDER BY created_at, id",
                (HITL_STATUS_PENDING,),
            ).fetchall()
        return map_rows(rows, HitlPendingRow)

    def list_by_session(
        self,
        session_key: str,
        *,
        status: str | None = None,
    ) -> list[HitlPendingRow]:
        sql = "SELECT * FROM hitl_pending WHERE session_key = ?"
        params: list[object] = [session_key]
        if status is not None:
            sql += " AND status = ?"
            params.append(status)
        sql += " ORDER BY created_at DESC, id DESC"
        with self._db.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return map_rows(rows, HitlPendingRow)

    def has_pending_ask(self, session_key: str, *, task_id: str) -> bool:
        with self._db.connect() as conn:
            r = conn.execute(
                "SELECT 1 FROM hitl_pending "
                "WHERE session_key = ? AND task_id = ? AND status = ? LIMIT 1",
                (session_key, task_id, HITL_STATUS_PENDING),
            ).fetchone()
        return r is not None

    def set_task_id(self, pk: int, task_id: str) -> None:
        with self._db.transaction() as conn:
            conn.execute(
                "UPDATE hitl_pending SET task_id = ? WHERE id = ?",
                (task_id, pk),
            )

    def update_payload(self, pk: int, payload: dict[str, Any]) -> None:
        with self._db.transaction() as conn:
            conn.execute(
                "UPDATE hitl_pending SET payload_json = ? WHERE id = ?",
                (json.dumps(payload, ensure_ascii=False), pk),
            )

    def mark_status(
        self,
        pk: int,
        status: str,
        *,
        resolved_by: str | None = None,
    ) -> None:
        resolved_at = now_ts() if status in HITL_TERMINAL_STATUSES else None
        with self._db.transaction() as conn:
            conn.execute(
                "UPDATE hitl_pending SET status = ?, resolved_by = ?, resolved_at = ? WHERE id = ?",
                (status, resolved_by, resolved_at, pk),
            )
