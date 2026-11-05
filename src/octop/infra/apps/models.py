"""Light app schema, CSV serialization and value validation.

An app owns a ``schema.json`` (column definitions) and a ``data.csv`` whose
header is exactly the column-name list. The dashboard and the agent tools both
go through these helpers so invalid rows never reach the file.
"""

from __future__ import annotations

import csv
import io
import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime

from octop.infra.errors import ErrorCode, OctopError

APP_ID_RE = re.compile(r"^app_[0-9A-Za-z]{6,32}$")
COLUMN_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
TABLE_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")

MAIN_TABLE_KEY = "main"
"""Key of the table every app starts with (``schema.json`` + ``data.csv``)."""

RESERVED_TABLE_KEYS = frozenset({"app", "data", "schema", "manifest", "tables"})
"""Table keys that would collide with an app file name."""

MAX_APPS_PER_OWNER = 50
MAX_TABLES_PER_APP = 12
MAX_COLUMNS = 50
MAX_NAME_LEN = 100
MAX_LABEL_LEN = 100
MAX_DESCRIPTION_LEN = 2000
MAX_ENUM_VALUES = 100
MAX_ENUM_VALUE_LEN = 200
MAX_CELL_LEN = 10_000
MAX_HTML_BYTES = 512 * 1024
MAX_CSV_BYTES = 5 * 1024 * 1024
MAX_ROWS = 20_000
MAX_ROW_READ = 500
MAX_COLLABORATORS = 50

COLUMN_TYPES = frozenset(
    {"string", "text", "integer", "number", "boolean", "date", "datetime", "enum"}
)

PII_KIND_RE = re.compile(r"^[a-z_]{1,40}$")
PII_MASK = "••••"
"""Placeholder shown to viewers and in exports they are allowed to make."""


def redact_rows(
    schema: Schema,
    rows: Sequence[Mapping[str, str]],
) -> list[dict[str, str]]:
    """Mask every column marked with a ``pii`` kind."""
    sensitive = {column.name for column in schema.columns if column.pii}
    if not sensitive:
        return [dict(row) for row in rows]
    return [
        {name: (PII_MASK if name in sensitive else str(value)) for name, value in row.items()}
        for row in rows
    ]


_TRUE_VALUES = frozenset({"true", "1", "yes", "y", "on"})
_FALSE_VALUES = frozenset({"false", "0", "no", "n", "off"})


def _invalid(code: ErrorCode, reason: str, **details: object) -> OctopError:
    return OctopError(code, reason, details={**details})


def validate_app_id(app_id: str) -> str:
    candidate = app_id.strip()
    if not APP_ID_RE.fullmatch(candidate):
        raise _invalid(ErrorCode.APP_NOT_FOUND, f"invalid app id: {app_id!r}")
    return candidate


def validate_name(raw: str) -> str:
    name = raw.strip()
    if not name:
        raise _invalid(ErrorCode.APP_NAME_INVALID, "name is required")
    if len(name) > MAX_NAME_LEN:
        raise _invalid(
            ErrorCode.APP_NAME_INVALID,
            f"name must be at most {MAX_NAME_LEN} characters",
        )
    return name


def validate_description(raw: str) -> str:
    description = raw.strip()
    if len(description) > MAX_DESCRIPTION_LEN:
        raise _invalid(
            ErrorCode.APP_NAME_INVALID,
            f"description must be at most {MAX_DESCRIPTION_LEN} characters",
        )
    return description


def validate_table_key(raw: str) -> str:
    """Validate a table key: snake-ish identifier that is safe as a file name."""
    key = raw.strip()
    if not TABLE_KEY_RE.fullmatch(key):
        raise _invalid(
            ErrorCode.APP_SCHEMA_INVALID,
            f"invalid table key: {raw!r}; use letters, digits and underscores, "
            "starting with a letter or underscore",
        )
    if key.lower() in RESERVED_TABLE_KEYS:
        raise _invalid(
            ErrorCode.APP_SCHEMA_INVALID,
            f"table key is reserved: {key!r}",
        )
    return key


@dataclass(frozen=True)
class Column:
    name: str
    label: str
    type: str = "string"
    required: bool = False
    values: tuple[str, ...] = ()
    pii: str | None = None
    """Sensitive-data kind (id_card, bank_card, phone, address, salary)."""

    def to_dict(self) -> dict[str, object]:
        data: dict[str, object] = {
            "name": self.name,
            "label": self.label,
            "type": self.type,
            "required": self.required,
        }
        if self.values:
            data["values"] = list(self.values)
        if self.pii:
            data["pii"] = self.pii
        return data


