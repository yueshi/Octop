"""Tell the model which chat channel this turn belongs to."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from langchain.agents.middleware import AgentMiddleware, ModelRequest, ModelResponse
from langchain_core.messages import SystemMessage
from langgraph.config import get_config

_MARKER = "<!-- octop-channel-context -->"

_CHANNEL_LABELS: dict[str, str] = {
    "discord": "Discord",
    "dingtalk": "钉钉",
    "feishu": "飞书",
    "qq": "QQ",
    "telegram": "Telegram",
    "wecom": "企业微信",
    "weixin": "微信",
    "yuanbao": "元宝",
    "xiaoyi": "小艺",
}

_CHANNEL_RULES: dict[str, str] = {
    "feishu": (
        "飞书支持 Markdown。按飞书能力回答；不要提微信、公众号或微信限制，"
        "不要说“发到微信”“在微信里点”，也不要讨论链接在其他平台能否打开或转发。"
    ),
    "weixin": (
        "微信只支持纯文本、图片、文件和视频，不支持 Markdown 标题、表格、粗体或卡片。"
        "链接单独一行给完整 URL；不要提飞书、卡片等微信没有的能力。"
    ),
    "wecom": ("企业微信支持纯文本和有限的 Markdown；链接单独一行，不要提个人微信的卡片能力。"),
    "qq": "QQ 支持纯文本、图片和文件；链接单独一行，不要提微信或飞书的能力。",
    "telegram": "Telegram 支持 Markdown；不要提微信或飞书的能力。",
    "discord": "Discord 支持 Markdown；不要提微信或飞书的能力。",
    "dingtalk": "钉钉支持 Markdown；不要提微信或飞书的能力。",
}

_DEFAULT_RULES = "按当前通道支持的能力回答；不要提及其他通道的限制。"


def channel_from_source(source: str) -> str:
    """Return the channel kind from values like ``feishu/01M5...``."""
    cleaned = source.strip()
    if not cleaned:
        return ""
    return cleaned.split("/", 1)[0].lower()


def channel_context_hint(channel: str) -> str | None:
    """Return the per-turn system hint for a channel, or ``None`` if unknown."""
    key = channel.strip().lower()
    label = _CHANNEL_LABELS.get(key)
    if label is None:
        return None
    rules = _CHANNEL_RULES.get(key, _DEFAULT_RULES)
    return (
        f"{_MARKER}\n"
        "## 当前对话通道\n"
        f"这一轮来自：{label}（{key}）。\n"
        f"{rules}\n"
        "历史对话里其他通道的说法不适用于本轮；只按当前通道回答。"
    )


def _content_text(message: SystemMessage) -> str:
    content = message.content
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict) and block.get("type") == "text":
            parts.append(str(block.get("text") or ""))
    return "\n".join(parts)


def _with_channel_context(request: ModelRequest[Any]) -> ModelRequest[Any]:
    configurable = get_config().get("configurable") or {}
    channel = channel_from_source(str(configurable.get("octop_source") or ""))
    hint = channel_context_hint(channel)
    if hint is None:
        return request

    current = request.system_message
    if current is None:
        return request.override(system_message=SystemMessage(content=hint))
    if _MARKER in _content_text(current):
        return request

    content = current.content
    if isinstance(content, str):
        updated: str | list[Any] = f"{content}\n\n{hint}" if content else hint
    elif isinstance(content, list):
        updated = [*content, {"type": "text", "text": f"\n\n{hint}"}]
    else:
        return request

    message = current.model_copy(update={"content": updated})
    return request.override(system_message=message)


class ChannelContextMiddleware(AgentMiddleware[Any, Any]):
    """Append the current channel to the system prompt on every model call."""

    def wrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], ModelResponse[Any]],
    ) -> ModelResponse[Any]:
        return handler(_with_channel_context(request))

    async def awrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], Awaitable[ModelResponse[Any]]],
    ) -> ModelResponse[Any]:
        return await handler(_with_channel_context(request))


__all__ = [
    "ChannelContextMiddleware",
    "channel_context_hint",
    "channel_from_source",
]
