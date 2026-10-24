"""Unit tests for the light-app auto-sync scheduler."""

from __future__ import annotations

import asyncio

from octop.infra.apps.scheduler import AppSourceScheduler
from tests.unit.apps.test_service import World, _create, world  # noqa: F401


async def test_tick_runs_due_sources_and_rearms(world: World) -> None:  # noqa: F811
    app_id = await _create(world)
    row = world.service.get(app_id)
    await world.workspace.awrite_text("inbound/imports/s.csv", "title,qty,status\nmug,1,new\n")
    world.service.save_source(
        row,
        source_id="s1",
        table_key="main",
        kind="workspace_file",
        mode="append",
        schedule_seconds=60,
        params={"path": "inbound/imports/s.csv"},
        created_by=world.alice_id,
    )
    sched = AppSourceScheduler(repo=world.repo, service=world.service, scan_seconds=3600)

    # Never-run scheduled source is due immediately; run imports the file.
    assert await sched.tick() == 1
    _, rows, total = await world.service.read_rows(row)
    assert total == 1 and rows[0]["title"] == "mug"
    view = world.service.get_source(row, "s1")
    assert view.last_status == "ok" and view.last_row_count == 1

    # Re-armed one interval out: not due until it passes.
    assert await sched.tick() == 0
    item = world.repo.get_source(app_id, "s1")
    assert item is not None and item.next_run_at is not None
    world.repo.set_source_run_state(
        "s1",
        status="ok",
        run_at=item.last_run_at or 0,
        next_run_at=item.last_run_at or 0,
        fail_count=0,
    )
    assert await sched.tick() == 1
    _, _, total = await world.service.read_rows(row)
    assert total == 2


async def test_tick_survives_failures(world: World) -> None:  # noqa: F811
    app_id = await _create(world)
    row = world.service.get(app_id)
    world.service.save_source(
        row,
        source_id="s1",
        table_key="main",
        kind="workspace_file",
        mode="append",
        schedule_seconds=60,
        params={"path": "inbound/imports/missing.csv"},
        created_by=world.alice_id,
    )
    sched = AppSourceScheduler(repo=world.repo, service=world.service, scan_seconds=3600)

    # The failing run is contained: tick counts it, state records the error.
    assert await sched.tick() == 1
    view = world.service.get_source(row, "s1")
    assert view.last_status == "error" and view.last_error

    item = world.repo.get_source(app_id, "s1")
    assert item is not None and item.fail_count == 1


async def test_start_and_shutdown(world: World) -> None:  # noqa: F811
    sched = AppSourceScheduler(repo=world.repo, service=world.service, scan_seconds=3600)
    sched.start()
    task = sched._task
    assert task is not None
    sched.start()  # idempotent: keeps the same task
    assert sched._task is task
    await asyncio.sleep(0)
    await sched.shutdown()
    assert sched._task is None
    await sched.shutdown()  # second call is a no-op