@dataclass(frozen=True)
class Schema:
    columns: tuple[Column, ...]
    version: int = 1

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(col.name for col in self.columns)

    def column(self, name: str) -> Column | None:
        for col in self.columns:
            if col.name == name:
                return col
        return None

    def to_dict(self) -> dict[str, object]:
        return {
            "version": self.version,
            "columns": [col.to_dict() for col in self.columns],
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2) + "\n"


def _parse_column(raw: object, *, index: int) -> Column:
    if not isinstance(raw, Mapping):
        raise _invalid(
            ErrorCode.APP_SCHEMA_INVALID,
            f"column #{index + 1} must be an object",
        )
    unknown = set(raw) - {"name", "label", "type", "required", "values", "pii"}
    if unknown:
        raise _invalid(
            ErrorCode.APP_SCHEMA_INVALID,
            f"column #{index + 1} has unknown fields: {', '.join(sorted(map(str, unknown)))}",
        )
    name = str(raw.get("name") or "").strip()
    if not COLUMN_NAME_RE.fullmatch(name):
        raise _invalid(
            ErrorCode.APP_SCHEMA_INVALID,
            f"column #{index + 1} has an invalid name {name!r}; use letters, digits and underscores, starting with a letter or underscore",
        )
    label = str(raw.get("label") or name).strip() or name
    if len(label) > MAX_LABEL_LEN:
        raise _invalid(
            ErrorCode.APP_SCHEMA_INVALID,
            f"column {name!r} label must be at most {MAX_LABEL_LEN} characters",
        )
    col_type = str(raw.get("type") or "string").strip().lower()
    if col_type not in COLUMN_TYPES:
        raise _invalid(
            ErrorCode.APP_SCHEMA_INVALID,
            f"column {name!r} has unsupported type {col_type!r}; use one of {', '.join(sorted(COLUMN_TYPES))}",
        )
    required = raw.get("required", False)
    if not isinstance(required, bool):
        raise _invalid(
            ErrorCode.APP_SCHEMA_INVALID,
            f"column {name!r} required must be true or false",
        )
    values: tuple[str, ...] = ()
    if col_type == "enum":
        raw_values = raw.get("values")
        if not isinstance(raw_values, list) or not raw_values:
            raise _invalid(
                ErrorCode.APP_SCHEMA_INVALID,
                f"enum column {name!r} requires a non-empty values list",
            )
        if len(raw_values) > MAX_ENUM_VALUES:
            raise _invalid(
                ErrorCode.APP_SCHEMA_INVALID,
                f"enum column {name!r} supports at most {MAX_ENUM_VALUES} values",
            )
        cleaned: list[str] = []
        for item in raw_values:
            text = str(item).strip()
            if not text:
                raise _invalid(
                    ErrorCode.APP_SCHEMA_INVALID,
                    f"enum column {name!r} has an empty value",
                )
            if len(text) > MAX_ENUM_VALUE_LEN:
                raise _invalid(
                    ErrorCode.APP_SCHEMA_INVALID,
                    f"enum column {name!r} values must be at most {MAX_ENUM_VALUE_LEN} characters",
                )
            if text in cleaned:
                raise _invalid(
                    ErrorCode.APP_SCHEMA_INVALID,
                    f"enum column {name!r} has a duplicate value {text!r}",
                )
            cleaned.append(text)
        values = tuple(cleaned)
    elif raw.get("values") not in (None, []):
        raise _invalid(
            ErrorCode.APP_SCHEMA_INVALID,
            f"column {name!r} only accepts values for enum columns",
        )
    pii: str | None = None
    raw_pii = raw.get("pii")
    if raw_pii not in (None, ""):
        pii = str(raw_pii).strip()
        if not PII_KIND_RE.fullmatch(pii):
            raise _invalid(
                ErrorCode.APP_SCHEMA_INVALID,
                f"column {name!r} has an invalid pii kind {pii!r}",
            )
    return Column(
        name=name,
        label=label,
        type=col_type,
        required=required,
        values=values,
        pii=pii,
    )


