"""Shared PII detection and value-level masking for MiniApps.

Column-level ``Column.pii`` metadata is the authoritative classification. The
value-level detector is a safety net for free-text fields: a phone number in a
``note`` or an ``emergency_contact`` cell must not leak just because the column
was never marked.

The harness already ships a conservative detector for provider keys, mainland
mobile numbers and resident IDs (used by ``PIIMiddleware``). Reuse it here so
the app surface and the model surface agree on what counts as sensitive.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from octop_harness.middleware.pii import detect_pii as _detect_harness_pii

PII_KINDS: tuple[str, ...] = (
    "id_card",
    "bank_card",
    "phone",
    "address",
    "salary",
    "email",
    "secret",
)

_HEADER_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "id_card",
        re.compile(
            r"身份证|证件号|证件后四位|证件尾号|护照|税号|纳税人识别号|统一社会信用代码|id_card|id_tail|tax_id|credit_code",
            re.IGNORECASE,
        ),
    ),
    ("bank_card", re.compile(r"银行卡|卡号|账号|账户|bank_card", re.IGNORECASE)),
    (
        "phone",
        re.compile(
            r"手机|电话|联系方式|紧急联系|联系人|emergency_contact|phone|mobile|tel",
            re.IGNORECASE,
        ),
    ),
    ("address", re.compile(r"住址|地址|address", re.IGNORECASE)),
    ("salary", re.compile(r"工资|薪资|薪酬|月薪|收入|salary", re.IGNORECASE)),
    ("email", re.compile(r"邮箱|电子邮件|e-?mail", re.IGNORECASE)),
)

_HARNESS_KIND_MAP: dict[str, str] = {
    "cn_mobile_phone": "phone",
    "cn_resident_id": "id_card",
    "openai": "secret",
    "openai_project": "secret",
    "anthropic": "secret",
    "aws_access_key_id": "secret",
    "google_api_key": "secret",
    "huggingface": "secret",
    "generic_api_key_assignment": "secret",
}

_BANK_CARD_RE = re.compile(r"(?<!\d)(?:\d[ -]?){15,18}\d(?!\d)")
_EMAIL_RE = re.compile(r"(?<![\w.+-])([A-Za-z0-9._%+-]+)@([A-Za-z0-9.-]+\.[A-Za-z]{2,})(?![\w.-])")


@dataclass(frozen=True)
class PIIHit:
    kind: str
    value: str
    start: int
    end: int


def detect_header_pii(header: str) -> str | None:
    """Return the PII kind implied by a column name or label."""
    text = header.strip()
    if not text:
        return None
    for kind, pattern in _HEADER_PATTERNS:
        if pattern.search(text):
            return kind
    return None


def _luhn_digits(value: str) -> bool:
    digits = [int(ch) for ch in value if ch.isdigit()]
    if not 16 <= len(digits) <= 19:
        return False
    checksum = 0
    for index, digit in enumerate(reversed(digits)):
        if index % 2 == 1:
            digit *= 2
            if digit > 9:
                digit -= 9
        checksum += digit
    return checksum % 10 == 0


def _overlaps(start: int, end: int, spans: Iterable[tuple[int, int]]) -> bool:
    return any(start < other_end and end > other_start for other_start, other_end in spans)


def detect_text_pii(text: str) -> list[PIIHit]:
    """Return PII spans inside free text, ordered left to right."""
    if not text:
        return []
    hits: list[PIIHit] = []
    spans: list[tuple[int, int]] = []

    for detected in _detect_harness_pii(text):
        kind = _HARNESS_KIND_MAP.get(str(detected.get("type") or ""))
        if kind is None:
            continue
        start = int(detected["start"])
        end = int(detected["end"])
        value = str(detected["value"])
        hits.append(PIIHit(kind=kind, value=value, start=start, end=end))
        spans.append((start, end))

    for bank_match in _BANK_CARD_RE.finditer(text):
        bank_value = bank_match.group(0)
        digits = re.sub(r"\D", "", bank_value)
        if not _luhn_digits(digits):
            continue
        if _overlaps(bank_match.start(), bank_match.end(), spans):
            continue
        hits.append(
            PIIHit(
                kind="bank_card",
                value=bank_value,
                start=bank_match.start(),
                end=bank_match.end(),
            )
        )
        spans.append((bank_match.start(), bank_match.end()))

    for email_match in _EMAIL_RE.finditer(text):
        if _overlaps(email_match.start(), email_match.end(), spans):
            continue
        hits.append(
            PIIHit(
                kind="email",
                value=email_match.group(0),
                start=email_match.start(),
                end=email_match.end(),
            )
        )
        spans.append((email_match.start(), email_match.end()))

    hits.sort(key=lambda item: item.start)
    return hits


def has_pii(text: str) -> bool:
    return bool(detect_text_pii(text))


def mask_value(value: str, kind: str) -> str:
    """Mask one PII value while keeping enough shape for a human to recognize it."""
    if kind == "phone":
        digits = value.strip()
        if len(digits) > 7:
            return f"{digits[:3]}****{digits[-4:]}"
        return "••••"
    if kind == "id_card":
        compact = value.strip()
        if len(compact) > 10:
            return f"{compact[:6]}********{compact[-4:]}"
        return "••••"
    if kind == "bank_card":
        digits = re.sub(r"\D", "", value)
        return f"****{digits[-4:]}" if len(digits) >= 4 else "••••"
    if kind == "email":
        local, _, domain = value.partition("@")
        if local and domain:
            return f"{local[:1]}***@{domain}"
        return "••••"
    if kind == "secret":
        compact = value.strip()
        if len(compact) > 7:
            return f"{compact[:3]}****{compact[-4:]}"
        return "••••"
    return "••••"


def redact_text(text: str) -> str:
    """Mask every detected PII span in free text."""
    hits = detect_text_pii(text)
    if not hits:
        return text
    result = text
    for hit in reversed(hits):
        result = result[: hit.start] + mask_value(hit.value, hit.kind) + result[hit.end :]
    return result


def find_unmarked_pii(
    columns: Iterable[tuple[str, str | None, str]],
    values: Mapping[str, Any],
) -> list[tuple[str, PIIHit]]:
    """Return ``(column, hit)`` for string cells that contain unmarked PII.

    ``columns`` is ``(name, pii_kind, type)``. Only free-text/string columns are
    scanned; marked columns are already masked by the column policy.
    """
    found: list[tuple[str, PIIHit]] = []
    for name, pii, column_type in columns:
        if pii or column_type not in {"string", "text"}:
            continue
        raw = values.get(name)
        if raw is None:
            continue
        text = str(raw)
        if not text:
            continue
        for hit in detect_text_pii(text):
            found.append((name, hit))
            break
    return found


__all__ = [
    "PIIHit",
    "PII_KINDS",
    "detect_header_pii",
    "detect_text_pii",
    "find_unmarked_pii",
    "has_pii",
    "mask_value",
    "redact_text",
]
