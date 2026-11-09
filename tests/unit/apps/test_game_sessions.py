"""Unit tests for external game session tokens."""

from __future__ import annotations

import time

import jwt
import pytest

from octop.infra.apps.game_sessions import (
    DEFAULT_GAME_TOKEN_TTL,
    GAME_TOKEN_TYPE,
    create_game_token,
    decode_game_token,
)

SECRET = b"unit-test-secret-0123456789abcdef"


def _create(**overrides: object) -> tuple[str, int]:
    kwargs: dict[str, object] = {
        "app_id": "app_1",
        "user_id": 7,
        "game_id": "duck",
        "session_id": "game_1",
    }
    kwargs.update(overrides)
    return create_game_token(SECRET, **kwargs)  # type: ignore[arg-type]


def test_roundtrip_carries_scope() -> None:
    token, expires_at = _create(is_admin=True)
    payload = decode_game_token(SECRET, token)
    assert payload["typ"] == GAME_TOKEN_TYPE
    assert payload["sid"] == "game_1"
    assert payload["app_id"] == "app_1"
    assert payload["user_id"] == 7
    assert payload["game_id"] == "duck"
    assert payload["admin"] is True
    assert payload["exp"] == expires_at
    assert expires_at - int(time.time()) <= DEFAULT_GAME_TOKEN_TTL


def test_admin_defaults_to_false() -> None:
    token, _ = _create()
    assert decode_game_token(SECRET, token)["admin"] is False


def test_ttl_has_a_floor(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("octop.infra.apps.game_sessions.time.time", lambda: 1_000_000.0)
    _, expires_at = _create(ttl_seconds=1)
    assert expires_at == 1_000_060


def test_expired_token_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("octop.infra.apps.game_sessions.time.time", lambda: 1_000_000.0)
    token, _ = _create(ttl_seconds=60)
    with pytest.raises(jwt.ExpiredSignatureError):
        decode_game_token(SECRET, token)


def test_wrong_token_type_is_rejected() -> None:
    token = jwt.encode(
        {
            "typ": "access",
            "sid": "game_1",
            "app_id": "app_1",
            "user_id": 7,
            "game_id": "duck",
        },
        SECRET,
        algorithm="HS256",
    )
    with pytest.raises(ValueError, match="not a game token"):
        decode_game_token(SECRET, token)


def test_missing_scope_is_rejected() -> None:
    token = jwt.encode(
        {
            "typ": GAME_TOKEN_TYPE,
            "app_id": "app_1",
            "user_id": 7,
            "game_id": "duck",
        },
        SECRET,
        algorithm="HS256",
    )
    with pytest.raises(ValueError, match="missing sid"):
        decode_game_token(SECRET, token)


def test_foreign_signature_is_rejected() -> None:
    token, _ = _create()
    with pytest.raises(jwt.InvalidSignatureError):
        decode_game_token(b"another-secret-0123456789abcdefgh", token)