def parse_schema_payload(payload: object) -> Schema:
    """Accept ``schema.json`` text (dict), a bare column list, or a JSON string."""
    data: object = payload
    if isinstance(payload, str):
        try:
            data = json.loads(payload)
        except ValueError as exc:
            raise _invalid(
                ErrorCode.APP_SCHEMA_INVALID, f"schema is not valid JSON: {exc}"
            ) from None
    raw_columns: object
    version = 1
    if isinstance(data, Mapping):
        raw_columns = data.get("columns")
        raw_version = data.get("version", 1)
        if not isinstance(raw_version, int) or raw_version < 1:
            raise _invalid(
                ErrorCode.APP_SCHEMA_INVALID, "schema version must be a positive integer"
            )
        version = raw_version
        unknown = set(data) - {"version", "columns"}
        if unknown:
            raise _invalid(
                ErrorCode.APP_SCHEMA_INVALID,
                f"schema has unknown fields: {', '.join(sorted(map(str, unknown)))}",
            )
    else:
        raw_columns = data
    if not isinstance(raw_columns, list):
        raise _invalid(ErrorCode.APP_SCHEMA_INVALID, "schema must contain a columns list")
    if not raw_columns:
        raise _invalid(ErrorCode.APP_SCHEMA_INVALID, "at least one column is required")
    if len(raw_columns) > MAX_COLUMNS:
        raise _invalid(
            ErrorCode.APP_SCHEMA_INVALID,
            f"at most {MAX_COLUMNS} columns are supported",
        )
    columns = tuple(_parse_column(raw, index=i) for i, raw in enumerate(raw_columns))
    seen: set[str] = set()
    for col in columns:
        if col.name in seen:
            raise _invalid(ErrorCode.APP_SCHEMA_INVALID, f"duplicate column name {col.name!r}")
        seen.add(col.name)
    return Schema(columns=columns, version=version)


def extend_schema(current: Schema, additions: object) -> Schema:
    """Append new columns (append-only; existing columns cannot change type)."""
    extra = parse_schema_payload(additions)
    existing = set(current.names)
    for col in extra.columns:
        if col.name in existing:
            raise _invalid(
                ErrorCode.APP_SCHEMA_INVALID,
                f"column {col.name!r} already exists",
            )
    if len(current.columns) + len(extra.columns) > MAX_COLUMNS:
        raise _invalid(
            ErrorCode.APP_SCHEMA_INVALID,
            f"at most {MAX_COLUMNS} columns are supported",
        )
    return Schema(columns=(*current.columns, *extra.columns), version=current.version + 1)


def _normalize_value(col: Column, raw: object) -> str:
    if raw is None:
        raw_text = ""
    elif isinstance(raw, bool):
        raw_text = "true" if raw else "false"
    elif isinstance(raw, (dict, list, tuple, set)):
        raise _invalid(
            ErrorCode.APP_ROW_INVALID,
            f"column {col.name!r} expects a scalar value",
        )
    else:
        raw_text = str(raw)
    text = raw_text.strip()
    if not text:
        return ""
    if len(text) > MAX_CELL_LEN:
        raise _invalid(
            ErrorCode.APP_ROW_INVALID,
            f"column {col.name!r} exceeds {MAX_CELL_LEN} characters",
        )
    if col.type in {"string", "text"}:
        return text
    if col.type == "integer":
        try:
            value = int(text, 10)
        except ValueError:
            raise _invalid(
                ErrorCode.APP_ROW_INVALID,
                f"column {col.name!r} expects an integer, got {text!r}",
            ) from None
        return str(value)
    if col.type == "number":
        try:
            number = float(text)
        except ValueError:
            raise _invalid(
                ErrorCode.APP_ROW_INVALID,
                f"column {col.name!r} expects a number, got {text!r}",
            ) from None
        if not math.isfinite(number):
            raise _invalid(
                ErrorCode.APP_ROW_INVALID,
                f"column {col.name!r} expects a finite number",
            )
        return repr(number)
    if col.type == "boolean":
        lowered = text.lower()
        if lowered in _TRUE_VALUES:
            return "true"
        if lowered in _FALSE_VALUES:
            return "false"
        raise _invalid(
            ErrorCode.APP_ROW_INVALID,
            f"column {col.name!r} expects true or false, got {text!r}",
        )
    if col.type == "date":
        try:
            date.fromisoformat(text)
        except ValueError:
            raise _invalid(
                ErrorCode.APP_ROW_INVALID,
                f"column {col.name!r} expects a date in YYYY-MM-DD form, got {text!r}",
            ) from None
        return text
    if col.type == "datetime":
        try:
            datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            raise _invalid(
                ErrorCode.APP_ROW_INVALID,
                f"column {col.name!r} expects an ISO-8601 datetime, got {text!r}",
            ) from None
        return text
    if col.type == "enum":
        if text not in col.values:
            raise _invalid(
                ErrorCode.APP_ROW_INVALID,
                f"column {col.name!r} expects one of: {', '.join(col.values)}",
            )
        return text
    raise _invalid(
        ErrorCode.APP_SCHEMA_INVALID,
        f"column {col.name!r} has unsupported type {col.type!r}",
    )


