"""Read-only data assistant for one light app.

The assistant is a restricted invocation of the app's owning agent: it answers
questions about the app's own tables using only two read-only tools
(``app_tables`` / ``app_query_rows``) bound to that single app. It never writes
data, never sees other apps, and its permission is exactly the caller's
``view`` permission on the app — enforced by the router before this module
runs.

Each turn is buffered and emitted after the model finishes it, so tool-call
rounds never leak preamble text into the answer and ``<think>`` blocks are
stripped with the same helper the rest of the codebase uses.
"""

from __future__ import annotations

import base64
import json
import logging
import re
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from typing import Annotated, Any

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import StructuredTool
from pydantic import Field

from octop.i18n import tr
from octop.infra.apps.charts import build_chart_spec
from octop.infra.apps.model_access import AppModelSource, resolve_app_model
from octop.infra.apps.models import MAIN_TABLE_KEY, MAX_ROW_READ
from octop.infra.apps.service import AppRecord, AppService
from octop.infra.db.repos.apps import (
    ASSISTANT_ROLE_ASSISTANT,
    ASSISTANT_ROLE_USER,
    LightAppRepo,
)
from octop.infra.errors import OctopError
from octop.infra.users.resource_policy import assert_token_quota_available
from octop.infra.utils.llm_text import llm_text_content
from octop.infra.utils.locale import Locale

logger = logging.getLogger(__name__)

MAX_ASSISTANT_ROUNDS = 8
"""Model rounds allowed per question before a final no-tool answer."""

ASSISTANT_CONTEXT_MESSAGES = 12
"""Persisted history messages replayed as context."""

MAX_QUESTION_LEN = 4000
MAX_ANSWER_LEN = 8000

# Chart specs are render-only metadata. They ride in the answer text behind an
# HTML comment so old databases need no schema migration; history strips them
# before replaying messages to the model or returning them to the dashboard.
_CHARTS_MARKER_RE = re.compile(r"\n\n<!--octop-charts:v1:(.*?)-->", re.DOTALL)
_CHARTS_MARKER_TEMPLATE = "\n\n<!--octop-charts:v1:{payload}-->"


def _split_charts(content: str) -> tuple[str, list[dict[str, Any]]]:
    """Separate visible answer text from its persisted chart specs."""
    match = _CHARTS_MARKER_RE.search(content)
    if match is None:
        return content, []
    try:
        raw = base64.urlsafe_b64decode(match.group(1).encode("ascii")).decode("utf-8")
        parsed = json.loads(raw)
    except (ValueError, UnicodeError):
        parsed = []
    charts = [item for item in parsed if isinstance(item, dict)] if isinstance(parsed, list) else []
    text = (content[: match.start()] + content[match.end() :]).rstrip()
    return text, charts


def _with_charts(answer: str, charts: list[dict[str, Any]]) -> str:
    if not charts:
        return answer
    raw = json.dumps(charts, ensure_ascii=False, separators=(",", ":"))
    payload = base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")
    return answer.rstrip() + _CHARTS_MARKER_TEMPLATE.format(payload=payload)


@dataclass(frozen=True)
class AssistantTurn:
    """One persisted assistant message."""

    role: str
    content: str
    created_at: int
    charts: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True)
class PreparedAsk:
    """Everything ``stream`` needs, resolved while the request can still fail."""

    row: AppRecord
    user_id: int
    question: str
    locale: Locale
    llm: Any
    model_ref: str


