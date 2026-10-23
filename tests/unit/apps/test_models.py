"""Unit tests for light-app schema, row and CSV helpers."""

from __future__ import annotations

import pytest

from octop.infra.apps.models import (
    MAX_HTML_BYTES,
    extend_schema,
    parse_csv,
    parse_schema_payload,
    render_csv,
    validate_app_id,
    validate_html,
    validate_name,
    validate_permission,
    validate_row,
)
from octop.infra.errors import ErrorCode, OctopError

COLUMNS: list[dict[str, object]] = [
    {"name": "title", "type": "string", "required": True},
    {"name": "qty", "type": "integer"},
    {"name": "price", "type": "number"},
    {"name": "done", "type": "boolean"},
    {"name": "due", "type": "date"},
    {"name": "status", "type": "enum", "values": ["new", "doing", "done"]},
]


def _assert_code(exc: pytest.ExceptionInfo[OctopError], code: ErrorCode) -> None:
    assert exc.value.code == code
    assert str(exc.value)


def test_parse_schema_accepts_list_dict_and_json_string() -> None:
    from_list = parse_schema_payload(COLUMNS)
    assert from_list.names == ("title", "qty", "price", "done", "due", "status")
    assert from_list.version == 1
    assert from_list.column("title") is not None
    assert from_list.column("title").label == "title"
    assert from_list.column("status").values == ("new", "doing", "done")

    from_dict = parse_schema_payload({"version": 3, "columns": COLUMNS})
    assert from_dict.version == 3

    from_json = parse_schema_payload('{"columns": [{"name": "note", "type": "text"}]}')
    assert from_json.names == ("note",)
    assert from_json.column("note").type == "text"


@pytest.mark.parametrize(
    "payload",
    [
        [],
        [{"name": "1bad"}],
        [{"name": "ok", "type": "blob"}],
        [{"name": "ok", "values": ["x"]}],
        [{"name": "ok", "type": "enum"}],
        [{"name": "ok", "type": "enum", "values": ["x", "x"]}],
        [{"name": "dup"}, {"name": "dup"}],
        [{"name": "ok", "extra": 1}],
        {"version": 0, "columns": [{"name": "ok"}]},
        {"columns": [{"name": "ok"}], "extra": True},
        "not json",
        "{}",
    ],
)
def test_parse_schema_rejects_invalid_payload(payload: object) -> None:
    with pytest.raises(OctopError) as exc:
        parse_schema_payload(payload)
    _assert_code(exc, ErrorCode.APP_SCHEMA_INVALID)


def test_extend_schema_is_append_only_and_bumps_version() -> None:
    base = parse_schema_payload([{"name": "title"}])
    extended = extend_schema(base, [{"name": "notes", "type": "text"}])
    assert extended.names == ("title", "notes")
    assert extended.version == 2
    assert base.names == ("title",)

    with pytest.raises(OctopError) as exc:
        extend_schema(extended, [{"name": "title"}])
    _assert_code(exc, ErrorCode.APP_SCHEMA_INVALID)


def test_validate_row_coerces_types() -> None:
    schema = parse_schema_payload(COLUMNS)
    row = validate_row(
        schema,
        {
            "title": "  Buy milk  ",
            "qty": "3",
            "price": 2.5,
            "done": "yes",
            "due": "2026-10-22",
            "status": "doing",
        },
    )
    assert row == {
        "title": "Buy milk",
        "qty": "3",
        "price": "2.5",
        "done": "true",
        "due": "2026-10-22",
        "status": "doing",
    }
    assert validate_row(schema, {"title": "x"})["done"] == ""


@pytest.mark.parametrize(
    "values",
    [
        {"title": "x", "mystery": "1"},
        {"title": "x", "qty": "3.5"},
        {"title": "x", "price": "NaN"},
        {"title": "x", "done": "maybe"},
        {"title": "x", "due": "2026-13-01"},
        {"title": "x", "status": "archived"},
        {"title": ""},
        {"qty": "1"},
        {"title": ["list"]},
    ],
)
def test_validate_row_rejects_invalid_values(values: dict[str, object]) -> None:
    schema = parse_schema_payload(COLUMNS)
    with pytest.raises(OctopError) as exc:
        validate_row(schema, values)
    _assert_code(exc, ErrorCode.APP_ROW_INVALID)


def test_csv_round_trip_preserves_quotes() -> None:
    schema = parse_schema_payload(COLUMNS)
    rows = [
        validate_row(
            schema,
            {
                "title": 'Say "hi", then\nleave',
                "qty": "1",
                "done": "false",
                "status": "new",
            },
        ),
        validate_row(schema, {"title": "plain"}),
    ]
    text = render_csv(schema, rows)
    assert text.splitlines()[0].startswith("title,qty,price,done,due,status")
    assert parse_csv(text, schema) == rows
    assert parse_csv("", schema) == []


def test_parse_csv_rejects_header_mismatch() -> None:
    schema = parse_schema_payload([{"name": "title"}])
    with pytest.raises(OctopError) as exc:
        parse_csv("other\na\n", schema)
    _assert_code(exc, ErrorCode.APP_SCHEMA_INVALID)


def test_validate_html() -> None:
    assert validate_html("  <h1>Hi</h1>  ") == "<h1>Hi</h1>"
    with pytest.raises(OctopError) as exc:
        validate_html("   ")
    _assert_code(exc, ErrorCode.APP_HTML_INVALID)
    with pytest.raises(OctopError) as exc:
        validate_html("plain text")
    _assert_code(exc, ErrorCode.APP_HTML_INVALID)
    with pytest.raises(OctopError) as exc:
        validate_html("<p>\x00</p>")
    _assert_code(exc, ErrorCode.APP_HTML_INVALID)
    with pytest.raises(OctopError) as exc:
        validate_html("<p>" + "x" * MAX_HTML_BYTES + "</p>")
    _assert_code(exc, ErrorCode.APP_HTML_INVALID)


def test_validate_permission() -> None:
    assert validate_permission("view") == "view"
    assert validate_permission("edit_data") == "edit_data"
    with pytest.raises(OctopError) as exc:
        validate_permission("edit_structure")
    _assert_code(exc, ErrorCode.APP_SHARE_INVALID)


def test_validate_app_id() -> None:
    assert validate_app_id(" app_ABCdef123 ") == "app_ABCdef123"
    with pytest.raises(OctopError) as exc:
        validate_app_id("abc")
    _assert_code(exc, ErrorCode.APP_NOT_FOUND)


def test_validate_name() -> None:
    assert validate_name("  Tracker  ") == "Tracker"
    with pytest.raises(OctopError) as exc:
        validate_name("   ")
    _assert_code(exc, ErrorCode.APP_NAME_INVALID)
