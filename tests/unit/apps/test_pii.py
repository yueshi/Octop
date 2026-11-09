"""Tests for shared MiniApp PII detection and masking."""

from __future__ import annotations

from octop.infra.apps.pii import (
    detect_header_pii,
    detect_text_pii,
    find_unmarked_pii,
    redact_text,
)


def test_header_keywords_mark_contact_and_id_tail() -> None:
    assert detect_header_pii("紧急联系人") == "phone"
    assert detect_header_pii("emergency_contact") == "phone"
    assert detect_header_pii("证件后四位") == "id_card"
    assert detect_header_pii("id_tail") == "id_card"
    assert detect_header_pii("销方税号") == "id_card"
    assert detect_header_pii("备注") is None


def test_detects_phone_inside_free_text() -> None:
    hits = detect_text_pii("李静 13800000002")

    assert [(hit.kind, hit.value) for hit in hits] == [("phone", "13800000002")]


def test_redacts_phone_inside_free_text() -> None:
    assert redact_text("李静 13800000002") == "李静 138****0002"


def test_detects_valid_resident_id_and_email() -> None:
    assert detect_text_pii("身份证 11010519491231002X")[0].kind == "id_card"
    assert redact_text("mail me at alice@example.com") == "mail me at a***@example.com"


def test_detects_bank_card_with_luhn() -> None:
    hits = detect_text_pii("卡号 4111 1111 1111 1111")

    assert [(hit.kind, hit.value) for hit in hits] == [("bank_card", "4111 1111 1111 1111")]
    assert redact_text("卡号 4111 1111 1111 1111") == "卡号 ****1111"


def test_find_unmarked_pii_skips_marked_columns() -> None:
    columns = [
        ("title", None, "string"),
        ("note", None, "text"),
        ("phone", "phone", "string"),
    ]

    hits = find_unmarked_pii(
        columns,
        {"title": "A", "note": "call 13800000002", "phone": "13800000002"},
    )

    assert [(name, hit.kind) for name, hit in hits] == [("note", "phone")]