class AppAssistant:
    """Answers questions about one app's data with a read-only tool loop."""

    def __init__(
        self,
        *,
        service: AppService,
        app_repo: LightAppRepo,
        agents: AppModelSource,
        usage_repo: Any | None = None,
        policy_repo: Any | None = None,
    ) -> None:
        self._service = service
        self._app_repo = app_repo
        self._agents = agents
        self._usage_repo = usage_repo
        self._policy_repo = policy_repo

    def _check_owner_quota(self, row: AppRecord) -> None:
        """助手同样按创建者计费与限额（会话历史仍按调用者分区）。"""
        if self._policy_repo is None or self._usage_repo is None:
            return
        assert_token_quota_available(self._policy_repo, self._usage_repo, row.owner_user_id)

    # ----- history -----

    def history(self, row: AppRecord, user_id: int, *, limit: int = 50) -> list[AssistantTurn]:
        messages = self._app_repo.list_assistant_messages(row.app_id, user_id, limit=limit)
        turns: list[AssistantTurn] = []
        for item in messages:
            content, charts = _split_charts(item.content)
            turns.append(
                AssistantTurn(
                    role=item.role,
                    content=content,
                    created_at=item.created_at,
                    charts=tuple(charts),
                )
            )
        return turns

    # ----- ask -----

    def prepare(
        self,
        row: AppRecord,
        *,
        user_id: int,
        question: str,
        locale: Locale = "zh",
    ) -> PreparedAsk:
        """Resolve model and validate inputs before streaming starts.

        Raises the owning agent's availability error (AGENT_NOT_RUNNING et al.)
        so the router can fail with a normal HTTP status instead of mid-stream.
        """
        clean_question = question.strip()[:MAX_QUESTION_LEN]
        llm, model_ref = resolve_app_model(self._agents, row.agent_id)
        return PreparedAsk(
            row=row,
            user_id=user_id,
            question=clean_question,
            locale=locale,
            llm=llm,
            model_ref=model_ref,
        )

    async def stream(self, prepared: PreparedAsk) -> AsyncIterator[dict[str, Any]]:
        """Yield ``token`` / ``tool`` / ``chart`` / ``done`` / ``error`` events."""
        row, user_id, question = prepared.row, prepared.user_id, prepared.question
        try:
            self._check_owner_quota(row)
            messages = await self._messages_for(row, user_id, question, prepared.locale)
            charts: list[dict[str, Any]] = []
            bound = prepared.llm.bind_tools(self._build_tools(row, charts, prepared.locale))

            usage_in = usage_out = 0
            answer = ""
            citations: list[dict[str, str]] = []
            for rounds in range(1, MAX_ASSISTANT_ROUNDS + 1):
                # Buffer the whole model turn: tool-call rounds often carry a
                # short preamble that must not leak into the answer or history.
                result = await bound.ainvoke(messages)
                usage = getattr(result, "usage_metadata", None) or {}
                usage_in += int(usage.get("input_tokens") or 0)
                usage_out += int(usage.get("output_tokens") or 0)
                text = llm_text_content(result)[:MAX_ANSWER_LEN]
                tool_calls = list(getattr(result, "tool_calls", None) or [])
                if not tool_calls:
                    answer = text
                    for piece in _slices(text):
                        yield {"type": "token", "text": piece}
                    yield {
                        "type": "done",
                        "rounds": rounds,
                        "input_tokens": usage_in,
                        "output_tokens": usage_out,
                        "citations": citations,
                        "charts": charts,
                    }
                    break
                if text:
                    yield {"type": "token", "text": text}
                messages.append(result)
                for call in tool_calls:
                    tool_name = str(call.get("name") or "")
                    yield {"type": "tool", "name": tool_name}
                    chart_start = len(charts)
                    tool_result = await self._run_tool(row, call, charts, prepared.locale)
                    # Emit each new chart as soon as its tool call completes.
                    for chart in charts[chart_start:]:
                        yield {"type": "chart", "chart": chart}
                    if tool_name == "app_query_rows":
                        try:
                            payload = json.loads(tool_result)
                        except ValueError:
                            payload = {}
                        if isinstance(payload, dict):
                            table = str(payload.get("table") or MAIN_TABLE_KEY)
                            for meta in payload.get("row_meta") or []:
                                if not isinstance(meta, dict):
                                    continue
                                row_id = str(meta.get("row_id") or "")
                                if row_id and all(item["row_id"] != row_id for item in citations):
                                    citations.append({"table": table, "row_id": row_id})
                    messages.append(
                        ToolMessage(content=tool_result, tool_call_id=str(call.get("id") or ""))
                    )
            else:
                # Tool budget exhausted. Ask for a final answer with tools
                # unbound instead of discarding the data/charts already
                # gathered; a hard error was the old, unhelpful behavior.
                answer, final_in, final_out = await self._finalize_answer(
                    prepared.llm, messages, prepared.locale
                )
                usage_in += final_in
                usage_out += final_out
                if not answer and charts:
                    answer = tr(
                        "light_apps.assistant_round_limit_partial",
                        prepared.locale,
                    )
                if answer:
                    for piece in _slices(answer):
                        yield {"type": "token", "text": piece}
                    yield {
                        "type": "done",
                        "rounds": MAX_ASSISTANT_ROUNDS,
                        "input_tokens": usage_in,
                        "output_tokens": usage_out,
                        "citations": citations,
                        "charts": charts,
                    }
                else:
                    yield {
                        "type": "error",
                        "message": tr(
                            "light_apps.assistant_round_limit",
                            prepared.locale,
                        ),
                    }
                    return
            if answer or charts:
                self._record(
                    row,
                    user_id,
                    question,
                    answer,
                    prepared.model_ref,
                    input_tokens=usage_in,
                    output_tokens=usage_out,
                    charts=charts,
                )
        except OctopError as exc:
            yield {"type": "error", "message": exc.localized_message(prepared.locale)}
        except Exception:
            logger.exception("app assistant failed app=%s", row.app_id)
            yield {"type": "error", "message": tr("light_apps.assistant_failed", prepared.locale)}

    async def _finalize_answer(
        self,
        llm: Any,
        messages: list[Any],
        locale: Locale,
    ) -> tuple[str, int, int]:
        """One last model call with tools unbound after the tool budget runs out."""
        try:
            result = await llm.ainvoke(
                [
                    *messages,
                    HumanMessage(content=tr("light_apps.assistant_finalize_prompt", locale)),
                ]
            )
        except Exception:
            logger.warning("app assistant finalization failed", exc_info=True)
            return "", 0, 0
        usage = getattr(result, "usage_metadata", None) or {}
        return (
            llm_text_content(result)[:MAX_ANSWER_LEN],
            int(usage.get("input_tokens") or 0),
            int(usage.get("output_tokens") or 0),
        )

    # ----- pieces -----

    async def _messages_for(
        self,
        row: AppRecord,
        user_id: int,
        question: str,
        locale: Locale,
    ) -> list[Any]:
        tables_view = await self._tables_view(row)
        messages: list[Any] = [SystemMessage(content=self._system_prompt(row, tables_view, locale))]
        for item in self.history(row, user_id, limit=ASSISTANT_CONTEXT_MESSAGES):
            if item.role == ASSISTANT_ROLE_USER:
                messages.append(HumanMessage(content=item.content))
            elif item.role == ASSISTANT_ROLE_ASSISTANT:
                messages.append(AIMessage(content=item.content))
        messages.append(HumanMessage(content=question))
        return messages

    async def _tables_view(self, row: AppRecord) -> list[dict[str, Any]]:
        view: list[dict[str, Any]] = []
        for table in self._service.list_tables(row):
            columns: list[dict[str, Any]] = []
            try:
                schema = await self._service.read_schema(row, table.table_key)
            except OctopError:
                pass
            else:
                columns = [
                    {
                        "name": col.name,
                        "label": col.label,
                        "type": col.type,
                        "required": col.required,
                        "values": list(col.values),
                    }
                    for col in schema.columns
                ]
            view.append(
                {
                    "table": table.table_key,
                    "label": table.label,
                    "row_count": table.row_count,
                    "columns": columns,
                }
            )
        return view

    def _system_prompt(self, row: AppRecord, tables: list[dict[str, Any]], locale: Locale) -> str:
        return tr(
            "light_apps.assistant_system_prompt",
            locale,
            app_name=row.name,
            tables=json.dumps(tables, ensure_ascii=False, indent=1),
        )

    def _build_tools(
        self,
        row: AppRecord,
        charts: list[dict[str, Any]] | None = None,
        locale: Locale = "zh",
    ) -> list[StructuredTool]:
        service = self._service

        async def app_tables() -> str:
            return json.dumps(await self._tables_view(row), ensure_ascii=False)

        async def app_query_rows(
            table: Annotated[str, Field(description="Table key from app_tables.")] = MAIN_TABLE_KEY,
            filters: Annotated[
                dict[str, str] | None,
                Field(description="Optional equality filters keyed by column name."),
            ] = None,
            limit: Annotated[int, Field(description="Rows to return (1-200).")] = 50,
            offset: Annotated[int, Field(description="Rows to skip, for paging.")] = 0,
        ) -> str:
            schema, rows, row_meta, total = await service.query_rows_detailed(
                row,
                table=table,
                filters=filters,
                limit=limit,
                offset=offset,
                redact_pii=True,
            )
            return json.dumps(
                {
                    "table": table,
                    "columns": schema.to_dict()["columns"],
                    "rows": rows,
                    "row_meta": row_meta,
                    "total": total,
                    "limit": limit,
                    "offset": offset,
                },
                ensure_ascii=False,
            )

        async def app_chart(
            x: Annotated[
                str,
                Field(description="Category or date column used for the x axis."),
            ],
            chart_type: Annotated[
                str,
                Field(description=("Chart type: bar, line, pie, area, scatter or stacked_bar.")),
            ] = "bar",
            y: Annotated[
                str | list[str] | None,
                Field(
                    description=(
                        "1-3 numeric columns; a single name or a list. Scatter "
                        "requires exactly one. Omit when agg=count."
                    )
                ),
            ] = None,
            agg: Annotated[
                str,
                Field(
                    description=("Aggregation: sum, avg, count, min or max; ignored by scatter.")
                ),
            ] = "sum",
            filters: Annotated[
                dict[str, str] | None,
                Field(description="Optional equality filters keyed by column name."),
            ] = None,
            title: Annotated[
                str | None,
                Field(description="Optional short chart title."),
            ] = None,
            limit: Annotated[
                int,
                Field(
                    description=(
                        "Maximum data points; 0 = auto (100 for scatter, 20 otherwise). "
                        "Scatter accepts up to 200, other types up to 50."
                    )
                ),
            ] = 0,
            table: Annotated[
                str,
                Field(description="Table key from app_tables."),
            ] = MAIN_TABLE_KEY,
        ) -> str:
            schema, rows, _row_meta, total = await service.query_rows_detailed(
                row,
                table=table,
                filters=filters,
                limit=MAX_ROW_READ,
                offset=0,
                redact_pii=True,
            )
            y_columns = [y] if isinstance(y, str) else y
            spec = build_chart_spec(
                schema,
                rows,
                chart_type=chart_type,
                x=x,
                y=y_columns,
                agg=agg,
                title=title,
                limit=limit,
                locale=locale,
            )
            spec["sampled"] = total > len(rows)
            spec["sample_size"] = len(rows)
            spec["matched_rows"] = total
            if charts is not None and spec not in charts:
                charts.append(spec)
            return json.dumps(
                {
                    "ok": True,
                    "type": spec["type"],
                    "title": spec["title"],
                    "points": len(spec["data"]),
                    "matched_rows": total,
                    "sampled": spec["sampled"],
                    "truncated": spec["truncated"],
                },
                ensure_ascii=False,
            )

        return [
            StructuredTool.from_function(
                coroutine=app_tables,
                name="app_tables",
                description=(
                    "List the tables of this app with their columns, types and row counts. "
                    "Call it first to see what data exists."
                ),
            ),
            StructuredTool.from_function(
                coroutine=app_query_rows,
                name="app_query_rows",
                description=(
                    "Read rows of one app table, optionally filtered by exact column values. "
                    "Values compare case-insensitively; use paging for long tables."
                ),
            ),
            StructuredTool.from_function(
                coroutine=app_chart,
                name="app_chart",
                description=(
                    "Draw a chart from this app's real rows. Use it whenever the user asks "
                    "for a chart, trend, distribution, correlation or composition. The "
                    "backend aggregates the data; never invent series values. Types: bar, "
                    "line, pie, area, scatter (x + exactly one numeric y), stacked_bar "
                    "(2-3 numeric y columns). After the chart is shown, give a one-sentence "
                    "takeaway instead of repeating the raw numbers."
                ),
            ),
        ]

    async def _run_tool(
        self,
        row: AppRecord,
        call: Mapping[str, Any],
        charts: list[dict[str, Any]] | None = None,
        locale: Locale = "zh",
    ) -> str:
        tool_map = {tool.name: tool for tool in self._build_tools(row, charts, locale)}
        name = str(call.get("name") or "")
        tool = tool_map.get(name)
        if tool is None:
            return json.dumps({"error": f"unknown tool: {name}"}, ensure_ascii=False)
        try:
            return str(await tool.ainvoke(dict(call.get("args") or {})))
        except Exception as exc:
            # Feed the failure back to the model; it can retry with fixed args.
            return json.dumps({"error": str(exc)}, ensure_ascii=False)

    def _record(
        self,
        row: AppRecord,
        user_id: int,
        question: str,
        answer: str,
        model_ref: str,
        *,
        input_tokens: int = 0,
        output_tokens: int = 0,
        charts: list[dict[str, Any]] | None = None,
    ) -> None:
        self._app_repo.add_assistant_message(
            row.app_id, user_id, role=ASSISTANT_ROLE_USER, content=question
        )
        self._app_repo.add_assistant_message(
            row.app_id,
            user_id,
            role=ASSISTANT_ROLE_ASSISTANT,
            content=_with_charts(answer, charts or []),
        )
        if self._usage_repo is None:
            return
        try:
            self._usage_repo.record(
                agent_id=row.agent_id,
                # 归属创建者（对话历史仍按调用者分区）
                user_id=row.owner_user_id,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                model=model_ref,
                source="app_assistant",
            )
        except Exception:
            logger.warning("failed to record app assistant usage", exc_info=True)


def _slices(text: str, size: int = 80) -> list[str]:
    """Split a finished answer into chunk-sized pieces for token-style SSE."""
    if not text:
        return []
    if len(text) <= size:
        return [text]
    return [text[i : i + size] for i in range(0, len(text), size)]
