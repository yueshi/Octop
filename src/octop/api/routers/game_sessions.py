"""External MiniApp game result ingress (token-authenticated)."""

from __future__ import annotations

import json
from typing import Any, Literal

import jwt
from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from octop.api.deps import get_server
from octop.infra.apps.game_sessions import decode_game_token
from octop.infra.apps.models import MAIN_TABLE_KEY
from octop.infra.apps.service import AppService
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.server import OctopServer

router = APIRouter(prefix="/game-sessions")

MAX_GAME_OPERATIONS = 10
MAX_GAME_ROWS = 1000


class GameOperation(BaseModel):
    table: str = Field(default=MAIN_TABLE_KEY, description="Target app table.")
    mode: Literal["append", "upsert"] = Field(
        default="append", description="append adds rows; upsert updates by key."
    )
    rows: list[dict[str, Any]] = Field(default_factory=list, description="Rows to write.")
    upsert_key: list[str] | None = Field(default=None, description="Key columns for upsert mode.")


class GameResultBody(BaseModel):
    session_id: str = Field(description="Session id returned when the game launched.")
    operations: list[GameOperation] = Field(default_factory=list)


class GameResultOut(BaseModel):
    session_id: str
    imported: int = 0
    updated: int = 0
    tables: dict[str, int] = Field(default_factory=dict)


def _service(server: OctopServer) -> AppService:
    if server.services is None or server.app_runtime is None:
        raise OctopError(ErrorCode.INTERNAL_ERROR, "server is not started")
    return AppService(
        app_repo=server.services.light_app_repo,
        agent_repo=server.services.agent_repo,
        user_repo=server.services.user_repo,
        workspaces=server.app_runtime.agent_registry,
        secret_repo=server.services.secret_repo,
    )


def _string_row(row: dict[str, Any]) -> dict[str, str]:
    return {str(key): "" if value is None else str(value) for key, value in row.items()}


@router.post(
    "/{token}/results",
    response_model=GameResultOut,
    summary="Write external game results into a MiniApp",
    description=(
        "Token-authenticated endpoint for an external game. The token is scoped "
        "to one app/user/game session and expires quickly. Appends use a "
        "session-scoped client request id so retries do not duplicate rows."
    ),
)
async def write_game_results(
    token: str,
    body: GameResultBody,
    server: OctopServer = Depends(get_server),
) -> GameResultOut:
    if server.services is None:
        raise OctopError(ErrorCode.INTERNAL_ERROR, "server is not started")
    secret = server.services.secret_repo.get("jwt")
    if secret is None:
        raise OctopError(ErrorCode.INTERNAL_ERROR, "jwt secret missing")
    try:
        payload = decode_game_token(secret, token)
    except jwt.ExpiredSignatureError as exc:
        raise OctopError(ErrorCode.TOKEN_EXPIRED, "game token expired") from exc
    except (jwt.InvalidTokenError, ValueError) as exc:
        raise OctopError(ErrorCode.AUTH_FAILED, "invalid game token") from exc
    if str(body.session_id) != str(payload["sid"]):
        raise OctopError(ErrorCode.AUTH_FAILED, "game session mismatch")
    if len(body.operations) > MAX_GAME_OPERATIONS:
        raise OctopError(
            ErrorCode.APP_LIMIT_EXCEEDED,
            f"at most {MAX_GAME_OPERATIONS} operations per result",
        )

    service = _service(server)
    app_id = str(payload["app_id"])
    user_id = int(payload["user_id"])
    try:
        app = service.get(app_id)
    except OctopError as exc:
        raise OctopError(ErrorCode.APP_NOT_FOUND, "app not found") from exc
    # Re-check at write time: access may have been revoked since launch.
    service.require(
        app,
        user_id,
        "edit_data",
        is_admin=bool(payload.get("admin")),
    )

    imported = 0
    updated = 0
    tables: dict[str, int] = {}
    for op_index, operation in enumerate(body.operations):
        if len(operation.rows) > MAX_GAME_ROWS:
            raise OctopError(
                ErrorCode.APP_LIMIT_EXCEEDED,
                f"at most {MAX_GAME_ROWS} rows per operation",
            )
        if operation.mode == "append":
            for row_index, raw in enumerate(operation.rows):
                try:
                    await service.append_row(
                        app,
                        raw,
                        table=operation.table,
                        client_request_id=(f"{payload['sid']}:{op_index}:{row_index}"),
                        source="agent",
                        created_by=user_id,
                    )
                except OctopError as exc:
                    raise OctopError(
                        ErrorCode.APP_ROW_INVALID,
                        f"{operation.table} row {row_index + 1}: {exc.message}",
                    ) from exc
                imported += 1
                tables[operation.table] = tables.get(operation.table, 0) + 1
        else:
            upsert_key = operation.upsert_key or []
            if not upsert_key:
                raise OctopError(
                    ErrorCode.APP_ROW_INVALID,
                    "upsert requires upsert_key",
                )
            for row_index, raw in enumerate(operation.rows):
                values = _string_row(raw)
                key_values = {name: values.get(name, "") for name in upsert_key}
                try:
                    existing = (
                        None
                        if any(value == "" for value in key_values.values())
                        else await service.find_row_by_values(
                            app,
                            key_values,
                            table=operation.table,
                        )
                    )
                    if existing is None:
                        await service.append_row(
                            app,
                            values,
                            table=operation.table,
                            client_request_id=(f"{payload['sid']}:{op_index}:{row_index}"),
                            source="agent",
                            created_by=user_id,
                        )
                        imported += 1
                    else:
                        await service.update_row(
                            app,
                            existing.row_id,
                            values,
                            table=operation.table,
                        )
                        updated += 1
                except OctopError as exc:
                    raise OctopError(
                        ErrorCode.APP_ROW_INVALID,
                        f"{operation.table} row {row_index + 1}: {exc.message}",
                    ) from exc
                tables[operation.table] = tables.get(operation.table, 0) + 1

    server.services.audit_repo.write(
        actor=f"game:{payload['game_id']}",
        action="miniapp.game_result",
        target=app_id,
        payload=json.dumps(
            {"session_id": payload["sid"], "tables": tables},
            ensure_ascii=False,
        ),
    )
    return GameResultOut(
        session_id=str(payload["sid"]),
        imported=imported,
        updated=updated,
        tables=tables,
    )
