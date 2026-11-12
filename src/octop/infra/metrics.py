"""In-memory metrics counters."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field


@dataclass
class Metrics:
    messages_total: int = 0
    stream_errors_total: int = 0
    cron_runs_total: int = 0
    cron_errors_total: int = 0
    cron_hitl_total: int = 0
    team_wake_total: int = 0
    team_wake_noop_total: int = 0
    team_tasks_stale_reaped_total: int = 0
    team_event_total: int = 0
    team_event_routine_total: int = 0
    team_event_judgment_total: int = 0
    external_event_received_total: int = 0
    external_event_duplicate_total: int = 0
    external_delivery_delivered_total: int = 0
    external_delivery_failed_total: int = 0
    external_poll_total: int = 0
    external_poll_error_total: int = 0
    light_app_open_total: int = 0
    light_app_row_write_total: int = 0
    light_app_export_total: int = 0
    light_app_assistant_total: int = 0
    light_app_generate_total: int = 0
    light_app_public_view_total: int = 0
    agent_active: int = 0
    first_agent_created_total: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def inc(self, name: str, n: int = 1) -> None:
        with self._lock:
            setattr(self, name, getattr(self, name) + n)

    def set(self, name: str, value: int) -> None:
        with self._lock:
            setattr(self, name, value)

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return {
                "messages_total": self.messages_total,
                "stream_errors_total": self.stream_errors_total,
                "cron_runs_total": self.cron_runs_total,
                "cron_errors_total": self.cron_errors_total,
                "cron_hitl_total": self.cron_hitl_total,
                "team_wake_total": self.team_wake_total,
                "team_wake_noop_total": self.team_wake_noop_total,
                "team_tasks_stale_reaped_total": self.team_tasks_stale_reaped_total,
                "team_event_total": self.team_event_total,
                "team_event_routine_total": self.team_event_routine_total,
                "team_event_judgment_total": self.team_event_judgment_total,
                "external_event_received_total": self.external_event_received_total,
                "external_event_duplicate_total": self.external_event_duplicate_total,
                "external_delivery_delivered_total": self.external_delivery_delivered_total,
                "external_delivery_failed_total": self.external_delivery_failed_total,
                "external_poll_total": self.external_poll_total,
                "external_poll_error_total": self.external_poll_error_total,
                "light_app_open_total": self.light_app_open_total,
                "light_app_row_write_total": self.light_app_row_write_total,
                "light_app_export_total": self.light_app_export_total,
                "light_app_assistant_total": self.light_app_assistant_total,
                "light_app_generate_total": self.light_app_generate_total,
                "light_app_public_view_total": self.light_app_public_view_total,
                "agent_active": self.agent_active,
                "first_agent_created_total": self.first_agent_created_total,
            }


METRICS = Metrics()
