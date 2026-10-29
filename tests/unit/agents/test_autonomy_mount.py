"""Digital-employee tool mounting (docs/digital-employee-plan.md S1.3).

An agent with ``autonomy_enabled`` must receive the board tools and the
``ask_creator`` escalation channel — the same runtime a team host gets, on
its own goal/task board.

"Same runtime" means the tool **and** its interrupt travel together (INV-1):
the ``ask_creator`` body always returns ``HEADLESS_FALLBACK``, so the only
thing that turns the call into a real pause is the graph-level
``interrupt_on`` entry.  Mounting the tool without the interrupt makes the
escalation channel silently self-answer.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from octop.config import OctopConfig
from octop.infra.agents.manager import AgentManager
from octop.infra.agents.teams.board_tools import TEAM_BOARD_TOOL_NAMES
from octop.infra.db.migrate import run_migrations
from octop.infra.db.pool import SqlitePool
from octop.infra.db.repos.agents import AgentRepo
from octop.infra.db.repos.users import UserRepo
from octop.infra.db.services import build_shared_services
from octop.infra.utils.paths import PathLayout


@pytest.fixture
def manager(tmp_path: Path) -> AgentManager:
    paths = PathLayout(tmp_path / ".octop")
    paths.ensure_root()
    db = SqlitePool(paths.db)
    run_migrations(db)
    services = build_shared_services(db=db, paths=paths, config=OctopConfig())
    return AgentManager(repos=services.repos, paths=services.paths)


def _tool_names(cfg: Any) -> set[str]:
    names: set[str] = set()
    for tool in getattr(cfg, "tools", None) or []:
        name = getattr(tool, "name", None)
        if isinstance(name, str):
            names.add(name)
    return names


def _interrupt_names(cfg: Any) -> set[str]:
    return set(getattr(cfg, "interrupt_on", None) or {})


def _seed(manager: AgentManager, agent_id: str, *, autonomy: int, kind: str) -> Any:
    user_id = UserRepo(manager._repos.db).create(
        username=f"u-{agent_id}", password_hash="h", role="user"
    )
    AgentRepo(manager._repos.db).create(
        agent_id=agent_id, user_id=user_id, name=agent_id, kind=kind
    )
    with manager._repos.db.transaction() as conn:
        conn.execute(
            "UPDATE agents SET autonomy_enabled = ? WHERE agent_id = ?",
            (autonomy, agent_id),
        )
    return manager._repos.agent_repo.get(agent_id)


def test_autonomous_expert_gets_board_tools_and_ask_creator(manager: AgentManager) -> None:
    row = _seed(manager, "solo", autonomy=1, kind="expert")
    assert row is not None
    assert row.autonomy_enabled == 1

    names = _tool_names(manager._build_harness_config(row))

    assert set(TEAM_BOARD_TOOL_NAMES).issubset(names)
    assert "ask_creator" in names


def test_plain_expert_does_not_get_board_tools(manager: AgentManager) -> None:
    row = _seed(manager, "plain", autonomy=0, kind="expert")
    assert row is not None

    names = _tool_names(manager._build_harness_config(row))

    assert not set(TEAM_BOARD_TOOL_NAMES) & names
    assert "ask_creator" not in names


def test_team_host_keeps_its_tools(manager: AgentManager) -> None:
    row = _seed(manager, "crew", autonomy=0, kind="team")
    assert row is not None

    names = _tool_names(manager._build_harness_config(row))

    assert set(TEAM_BOARD_TOOL_NAMES).issubset(names)
    assert "ask_creator" in names


def test_ask_creator_tool_and_interrupt_travel_together(manager: AgentManager) -> None:
    """INV-1: carrying the tool implies carrying the interrupt.

    This is the assertion the original mount test was missing: it only
    checked the tool name, which passes even when the escalation channel
    cannot interrupt at all.
    """
    cases = (
        ("solo", "expert", 1),
        ("plain", "expert", 0),
        ("crew", "team", 0),
    )
    for agent_id, kind, autonomy in cases:
        row = _seed(manager, agent_id, autonomy=autonomy, kind=kind)
        assert row is not None
        cfg = manager._build_harness_config(row)

        has_tool = "ask_creator" in _tool_names(cfg)
        has_interrupt = "ask_creator" in _interrupt_names(cfg)

        assert has_tool == has_interrupt, agent_id
        if has_tool:
            entry = (cfg.interrupt_on or {})["ask_creator"]
            assert entry["allowed_decisions"] == ["respond"]


def test_autonomous_expert_can_interrupt_on_ask_creator(manager: AgentManager) -> None:
    """A digital employee's escalation channel is actually wired.

    Without this the agent receives ``HEADLESS_FALLBACK``, concludes nobody
    answered, and proceeds on its own — the creator is never asked.
    """
    row = _seed(manager, "solo2", autonomy=1, kind="expert")
    assert row is not None

    cfg = manager._build_harness_config(row)

    assert "ask_creator" in _tool_names(cfg)
    assert "ask_creator" in _interrupt_names(cfg)
    assert (cfg.interrupt_on or {})["ask_creator"]["allowed_decisions"] == ["respond"]


def test_plain_expert_gets_no_ask_creator_interrupt(manager: AgentManager) -> None:
    """An agent without the tool must not carry a dangling interrupt."""
    row = _seed(manager, "plain2", autonomy=0, kind="expert")
    assert row is not None

    cfg = manager._build_harness_config(row)

    assert "ask_creator" not in _tool_names(cfg)
    assert "ask_creator" not in _interrupt_names(cfg)


def test_autonomous_expert_has_no_peer_tools(manager: AgentManager) -> None:
    """A digital employee must not dispatch work away from its board.

    ``ask_agent`` runs synchronously on the caller's turn, so an unattended
    wake would block on another agent and the work would never appear on
    this agent's board — nobody would ever settle it. Turning the peer
    middleware off for autonomous agents removes ``ask_agent`` and
    ``agent_list`` together.
    """
    row = _seed(manager, "solo3", autonomy=1, kind="expert")
    assert row is not None

    cfg = manager._build_harness_config(row)

    assert getattr(cfg, "team_enabled", True) is False


def test_plain_expert_keeps_peer_tools(manager: AgentManager) -> None:
    row = _seed(manager, "plain3", autonomy=0, kind="expert")
    assert row is not None

    cfg = manager._build_harness_config(row)

    assert getattr(cfg, "team_enabled", True) is True


def test_autonomous_expert_gets_the_employee_briefing(manager: AgentManager) -> None:
    """Board tools without the briefing leave the agent guessing its role."""
    row = _seed(manager, "solo4", autonomy=1, kind="expert")
    assert row is not None

    cfg = manager._build_harness_config(row)

    assert (
        "数字员工" in (cfg.system_prompt or "")
        or "digital employee" in (cfg.system_prompt or "").lower()
    )


def test_plain_expert_gets_no_employee_briefing(manager: AgentManager) -> None:
    row = _seed(manager, "plain4", autonomy=0, kind="expert")
    assert row is not None

    cfg = manager._build_harness_config(row)

    assert "数字员工" not in (cfg.system_prompt or "")


def test_responsible_scope_reaches_the_prompt(manager: AgentManager) -> None:
    """The job description must reach the model, not just the database."""
    from octop.infra.db.repos.autonomy_policy import AutonomyPolicy

    row = _seed(manager, "solo5", autonomy=1, kind="expert")
    assert row is not None
    manager._repos.autonomy_policy_repo.upsert(
        AutonomyPolicy(agent_id="solo5", responsible_scope="Weekly revenue reporting")
    )

    cfg = manager._build_harness_config(row)

    assert "Weekly revenue reporting" in (cfg.system_prompt or "")


def test_empty_scope_adds_nothing(manager: AgentManager) -> None:
    row = _seed(manager, "solo6", autonomy=1, kind="expert")
    assert row is not None

    cfg = manager._build_harness_config(row)

    assert "职责范围" not in (cfg.system_prompt or "")
    assert "responsibilities" not in (cfg.system_prompt or "").lower()


def test_tool_scope_deny_disables_named_tools(manager: AgentManager) -> None:
    """The job spec can narrow the tool surface."""
    from octop.infra.db.repos.autonomy_policy import AutonomyPolicy

    row = _seed(manager, "solo7", autonomy=1, kind="expert")
    assert row is not None
    manager._repos.autonomy_policy_repo.upsert(
        AutonomyPolicy(agent_id="solo7", tool_scope_deny="execute, web_fetch")
    )

    cfg = manager._build_harness_config(row)

    disabled = set(getattr(cfg, "tools_disabled", ()) or ())
    assert {"execute", "web_fetch"} <= disabled


def test_tool_scope_allow_narrows_to_the_named_tools(manager: AgentManager) -> None:
    from octop.infra.db.repos.autonomy_policy import AutonomyPolicy

    row = _seed(manager, "solo8", autonomy=1, kind="expert")
    assert row is not None
    manager._repos.autonomy_policy_repo.upsert(
        AutonomyPolicy(agent_id="solo8", tool_scope_allow="read_file, write_file")
    )

    cfg = manager._build_harness_config(row)

    disabled = set(getattr(cfg, "tools_disabled", ()) or ())
    assert "execute" in disabled  # not in the allow list
    assert "read_file" not in disabled
