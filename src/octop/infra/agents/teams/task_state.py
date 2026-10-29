"""Team task status machine — shared by host tools and the dashboard PATCH.

Both writers validate transitions here so the server, not the client, decides
what a legal flow is. Same-status updates are treated as no-ops (True).
"""

from __future__ import annotations

TASK_STATUSES: frozenset[str] = frozenset(
    {
        "proposed",
        "assigned",
        "running",
        "blocked",
        "needs_input",
        "review",
        "done",
        "failed",
        "cancelled",
    }
)

GOAL_STATUSES: frozenset[str] = frozenset({"planning", "active", "paused", "done", "archived"})

OPEN_TASK_STATUSES: frozenset[str] = TASK_STATUSES - {"done", "failed", "cancelled"}

ALLOWED_TASK_TRANSITIONS: dict[str, frozenset[str]] = {
    # Waiting-related edges (docs/team-drive-v2.md §3.3): a task that cannot
    # be dispatched yet parks in blocked/needs_input without inventing a state.
    "proposed": frozenset({"assigned", "running", "blocked", "needs_input", "cancelled"}),
    "assigned": frozenset({"running", "proposed", "blocked", "needs_input", "cancelled"}),
    "running": frozenset({"blocked", "needs_input", "review", "done", "failed", "cancelled"}),
    "blocked": frozenset({"running", "proposed", "done", "failed", "cancelled"}),
    "needs_input": frozenset({"running", "proposed", "blocked", "cancelled"}),
    # ``proposed`` is the rework edge: a rejected review sends the task back
    # to the queue (team-l3-roadmap A0/A2).
    "review": frozenset({"done", "failed", "running", "proposed", "cancelled"}),
    "done": frozenset(),
    "failed": frozenset({"proposed", "cancelled"}),
    "cancelled": frozenset(),
}


def is_valid_task_transition(current: str, next_status: str) -> bool:
    if next_status not in TASK_STATUSES:
        return False
    if next_status == current:
        return True
    return next_status in ALLOWED_TASK_TRANSITIONS.get(current, frozenset())
