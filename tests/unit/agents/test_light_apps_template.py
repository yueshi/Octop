"""Sanity checks for the bundled light-apps template."""

from __future__ import annotations

import json

from octop.infra.agents.experts.catalog import default_library_root, parse_task_examples

_DIR = default_library_root() / "light-apps"


def test_all_required_files_present() -> None:
    for name in ("SOUL.md", "AGENTS.md", "manifest.json"):
        assert (_DIR / name).is_file(), f"missing {name}"


def test_manifest_declares_id_label_and_prompt_files() -> None:
    meta = json.loads((_DIR / "manifest.json").read_text(encoding="utf-8"))
    assert meta["id"] == "light-apps"
    label = meta.get("label")
    assert isinstance(label, dict)
    assert isinstance(label.get("zh"), str) and label["zh"]
    assert isinstance(label.get("en"), str) and label["en"]
    assert meta.get("prompt_files") == ["SOUL.md", "AGENTS.md"]


def test_manifest_declares_task_examples() -> None:
    meta = json.loads((_DIR / "manifest.json").read_text(encoding="utf-8"))
    parsed = parse_task_examples(meta)
    assert parsed is not None
    assert len(parsed["zh"]) == len(parsed["en"]) == 3


def test_prompt_files_cover_the_hard_rules() -> None:
    agents_md = (_DIR / "AGENTS.md").read_text(encoding="utf-8")
    soul_md = (_DIR / "SOUL.md").read_text(encoding="utf-8")
    for token in (
        "app_create",
        "app_write_html",
        "app_append_row",
        "app_delete",
        "app_inspect_file",
    ):
        assert token in agents_md, token
    assert "octopApp" in agents_md
    assert "listRows" in agents_md
    assert "data:" in agents_md
    assert "blob:" in agents_md
    assert "fetch" in agents_md
    assert "openExternal" in agents_md
    assert "app_create" in soul_md
