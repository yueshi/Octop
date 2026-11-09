"""Short-lived tokens for external MiniApp games.

The game runs on its own origin and writes results back through a scoped
token, so it never receives the dashboard JWT. Tokens are stateless: the
payload names the app, user, game and session; result writes are idempotent
through client request ids / upsert keys.
"""

from __future__ import annotations

import time
from typing import Any

import jwt

GAME_TOKEN_TYPE = "miniapp_game"
# Long enough for a real play session; result writes are still scoped to one
# app/user/game and die with the token.
DEFAULT_GAME_TOKEN_TTL = 2 * 60 * 60


def create_game_token(
    secret: bytes,
    *,
    app_id: str,
    user_id: int,
    game_id: str,
    session_id: str,
    is_admin: bool = False,
    ttl_seconds: int = DEFAULT_GAME_TOKEN_TTL,
) -> tuple[str, int]:
    now = int(time.time())
    expires_at = now + max(60, int(ttl_seconds))
    token = jwt.encode(
        {
            "typ": GAME_TOKEN_TYPE,
            "sid": session_id,
            "app_id": app_id,
            "user_id": int(user_id),
            "game_id": game_id,
            "admin": bool(is_admin),
            "iat": now,
            "exp": expires_at,
        },
        secret,
        algorithm="HS256",
    )
    return token, expires_at


def decode_game_token(secret: bytes, token: str) -> dict[str, Any]:
    payload = jwt.decode(token, secret, algorithms=["HS256"])
    if payload.get("typ") != GAME_TOKEN_TYPE:
        raise ValueError("not a game token")
    for key in ("sid", "app_id", "user_id", "game_id"):
        if not payload.get(key):
            raise ValueError(f"game token is missing {key}")
    payload["user_id"] = int(payload["user_id"])
    payload["admin"] = bool(payload.get("admin"))
    return payload
