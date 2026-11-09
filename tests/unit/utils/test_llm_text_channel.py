"""Tests for channel-facing thinking tag formatting."""

from __future__ import annotations

from octop.infra.utils.llm_text import prepare_channel_text, strip_thinking


def test_prepare_channel_text_strips_when_hidden() -> None:
    raw = "<think>secret</think>\n可见回复"
    assert prepare_channel_text(raw, show_thinking=False) == "可见回复"
    assert strip_thinking(raw) == "可见回复"


def test_prepare_channel_text_formats_when_shown() -> None:
    raw = "<think>secret</think>\n可见回复"
    assert prepare_channel_text(raw, show_thinking=True) == ("💭 Thinking: secret\n\n可见回复")


def test_prepare_channel_text_handles_thinking_alias() -> None:
    raw = "<thinking>plan</thinking>答案"
    assert prepare_channel_text(raw, show_thinking=False) == "答案"


def test_prepare_channel_text_drops_empty_when_only_thinking() -> None:
    assert prepare_channel_text("<think>only</think>", show_thinking=False) == ""


def test_format_channel_text_strips_markdown_for_weixin() -> None:
    from octop.infra.utils.llm_text import format_channel_text

    raw = (
        "## 读书记录台\n\n"
        "**公开链接**\n\n"
        "| 检查项 | 结果 |\n|---|---|\n| 公网 | 200 |\n\n"
        "[打开](https://example.com/p/token)"
    )
    out = format_channel_text(raw, "weixin")

    assert "##" not in out
    assert "**" not in out
    assert "|---" not in out
    assert "读书记录台" in out
    assert "公开链接" in out
    assert "检查项 · 结果" in out
    assert "打开 (https://example.com/p/token)" in out


def test_format_channel_text_preserves_markdown_for_feishu() -> None:
    from octop.infra.utils.llm_text import format_channel_text

    raw = "## 标题\n\n**粗体**"
    assert format_channel_text(raw, "feishu") == raw


def test_strip_internal_payloads_removes_leading_retention_json() -> None:
    from octop.infra.utils.llm_text import strip_internal_payloads

    raw = (
        '{"matches":[{"atom_id":"e2733ca0","relation":"unrelated",'
        '"document":"USER.md","quote":"对外谨慎","confidence":"high",'
        '"same_subject":false,"same_scope":false}]}'
        "读书记录台入口\nhttp://example.test/p/token"
    )
    assert strip_internal_payloads(raw) == "读书记录台入口\nhttp://example.test/p/token"


def test_strip_internal_payloads_keeps_other_json() -> None:
    from octop.infra.utils.llm_text import strip_internal_payloads

    raw = '{"apps":[{"app_id":"app_1"}]}\n结果'
    assert strip_internal_payloads(raw) == raw
    assert strip_internal_payloads('说明 {"matches":[]} 结束') == '说明 {"matches":[]} 结束'


def test_format_channel_text_strips_retention_json_for_weixin() -> None:
    from octop.infra.utils.llm_text import format_channel_text

    raw = '{"matches":[{"atom_id":"a","relation":"unrelated"}]}答案'
    assert format_channel_text(raw, "weixin") == "答案"