def validate_row(schema: Schema, raw: Mapping[str, object]) -> dict[str, str]:
    known = set(schema.names)
    unknown = set(raw) - known
    if unknown:
        raise _invalid(
            ErrorCode.APP_ROW_INVALID,
            f"unknown columns: {', '.join(sorted(map(str, unknown)))}",
        )
    row: dict[str, str] = {}
    for col in schema.columns:
        value = _normalize_value(col, raw.get(col.name))
        if col.required and not value:
            raise _invalid(
                ErrorCode.APP_ROW_INVALID,
                f"column {col.name!r} is required",
            )
        row[col.name] = value
    return row


def parse_csv(text: str, schema: Schema) -> list[dict[str, str]]:
    if not text.strip():
        return []
    buffer = io.StringIO(text, newline="")
    reader = csv.DictReader(buffer)
    header = reader.fieldnames or []
    if tuple(header) != schema.names:
        raise _invalid(
            ErrorCode.APP_SCHEMA_INVALID,
            "data.csv header does not match schema.json columns",
        )
    rows: list[dict[str, str]] = []
    for raw in reader:
        rows.append({name: str(raw.get(name) or "") for name in schema.names})
    return rows


def render_csv(schema: Schema, rows: Sequence[Mapping[str, str]]) -> str:
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(
        buffer,
        fieldnames=list(schema.names),
        lineterminator="\n",
        extrasaction="ignore",
    )
    writer.writeheader()
    for row in rows:
        writer.writerow({name: str(row.get(name, "")) for name in schema.names})
    return buffer.getvalue()


def validate_html(raw: str) -> str:
    html = raw.strip()
    if not html:
        raise _invalid(ErrorCode.APP_HTML_INVALID, "HTML must not be empty")
    data = html.encode("utf-8")
    if len(data) > MAX_HTML_BYTES:
        raise _invalid(
            ErrorCode.APP_HTML_INVALID,
            f"HTML must be at most {MAX_HTML_BYTES // 1024} KB",
            max_kb=MAX_HTML_BYTES // 1024,
        )
    if "\x00" in html:
        raise _invalid(ErrorCode.APP_HTML_INVALID, "HTML must not contain NUL bytes")
    if not re.search(r"<\s*[a-zA-Z!/]", html):
        raise _invalid(ErrorCode.APP_HTML_INVALID, "HTML must contain at least one tag")
    refs = find_external_refs(html)
    if refs:
        raise _invalid(
            ErrorCode.APP_HTML_INVALID,
            _external_refs_message(refs),
            refs=[{"kind": ref.kind, "url": ref.url, "line": ref.line} for ref in refs],
        )
    return html


def validate_permission(raw: str) -> str:
    permission = raw.strip()
    if permission not in {"view", "edit_data"}:
        raise _invalid(
            ErrorCode.APP_SHARE_INVALID,
            "permission must be 'view' or 'edit_data'",
        )
    return permission


# --- external reference scan -------------------------------------------------
#
# The app page runs in a sandboxed iframe with ``default-src 'none'`` and no
# same-origin access, so any remote resource is blocked by the browser and any
# network call would fail. More importantly, an external script or an image
# beacon would leak the app's rows to a third party. The agent writes the HTML
# from a model that may have read untrusted file content, so the check happens
# at write time instead of leaving a blank page for the user to discover.
#
# ``octopApp.openExternal(url)`` replaces ``<a href="https://…">``; it ships
# with the asset channel in the same P0 release.

_TAG_RE = re.compile(r"<(?P<tag>[a-zA-Z][a-zA-Z0-9-]*)(?P<attrs>(?:[^>\"']|\"[^\"]*\"|'[^']*')*)>")
_ATTR_RE = re.compile(
    r"(?P<name>[a-zA-Z_:][-a-zA-Z0-9_:.]*)\s*=\s*"
    r"(?:\"(?P<dq>[^\"]*)\"|'(?P<sq>[^']*)'|(?P<uq>[^\s\"'=<>`]+))"
)

# Tags whose URL attributes make the browser load or fetch a remote resource.
_REMOTE_TAGS: dict[str, tuple[str, ...]] = {
    "script": ("src",),
    "link": ("href",),
    "img": ("src", "srcset"),
    "iframe": ("src",),
    "frame": ("src",),
    "video": ("src", "poster"),
    "audio": ("src",),
    "source": ("src", "srcset"),
    "track": ("src",),
    "embed": ("src",),
    "object": ("data",),
    "a": ("href",),
}

