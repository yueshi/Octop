"""Shared helpers for one-shot LangChain LLM calls.

Used by chat polish, proactive care (memory), and SkillHub expert
manifest generation so text extraction and invoke timing stay consistent.
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

_THINKING_RE = re.compile(
    r"<(?:think|thinking)>[\s\S]*?</(?:think|thinking)>\s*",
    re.IGNORECASE,
)
_THINKING_CAPTURE_RE = re.compile(
    r"<(?:think|thinking)>([\s\S]*?)</(?:think|thinking)>\s*",
    re.IGNORECASE,
)
_THINKING_OPEN_RE = re.compile(r"<(?:think|thinking)>", re.IGNORECASE)
_THINKING_CLOSE_RE = re.compile(r"</(?:think|thinking)>", re.IGNORECASE)

DEFAULT_THINKING_TEMPLATE = "💭 Thinking: {content}"


def strip_thinking(text: str) -> str:
    """Remove tagged or malformed thinking prefixes from model output.

    Some OpenAI-compatible model routers return reasoning in ordinary
    ``content`` and only emit the closing ``</think>`` marker.  Treat a
    closing marker that appears before any opening marker as the boundary
    between the hidden prefix and the user-visible answer.
    """
    cleaned = _THINKING_RE.sub("", text)

    while True:
        close_match = _THINKING_CLOSE_RE.search(cleaned)
        if close_match is None:
            break
        open_match = _THINKING_OPEN_RE.search(cleaned)
        if open_match is not None and open_match.start() < close_match.start():
            break
        cleaned = cleaned[close_match.end() :]

    open_match = _THINKING_OPEN_RE.search(cleaned)
    if open_match is not None:
        cleaned = cleaned[: open_match.start()]
    return cleaned.strip()


_INTERNAL_PAYLOAD_KEYS = frozenset({"matches"})


def _leading_json_end(text: str) -> int:
    """Return the index just past a leading balanced JSON object, or -1."""
    depth = 0
    in_string = False
    escaped = False
    for index, char in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return index + 1
    return -1


def strip_internal_payloads(text: str) -> str:
    """Drop internal JSON envelopes that must never reach an end user.

    Memory retention compares a saved atom with USER.md/MEMORY.md through an
    auxiliary LLM. Its ``{"matches": [...]}`` output can leak into the main
    stream; strip a leading envelope before channel delivery.
    """
    stripped = (text or "").lstrip()
    if not stripped.startswith("{"):
        return text
    end = _leading_json_end(stripped)
    if end < 0:
        return text
    try:
        payload = json.loads(stripped[:end])
    except (ValueError, TypeError):
        return text
    if not isinstance(payload, dict) or not (_INTERNAL_PAYLOAD_KEYS & payload.keys()):
        return text
    return stripped[end:].lstrip()


def prepare_channel_text(
    text: str,
    *,
    show_thinking: bool = False,
    thinking_template: str = DEFAULT_THINKING_TEMPLATE,
) -> str:
    """Format or strip embedded thinking tags for IM channel delivery.

    Mirrors octop-gateway ``BaseChannel._clean_output`` but also handles
    ``<thinking>`` tags and orphan closing markers. Used for both inbound
    reply cleaning and proactive ``push_text`` (team wrap-up, cron, …).
    """
    if not text:
        return ""
    if not show_thinking:
        return strip_internal_payloads(strip_thinking(text))

    def _format_block(match: re.Match[str]) -> str:
        content = match.group(1).strip()
        if not content:
            return ""
        try:
            formatted = thinking_template.format(content=content)
        except (KeyError, IndexError, ValueError):
            formatted = f"{content}"
        return f"{formatted}\n\n"

    formatted = _THINKING_CAPTURE_RE.sub(_format_block, text)
    # Drop malformed leftovers so raw tags never reach the user.
    return strip_internal_payloads(strip_thinking(formatted))


_MD_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s*")
_MD_QUOTE_RE = re.compile(r"^\s{0,3}>\s?")
_MD_TABLE_SEPARATOR_RE = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(?:\|\s*:?-{2,}:?\s*)+\|?\s*$")
_MD_RULE_RE = re.compile(r"^\s*[-*_]{3,}\s*$")
_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")
_PLAIN_TEXT_CHANNELS = frozenset({"weixin", "qq"})


def format_channel_text(text: str, channel_type: str) -> str:
    """Make model Markdown readable on plain-text IM transports.

    WeChat iLink and QQ send text items verbatim, so ``##``/``**``/tables
    otherwise reach the user as raw markup. Other channels keep Markdown.
    """
    text = strip_internal_payloads(text)
    if not text or channel_type.strip().lower() not in _PLAIN_TEXT_CHANNELS:
        return text

    lines: list[str] = []
    in_code = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("```"):
            in_code = not in_code
            continue
        if in_code:
            lines.append(line.rstrip())
            continue
        if _MD_TABLE_SEPARATOR_RE.match(stripped) or _MD_RULE_RE.match(stripped):
            continue
        cleaned = _MD_HEADING_RE.sub("", line)
        cleaned = _MD_QUOTE_RE.sub("", cleaned)
        if cleaned.strip().startswith("|") and cleaned.strip().endswith("|"):
            cells = [cell.strip() for cell in cleaned.strip().strip("|").split("|")]
            cleaned = " · ".join(cell for cell in cells if cell)
        cleaned = _MD_LINK_RE.sub(lambda match: f"{match.group(1)} ({match.group(2)})", cleaned)
        cleaned = cleaned.replace("**", "").replace("__", "").replace("`", "")
        lines.append(cleaned.rstrip())

    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def llm_text_content(result: Any) -> str:
    """Extract plain text from a LangChain ``ainvoke`` result.

    Skips thinking/reasoning content blocks and strips ``<think>`` tags
    so callers get the user-visible response only.
    """
    content = getattr(result, "content", result)
    if isinstance(content, str):
        return strip_thinking(content)
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict):
                block_type = str(block.get("type") or "").lower()
                if block_type in ("thinking", "reasoning"):
                    continue
                if block_type == "text":
                    parts.append(str(block.get("text") or ""))
                else:
                    text = block.get("text") or block.get("content")
                    if text:
                        parts.append(str(text))
            elif isinstance(block, str):
                parts.append(block)
        return strip_thinking("".join(parts))
    return strip_thinking(str(content or ""))


async def ainvoke_text(
    llm: Any,
    messages: list[Any],
    *,
    timeout: float | None = 30.0,
) -> str:
    """Invoke *llm* with *messages* and return stripped plain text.

    When *timeout* is set, wraps ``ainvoke`` in ``asyncio.wait_for``
    (same pattern as chat polish). Pass ``timeout=None`` to wait unbound.
    """
    coro = llm.ainvoke(messages)
    if timeout is None:
        result = await coro
    else:
        result = await asyncio.wait_for(coro, timeout=timeout)
    return llm_text_content(result)
