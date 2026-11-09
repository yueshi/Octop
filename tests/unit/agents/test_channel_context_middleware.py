from langchain.agents.middleware import ModelRequest
from langchain_core.messages import SystemMessage
from langchain_openai import ChatOpenAI

from octop.infra.agents.middleware import channel_context


def _request(system: str | None = "base") -> ModelRequest:
    model = ChatOpenAI(model="gpt-5.4", api_key="test")
    message: SystemMessage | None = None if system is None else SystemMessage(content=system)
    return ModelRequest(model=model, messages=[], system_message=message)


def _configure(monkeypatch, source: str) -> None:
    monkeypatch.setattr(
        channel_context,
        "get_config",
        lambda: {"configurable": {"octop_source": source}},
    )


def test_channel_from_source_parses_channel_prefix() -> None:
    assert channel_context.channel_from_source("feishu/01M5") == "feishu"
    assert channel_context.channel_from_source(" Dashboard/octop-dashboard ") == "dashboard"
    assert channel_context.channel_from_source("") == ""


def test_feishu_hint_is_appended_and_forbids_wechat_wording(monkeypatch) -> None:
    _configure(monkeypatch, "feishu/01M5")
    request = _request("base prompt")

    updated = channel_context._with_channel_context(request)

    assert updated is not request
    assert updated.system_message is not None
    text = str(updated.system_message.text)
    assert text.startswith("base prompt\n\n")
    assert "当前对话通道" in text
    assert "飞书" in text
    assert "不要提微信" in text
    assert "历史对话里其他通道的说法不适用于本轮" in text


def test_weixin_hint_requires_plain_text(monkeypatch) -> None:
    _configure(monkeypatch, "weixin/01M5")
    request = _request()

    updated = channel_context._with_channel_context(request)

    assert updated.system_message is not None
    text = str(updated.system_message.text)
    assert "微信只支持纯文本" in text
    assert "链接单独一行" in text


def test_dashboard_and_unknown_sources_are_unchanged(monkeypatch) -> None:
    _configure(monkeypatch, "dashboard/octop-dashboard")
    request = _request()
    assert channel_context._with_channel_context(request) is request

    _configure(monkeypatch, "")
    assert channel_context._with_channel_context(request) is request


def test_missing_system_message_gets_the_hint(monkeypatch) -> None:
    _configure(monkeypatch, "feishu/01M5")
    request = _request(system=None)

    updated = channel_context._with_channel_context(request)

    assert updated.system_message is not None
    assert "当前对话通道" in str(updated.system_message.text)


def test_hint_is_not_duplicated(monkeypatch) -> None:
    _configure(monkeypatch, "feishu/01M5")
    hint = channel_context.channel_context_hint("feishu")
    assert hint is not None
    request = _request(f"base\n\n{hint}")

    assert channel_context._with_channel_context(request) is request


def test_list_content_gets_a_text_block(monkeypatch) -> None:
    _configure(monkeypatch, "telegram/01M5")
    request = ModelRequest(
        model=ChatOpenAI(model="gpt-5.4", api_key="test"),
        messages=[],
        system_message=SystemMessage(content=[{"type": "text", "text": "base"}]),
    )

    updated = channel_context._with_channel_context(request)

    assert updated.system_message is not None
    content = updated.system_message.content
    assert isinstance(content, list)
    assert len(content) == 2
    assert content[0] == {"type": "text", "text": "base"}
    assert channel_context._MARKER in content[1]["text"]
