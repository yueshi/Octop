"""One-shot text generation for one light app.

Unlike the data assistant, this is a stateless call: no history, no tools and
no access to the app's tables. It exists so app HTML can use the owning
agent's model through ``octopApp.generate(...)`` without ever holding a JWT.
The router enforces the caller's ``view`` permission before this module runs.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from octop.i18n import tr
from octop.infra.apps.model_access import AppModelSource, resolve_app_model
from octop.infra.apps.service import AppRecord
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.users.resource_policy import assert_token_quota_available
from octop.infra.utils.llm_text import llm_text_content
from octop.infra.utils.locale import Locale

logger = logging.getLogger(__name__)

MAX_PROMPT_LEN = 8000
MAX_SYSTEM_LEN = 4000
MAX_OUTPUT_CHARS = 16000
GENERATE_TIMEOUT_SECONDS = 90.0


@dataclass(frozen=True)
class GeneratedText:
    """One generated answer plus its billing metadata."""

    text: str
    model_ref: str
    input_tokens: int
    output_tokens: int


class AppGenerator:
    """Runs one prompt against the app owner's default model.

    Cost attribution: the model, the token usage and the quota gate all belong
    to the **app's owner** (its creator agent / owner user) — never to whoever
    happened to trigger the call. A visitor with read access spends the owner's
    budget, and the owner's quota is the gate that stops it.
    """

    def __init__(
        self,
        *,
        agents: AppModelSource,
        usage_repo: Any | None = None,
        policy_repo: Any | None = None,
    ) -> None:
        self._agents = agents
        self._usage_repo = usage_repo
        self._policy_repo = policy_repo

    def _check_owner_quota(self, row: AppRecord) -> None:
        """Gate on the owner's lifetime token quota, not the caller's."""
        if self._policy_repo is None or self._usage_repo is None:
            return
        assert_token_quota_available(self._policy_repo, self._usage_repo, row.owner_user_id)

    async def generate(
        self,
        row: AppRecord,
        *,
        user_id: int,
        prompt: str,
        system: str = "",
        locale: Locale = "zh",
    ) -> GeneratedText:
        question = prompt.strip()
        if not question:
            raise OctopError(ErrorCode.APP_PROMPT_INVALID, "prompt must not be empty")
        llm, model_ref = resolve_app_model(self._agents, row.agent_id)
        # 额度闸门归属创建者：调用者再多人，也共用创建者的一条额度线。
        self._check_owner_quota(row)
        messages = [
            SystemMessage(content=self._system_prompt(row, system, locale)),
            HumanMessage(content=question[:MAX_PROMPT_LEN]),
        ]
        try:
            result = await asyncio.wait_for(llm.ainvoke(messages), timeout=GENERATE_TIMEOUT_SECONDS)
        except OctopError:
            raise
        except Exception as exc:
            logger.exception("app generation failed app=%s", row.app_id)
            raise OctopError(ErrorCode.APP_GENERATE_FAILED, str(exc)) from exc

        text = llm_text_content(result)[:MAX_OUTPUT_CHARS]
        usage = getattr(result, "usage_metadata", None) or {}
        input_tokens = int(usage.get("input_tokens") or 0)
        output_tokens = int(usage.get("output_tokens") or 0)
        self._record(
            row,
            user_id,
            model_ref,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )
        return GeneratedText(
            text=text,
            model_ref=model_ref,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )

    def _system_prompt(self, row: AppRecord, system: str, locale: Locale) -> str:
        prompt = tr("light_apps.generate_system_prompt", locale, app_name=row.name)
        extra = system.strip()[:MAX_SYSTEM_LEN]
        if extra:
            prompt = f"{prompt}\n\n{extra}"
        return prompt

    def _record(
        self,
        row: AppRecord,
        user_id: int,
        model_ref: str,
        *,
        input_tokens: int,
        output_tokens: int,
    ) -> None:
        if self._usage_repo is None:
            return
        try:
            self._usage_repo.record(
                agent_id=row.agent_id,
                # 归属创建者：谁点的按钮不重要，账记在小程序所有者头上。
                user_id=row.owner_user_id,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                model=model_ref,
                source="app_generate",
            )
        except Exception:
            logger.warning("failed to record app generation usage", exc_info=True)
