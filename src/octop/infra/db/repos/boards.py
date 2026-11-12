"""Personal workbench boards — SQL only.

One row per named layout. The app ids, positions and sizes live inside
``layout_json``; access control and layout validation belong to the domain
service.
"""

from __future__ import annotations

from dataclasses import dataclass

from octop.infra.db.pool import DatabasePool
from octop.infra.db.repos._base import DbRow, map_rows, now_ts, partial_updates
from octop.infra.utils.ulid import new_short_id

EMPTY_BOARD_LAYOUT = '{"version":1,"items":[]}'


@dataclass(frozen=True)
class LightAppBoardRow:
    board_id: str
    pk: int
    owner_user_id: int
    name: str
    description: str
    layout_json: str
    version: int
    created_at: int
    updated_at: int

    @classmethod
    def from_row(cls, r: DbRow) -> LightAppBoardRow:
        return cls(
            board_id=str(r["board_id"]),
            pk=int(r["id"]),
            owner_user_id=int(r["owner_user_id"]),
            name=str(r["name"]),
            description=str(r["description"] or ""),
            layout_json=str(r["layout_json"] or EMPTY_BOARD_LAYOUT),
            version=int(r["version"] or 1),
            created_at=int(r["created_at"]),
            updated_at=int(r["updated_at"]),
        )


class LightAppBoardRepo:
    def __init__(self, db: DatabasePool) -> None:
        self._db = db

    def allocate_id(self) -> str:
        for _ in range(16):
            board_id = f"board_{new_short_id(10)}"
            if self.get(board_id) is None:
                return board_id
        raise RuntimeError("failed to allocate unique board id")

    def create(
        self,
        *,
        owner_user_id: int,
        name: str,
        description: str = "",
        board_id: str | None = None,
        layout_json: str = EMPTY_BOARD_LAYOUT,
    ) -> LightAppBoardRow:
        board_id = board_id or self.allocate_id()
        ts = now_ts()
        with self._db.transaction() as conn:
            conn.execute(
                "INSERT INTO miniapp_boards("
                "board_id, owner_user_id, name, description, layout_json, "
                "version, created_at, updated_at"
                ") VALUES (?, ?, ?, ?, ?, 1, ?, ?)",
                (board_id, owner_user_id, name, description, layout_json, ts, ts),
            )
        row = self.get(board_id)
        if row is None:
            raise RuntimeError(f"board insert failed: {board_id}")
        return row

    def get(self, board_id: str) -> LightAppBoardRow | None:
        with self._db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM miniapp_boards WHERE board_id = ?",
                (board_id,),
            ).fetchone()
        return LightAppBoardRow.from_row(row) if row else None

    def list_for_owner(self, owner_user_id: int) -> list[LightAppBoardRow]:
        with self._db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM miniapp_boards WHERE owner_user_id = ? ORDER BY updated_at DESC",
                (owner_user_id,),
            ).fetchall()
        return map_rows(rows, LightAppBoardRow)

    def update_metadata(
        self,
        board_id: str,
        *,
        name: str | None = None,
        description: str | None = None,
    ) -> None:
        fields, params = partial_updates([("name", name), ("description", description)])
        if not fields:
            return
        fields.append("updated_at = ?")
        params.append(now_ts())
        params.append(board_id)
        with self._db.transaction() as conn:
            conn.execute(
                f"UPDATE miniapp_boards SET {', '.join(fields)} WHERE board_id = ?",
                params,
            )

    def set_layout(self, board_id: str, layout_json: str, expected_version: int) -> bool:
        """CAS write; false means another tab saved a newer layout."""
        with self._db.transaction() as conn:
            cursor = conn.execute(
                "UPDATE miniapp_boards SET layout_json = ?, version = version + 1, "
                "updated_at = ? WHERE board_id = ? AND version = ?",
                (layout_json, now_ts(), board_id, expected_version),
            )
        return int(cursor.rowcount or 0) > 0

    def delete(self, board_id: str) -> None:
        with self._db.transaction() as conn:
            conn.execute("DELETE FROM miniapp_boards WHERE board_id = ?", (board_id,))
