"""external_subscriptions — fan-out rules from a source to a target."""

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
class ExternalSubscriptionRow:
    pk: int
    subscription_id: str
    source_id: str
    target_type: str
    target_id: str
    mode: str
    filter_json: str
    template: str
    mapping_json: str
    coalesce_seconds: int
    max_per_minute: int
    enabled: bool
    created_by: int
    created_at: int
    updated_at: int

    @classmethod
    def from_row(cls, r: DbRow) -> ExternalSubscriptionRow:
        return cls(
            pk=int(r["id"]),
            subscription_id=str(r["subscription_id"]),
            source_id=str(r["source_id"]),
            target_type=str(r["target_type"]),
            target_id=str(r["target_id"]),
            mode=str(r["mode"]),
            filter_json=str(r["filter_json"] or "{}"),
            template=str(r["template"] or ""),
            mapping_json=str(r["mapping_json"] or "{}"),
            coalesce_seconds=int(r["coalesce_seconds"] or 0),
            max_per_minute=int(r["max_per_minute"] or 0),
            enabled=bool(int(r["enabled"] or 0)),
            created_by=int(r["created_by"]),
            created_at=int(r["created_at"]),
            updated_at=int(r["updated_at"]),
        )


class ExternalSubscriptionRepo:
    def __init__(self, db: DatabasePool) -> None:
        self._db = db

    def create(
        self,
        *,
        source_id: str,
        target_type: str,
        target_id: str,
        mode: str,
        created_by: int,
        filter_json: str = "{}",
        template: str = "",
        mapping_json: str = "{}",
        coalesce_seconds: int = 0,
        max_per_minute: int = 0,
    ) -> ExternalSubscriptionRow:
        subscription_id = new_short_id()
        ts = now_ts()
        with self._db.transaction() as conn:
            conn.execute(
                "INSERT INTO external_subscriptions("
                "subscription_id, source_id, target_type, target_id, mode, "
                "filter_json, template, mapping_json, coalesce_seconds, "
                "max_per_minute, enabled, created_by, created_at, updated_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)",
                (
                    subscription_id,
                    source_id,
                    target_type,
                    target_id,
                    mode,
                    filter_json,
                    template,
                    mapping_json,
                    coalesce_seconds,
                    max_per_minute,
                    created_by,
                    ts,
                    ts,
                ),
            )
            row = conn.execute(
                "SELECT * FROM external_subscriptions WHERE subscription_id = ?",
                (subscription_id,),
            ).fetchone()
        if row is None:  # pragma: no cover - insert + select in one transaction
            raise RuntimeError("external subscription insert returned no row")
        return ExternalSubscriptionRow.from_row(row)

    def get(self, subscription_id: str) -> ExternalSubscriptionRow | None:
        with self._db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM external_subscriptions WHERE subscription_id = ?",
                (subscription_id,),
            ).fetchone()
        return ExternalSubscriptionRow.from_row(row) if row is not None else None

    def list_for_source(
        self, source_id: str, *, enabled_only: bool = False
    ) -> list[ExternalSubscriptionRow]:
        sql = "SELECT * FROM external_subscriptions WHERE source_id = ?"
        if enabled_only:
            sql += " AND enabled = 1"
        sql += " ORDER BY id"
        with self._db.connect() as conn:
            rows = conn.execute(sql, (source_id,)).fetchall()
        return map_rows(rows, ExternalSubscriptionRow)

    def list_for_owner(self, owner_user_id: int | None) -> list[ExternalSubscriptionRow]:
        sql = (
            "SELECT s.* FROM external_subscriptions s "
            "JOIN external_sources src ON src.source_id = s.source_id"
        )
        params: tuple[object, ...] = ()
        if owner_user_id is not None:
            sql += " WHERE src.owner_user_id = ?"
            params = (owner_user_id,)
        sql += " ORDER BY s.id"
        with self._db.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return map_rows(rows, ExternalSubscriptionRow)

    def update(
        self,
        subscription_id: str,
        *,
        target_id: object = UNSET,
        mode: object = UNSET,
        filter_json: object = UNSET,
        template: object = UNSET,
        mapping_json: object = UNSET,
        coalesce_seconds: object = UNSET,
        max_per_minute: object = UNSET,
        enabled: object = UNSET,
    ) -> ExternalSubscriptionRow | None:
        pairs = [
            ("target_id", target_id),
            ("mode", mode),
            ("filter_json", filter_json),
            ("template", template),
            ("mapping_json", mapping_json),
            ("coalesce_seconds", coalesce_seconds),
            ("max_per_minute", max_per_minute),
            ("enabled", bool_int(enabled) if isinstance(enabled, bool) else enabled),
        ]
        clauses, params = optional_updates(pairs)
        clauses.append("updated_at = ?")
        params.append(now_ts())
        with self._db.transaction() as conn:
            conn.execute(
                f"UPDATE external_subscriptions SET {', '.join(clauses)} WHERE subscription_id = ?",
                (*params, subscription_id),
            )
            row = conn.execute(
                "SELECT * FROM external_subscriptions WHERE subscription_id = ?",
                (subscription_id,),
            ).fetchone()
        return ExternalSubscriptionRow.from_row(row) if row is not None else None

    def delete(self, subscription_id: str) -> bool:
        with self._db.transaction() as conn:
            cur = conn.execute(
                "DELETE FROM external_subscriptions WHERE subscription_id = ?",
                (subscription_id,),
            )
        return bool(cur.rowcount)
