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

import json
import logging
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from typing import Annotated, Any, Protocol

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import StructuredTool
from pydantic import Field

from octop.i18n import tr
from octop.infra.apps.models import MAIN_TABLE_KEY
from octop.infra.apps.service import AppRecord, AppService
from octop.infra.db.repos.apps import (
    ASSISTANT_ROLE_ASSISTANT,
    ASSISTANT_ROLE_USER,
    LightAppRepo,
)
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.utils.llm_text import llm_text_content
from octop.infra.utils.locale import Locale

logger = logging.getLogger(__name__)

MAX_ASSISTANT_ROUNDS = 4
"""Model rounds (tool-call loops) allowed per question."""

ASSISTANT_CONTEXT_MESSAGES = 12
"""Persisted history messages replayed as context."""

MAX_QUESTION_LEN = 4000
MAX_ANSWER_LEN = 8000


class AssistantModelSource(Protocol):
    """The slice of the live harness the assistant needs."""

    def get_agent(self, agent_id: str) -> Any: ...


@dataclass(frozen=True)
class AssistantTurn:
    """One persisted assistant message."""

    role: str
    content: str
    created_at: int


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
        agents: AssistantModelSource,
        usage_repo: Any | None = None,
    ) -> None:
        self._service = service
        self._app_repo = app_repo
        self._agents = agents
        self._usage_repo = usage_repo

    # ----- history -----

    def history(self, row: AppRecord, user_id: int, *, limit: int = 50) -> list[AssistantTurn]:
        messages = self._app_repo.list_assistant_messages(row.app_id, user_id, limit=limit)
        return [
            AssistantTurn(role=item.role, content=item.content, created_at=item.created_at)
            for item in messages
        ]

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
        agent = self._agents.get_agent(row.agent_id)
        try:
            model_ref = agent.config.pick_default_model_ref()
            # The factory returns a ModelAccess wrapper in production; tests
            # stub a plain chat model. The bound loop needs the raw model.
            access = agent.model_factory.get(model_ref)
            llm = getattr(access, "chat_model", access)
        except Exception as exc:
            raise OctopError(ErrorCode.INTERNAL_ERROR, f"no usable model: {exc}") from exc
        return PreparedAsk(
            row=row,
            user_id=user_id,
            question=clean_question,
            locale=locale,
            llm=llm,
            model_ref=model_ref,
        )

    async def stream(self, prepared: PreparedAsk) -> AsyncIterator[dict[str, Any]]:
        """Yield ``token`` / ``tool`` / ``done`` / ``error`` events."""
        row, user_id, question = prepared.row, prepared.user_id, prepared.question
        try:
            messages = await self._messages_for(row, user_id, question, prepared.locale)
            bound = prepared.llm.bind_tools(self._build_tools(row))

            usage_in = usage_out = 0
            answer = ""
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
                    }
                    break
                if text:
                    yield {"type": "token", "text": text}
                messages.append(result)
                for call in tool_calls:
                    yield {"type": "tool", "name": str(call.get("name") or "")}
                    tool_result = await self._run_tool(row, call)
                    messages.append(
                        ToolMessage(content=tool_result, tool_call_id=str(call.get("id") or ""))
                    )
            else:
                # The model kept calling tools; stop instead of looping on.
                yield {
                    "type": "error",
                    "message": tr("light_apps.assistant_round_limit", prepared.locale),
                }
                return
            if answer:
                self._record(
                    row,
                    user_id,
                    question,
                    answer,
                    prepared.model_ref,
                    input_tokens=usage_in,
                    output_tokens=usage_out,
                )
        except OctopError as exc:
            yield {"type": "error", "message": exc.localized_message(prepared.locale)}
        except Exception:
            logger.exception("app assistant failed app=%s", row.app_id)
            yield {"type": "error", "message": tr("light_apps.assistant_failed", prepared.locale)}

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
        for item in self._app_repo.list_assistant_messages(
            row.app_id, user_id, limit=ASSISTANT_CONTEXT_MESSAGES
        ):
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

    def _build_tools(self, row: AppRecord) -> list[StructuredTool]:
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
            schema, rows, total = await service.query_rows(
                row,
                table=table,
                filters=filters,
                limit=limit,
                offset=offset,
            )
            return json.dumps(
                {
                    "table": table,
                    "columns": schema.to_dict()["columns"],
                    "rows": rows,
                    "total": total,
                    "limit": limit,
                    "offset": offset,
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
        ]

    async def _run_tool(self, row: AppRecord, call: Mapping[str, Any]) -> str:
        tool_map = {tool.name: tool for tool in self._build_tools(row)}
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
    ) -> None:
        self._app_repo.add_assistant_message(
            row.app_id, user_id, role=ASSISTANT_ROLE_USER, content=question
        )
        self._app_repo.add_assistant_message(
            row.app_id, user_id, role=ASSISTANT_ROLE_ASSISTANT, content=answer
        )
        if self._usage_repo is None:
            return
        try:
            self._usage_repo.record(
                agent_id=row.agent_id,
                user_id=user_id,
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