_REMOTE_KIND_BY_TAG: dict[str, str] = {
    "script": "external script",
    "link": "external stylesheet or link",
    "img": "external image",
    "iframe": "embedded frame",
    "frame": "embedded frame",
    "video": "external media",
    "audio": "external media",
    "source": "external media",
    "track": "external media",
    "embed": "embedded object",
    "object": "embedded object",
    "a": "external link",
}

_NETWORK_APIS: tuple[tuple[str, str], ...] = (
    (r"\bfetch\s*\(", "fetch()"),
    (r"\bXMLHttpRequest\b", "XMLHttpRequest"),
    (r"\bWebSocket\s*\(", "WebSocket()"),
    (r"\bEventSource\s*\(", "EventSource()"),
    (r"\bsendBeacon\s*\(", "sendBeacon()"),
    (r"\bimport\s*\(\s*['\"]https?:", "dynamic import()"),
)

_CSS_IMPORT_RE = re.compile(r"@import\s+(?:url\(\s*)?['\"]?([^'\";)]+)", re.IGNORECASE)
_CSS_URL_RE = re.compile(r"url\(\s*['\"]?([^'\")]+)", re.IGNORECASE)


@dataclass(frozen=True)
class ExternalRef:
    """One remote reference an app page is not allowed to keep."""

    kind: str
    url: str
    line: int


def _is_remote_url(url: str) -> bool:
    text = url.strip()
    if not text:
        return False
    lowered = text.lower()
    return lowered.startswith(("http://", "https://", "//"))


def _is_javascript_url(url: str) -> bool:
    return url.strip().lower().startswith("javascript:")


def _line_of(html: str, offset: int) -> int:
    return html.count("\n", 0, offset) + 1


def _srcset_urls(value: str) -> list[str]:
    urls: list[str] = []
    for candidate in value.split(","):
        url = candidate.strip().split(" ")[0].strip()
        if url:
            urls.append(url)
    return urls


def find_external_refs(html: str) -> list[ExternalRef]:
    """Return every remote reference in *html* that the app sandbox forbids.

    Covers remote loads (``<script src>``, ``<link href>``, images, media,
    frames), CSS ``@import`` / ``url()``, network APIs, ``javascript:`` URLs
    and external ``<a href>`` links.
    """
    refs: list[ExternalRef] = []
    seen: set[tuple[str, str, int]] = set()

    def add(kind: str, url: str, offset: int) -> None:
        key = (kind, url, _line_of(html, offset))
        if key in seen:
            return
        seen.add(key)
        refs.append(ExternalRef(kind=kind, url=url.strip()[:200], line=key[2]))

    for tag_match in _TAG_RE.finditer(html):
        tag = tag_match.group("tag").lower()
        allowed = _REMOTE_TAGS.get(tag)
        if not allowed:
            continue
        kind = _REMOTE_KIND_BY_TAG[tag]
        attrs = tag_match.group("attrs")
        for attr_match in _ATTR_RE.finditer(attrs):
            name = attr_match.group("name").lower()
            if name not in allowed:
                continue
            value = (
                attr_match.group("dq")
                if attr_match.group("dq") is not None
                else attr_match.group("sq")
                if attr_match.group("sq") is not None
                else attr_match.group("uq") or ""
            )
            offset = tag_match.start() + attr_match.start()
            if name == "srcset":
                for url in _srcset_urls(value):
                    if _is_remote_url(url):
                        add(kind, url, offset)
                continue
            if _is_javascript_url(value):
                add("javascript URL", value, offset)
                continue
            if _is_remote_url(value):
                add(kind, value, offset)

    for match in _CSS_IMPORT_RE.finditer(html):
        if _is_remote_url(match.group(1)):
            add("CSS @import", match.group(1), match.start())

    for match in _CSS_URL_RE.finditer(html):
        if _is_remote_url(match.group(1)):
            add("CSS url()", match.group(1), match.start())

    for pattern, label in _NETWORK_APIS:
        for match in re.finditer(pattern, html):
            add(f"network call {label}", match.group(0), match.start())

    refs.sort(key=lambda ref: ref.line)
    return refs


def _external_refs_message(refs: list[ExternalRef]) -> str:
    """Build the agent-facing message: what is wrong and how to fix it."""
    listed = ", ".join(ref.url or ref.kind for ref in refs[:3])
    detail = "; ".join(f"{ref.kind} (line {ref.line})" for ref in refs[:3])
    message = (
        f"HTML must not reference external resources, but {listed} were found: {detail}. "
        "The app page runs in a sandbox with no network access: inline all CSS and JS, "
        "use data: or blob: URLs for images, and call octopApp.openExternal(url) for links."
    )
    if len(refs) > 3:
        message += f" (+{len(refs) - 3} more)"
    return message
