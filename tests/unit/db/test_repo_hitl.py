"""Unit tests for the hitl_pending repo (durable HITL mirror)."""

from __future__ import annotations

from pathlib import Path

import pytest

from octop.infra.db.migrate import run_migrations
from octop.infra.db.pool import SqlitePool
from octop.infra.db.repos.hitl import HitlPendingRepo


@pytest.fixture
def repo(tmp_path: Path) -> HitlPendingRepo:
    pool = SqlitePool(tmp_path / "octop.db")
    run_migrations(pool)
    return HitlPendingRepo(pool)


def test_create_round_trips_payload(repo: HitlPendingRepo) -> None:
    row = repo.create(
        session_key="team-01:dashboard:1:dm",
        thread_id="thr-1",
        kind="ask",
        payload={
            "pending_id": "ab12",
            "agent_id": "team-01",
            "user_id": 1,
            "action_requests": [
                {"name": "ask_creator", "args": {"task_id": "t1"}},
            ],
        },
        created_by_agent="team-01",
        task_id="t1",
        expires_at=1234567890,
    )
    got = repo.get(row.pk)
    assert got is not None
    assert got.session_key == "team-01:dashboard:1:dm"
    assert got.thread_id == "thr-1"
    assert got.kind == "ask"
    assert got.task_id == "t1"
    assert got.status == "pending"
    assert got.payload["pending_id"] == "ab12"
    assert got.created_by_agent == "team-01"
    assert got.expires_at == 1234567890
    assert got.resolved_at is None


def test_mark_status_sets_resolved_at(repo: HitlPendingRepo) -> None:
    row = repo.create(session_key="k", thread_id=None, kind="approval", payload={})
    repo.mark_status(row.pk, "approved", resolved_by="user:1")
    got = repo.get(row.pk)
    assert got is not None
    assert got.status == "approved"
    assert got.resolved_by == "user:1"
    assert got.resolved_at is not None
    assert repo.list_pending() == []


def test_list_pending_and_by_session(repo: HitlPendingRepo) -> None:
    first = repo.create(session_key="s1", thread_id=None, kind="approval", payload={})
    second = repo.create(session_key="s2", thread_id=None, kind="ask", payload={})
    # Global listing (boot recovery source), ordered by insertion.
    assert [r.pk for r in repo.list_pending()] == [first.pk, second.pk]
    assert [r.session_key for r in repo.list_by_session("s2", status="pending")] == ["s2"]
    assert repo.list_by_session("s1", status="approved") == []


def test_has_pending_ask_scopes_by_task(repo: HitlPendingRepo) -> None:
    repo.create(session_key="s1", thread_id=None, kind="ask", payload={}, task_id="t1")
    assert repo.has_pending_ask("s1", task_id="t1")
    assert not repo.has_pending_ask("s1", task_id="t2")
    assert not repo.has_pending_ask("s2", task_id="t1")


def test_update_payload(repo: HitlPendingRepo) -> None:
    row = repo.create(session_key="s1", thread_id=None, kind="ask", payload={"ask_answers": []})
    repo.update_payload(row.pk, {"ask_answers": ["option a"], "ask_question_index": 1})
    got = repo.get(row.pk)
    assert got is not None
    assert got.payload["ask_answers"] == ["option a"]
    assert got.payload["ask_question_index"] == 1


def test_set_task_id_links_late(repo: HitlPendingRepo) -> None:
    row = repo.create(session_key="s1", thread_id=None, kind="approval", payload={})
    assert row.task_id is None
    repo.set_task_id(row.pk, "t1")
    got = repo.get(row.pk)
    assert got is not None and got.task_id == "t1"
