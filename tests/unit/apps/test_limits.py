"""Unit tests for the per-process MiniApp traffic limits."""

from __future__ import annotations

import pytest

from octop.infra.apps import limits
from octop.infra.errors import ErrorCode, OctopError


@pytest.fixture(autouse=True)
def _reset_limits() -> None:
    limits.reset_for_tests()
    yield
    limits.reset_for_tests()


async def test_app_llm_rejects_a_third_concurrent_call() -> None:
    async with limits.app_llm_slot("app_a"), limits.app_llm_slot("app_a"):
        with pytest.raises(OctopError) as exc:
            async with limits.app_llm_slot("app_a"):
                pass
        assert exc.value.code is ErrorCode.APP_BUSY
        assert exc.value.status == 429
        assert exc.value.details["retry_after"] == 60


async def test_app_llm_rejects_over_the_minute_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(limits, "MAX_LLM_PER_MINUTE", 1)
    async with limits.app_llm_slot("app_a"):
        pass
    with pytest.raises(OctopError) as exc:
        async with limits.app_llm_slot("app_a"):
            pass
    assert exc.value.code is ErrorCode.APP_BUSY


async def test_public_slots_are_isolated_per_key() -> None:
    async with limits.public_slot("token_a:1.2.3.4"), limits.public_slot("token_b:1.2.3.4"):
        pass
    async with limits.public_slot("token_a:1.2.3.4"):
        pass


async def test_public_slot_rejects_over_the_minute_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(limits, "PUBLIC_PER_MINUTE", 1)
    async with limits.public_slot("token:ip"):
        pass
    with pytest.raises(OctopError) as exc:
        async with limits.public_slot("token:ip"):
            pass
    assert exc.value.code is ErrorCode.APP_BUSY
