"""In-memory registry of pending HITL approvals for IM channels."""

from __future__ import annotations

import contextlib
import json
import secrets
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from octop.infra.gateway.hitl.format import is_ask_action_requests

if TYPE_CHECKING:
    from octop.infra.db.repos.hitl import HitlPendingRepo

HitlPendingStatus = Literal["pending", "approved", "rejected", "expired"]

# ``kind`` column values for the durable ``hitl_pending`` mirror.
HITL_KIND_APPROVAL = "approval"
HITL_KIND_ASK = "ask"

_DEFAULT_TTL_SECONDS = 30 * 60


def _same_pause_slot(
    existing: HitlPendingRecord,
    *,
    session_key: str,
    resume_agent_id: str | None,
) -> bool:
    if existing.session_key != session_key or existing.status != "pending":
        return False
    if resume_agent_id:
        return existing.resume_agent_id == resume_agent_id
    return not existing.resume_agent_id


def _actions_equivalent(
    left: list[dict[str, Any]],
    right: list[dict[str, Any]],
) -> bool:
    return json.dumps(left, sort_keys=True, default=str) == json.dumps(
        right, sort_keys=True, default=str
    )


def _action_task_id(action_requests: list[dict[str, Any]]) -> str | None:
    """Task association carried by ask-style tools (e.g. ``ask_creator``)."""
    for action in action_requests:
        if not isinstance(action, dict):
            continue
        args = action.get("args")
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except ValueError:
                args = {}
        if isinstance(args, dict) and args.get("task_id"):
            return str(args["task_id"])
    return None


@dataclass
class HitlPendingRecord:
    pending_id: str
    thread_id: str
    agent_id: str
    user_id: int
    session_key: str
    channel_type: str
    action_requests: list[dict[str, Any]]
    review_configs: list[dict[str, Any]] | None
    created_at: float
    status: HitlPendingStatus = "pending"
    ask_question_index: int = 0
    ask_answers: list[str] = field(default_factory=list)
    # Team member pauses are stored on the room thread the user is viewing.
    # Resume still targets the member agent and ``room~member`` thread.
    resume_agent_id: str | None = None
    resume_thread_id: str | None = None
    # Durable ``hitl_pending`` linkage (None for records created pre-repo or
    # by tests that build bare records).
    task_id: str | None = None
    row_pk: int | None = None


@dataclass
class HitlPendingStore:
    """Session-scoped pending HITL records (process-local, TTL-gc).

    When a ``hitl_repo`` is attached, every register/resolve also lands in the
    ``hitl_pending`` table and :meth:`load_persisted` rehydrates still-open
    pauses at boot, so a restart no longer drops an approval the creator has
    not answered yet.
    """

    ttl_seconds: float = _DEFAULT_TTL_SECONDS
    repo: HitlPendingRepo | None = None
    # Shadow coordinator (docs/team-drive-v2.md §11.2 phase 1): team-scoped
    # resolutions are appended to ``team_events`` on the side.
    event_recorder: Any | None = None
    _records: dict[str, HitlPendingRecord] = field(default_factory=dict)

    def register(
        self,
        *,
        thread_id: str,
        agent_id: str,
        user_id: int,
        session_key: str,
        channel_type: str,
        action_requests: list[dict[str, Any]],
        review_configs: list[dict[str, Any]] | None,
        resume_agent_id: str | None = None,
    ) -> HitlPendingRecord:
        self._gc()
        # A new pause replaces only the same slot. Two team members share the
        # room session, so one member's card must not expire the other's.
        # The same interrupt is often projected twice; reuse that pending id
        # so an automatic resume of the first frame is not already expired.
        for existing in list(self._records.values()):
            if not _same_pause_slot(
                existing,
                session_key=session_key,
                resume_agent_id=resume_agent_id,
            ):
                continue
            if _actions_equivalent(existing.action_requests, action_requests):
                return existing
        for existing in list(self._records.values()):
            if not _same_pause_slot(
                existing,
                session_key=session_key,
                resume_agent_id=resume_agent_id,
            ):
                continue
            existing.status = "expired"
        pending_id = secrets.token_hex(2)
        while pending_id in self._records:
            pending_id = secrets.token_hex(2)
        record = HitlPendingRecord(
            pending_id=pending_id,
            thread_id=thread_id,
            agent_id=agent_id,
            user_id=user_id,
            session_key=session_key,
            channel_type=channel_type,
            action_requests=list(action_requests),
            review_configs=list(review_configs) if review_configs else None,
            created_at=time.time(),
            resume_agent_id=resume_agent_id,
            task_id=_action_task_id(action_requests),
        )
        self._records[pending_id] = record
        self._persist_new(record)
        return record

    def get(self, pending_id: str) -> HitlPendingRecord | None:
        self._gc()
        record = self._records.get(pending_id)
        if record is None:
            return None
        if record.status != "pending":
            return record
        if time.time() - record.created_at > self.ttl_seconds:
            record.status = "expired"
            return record
        return record

    def get_pending(
        self,
        pending_id: str,
        *,
        session_key: str,
        agent_id: str,
    ) -> HitlPendingRecord | None:
        record = self.get(pending_id)
        if record is None or record.status != "pending":
            return None
        if record.session_key != session_key or record.agent_id != agent_id:
            return None
        return record

    def resolve_for_session(
        self,
        session_key: str,
        pending_id: str | None = None,
        *,
        agent_id: str | None = None,
    ) -> HitlPendingRecord | None:
        self._gc()
        if pending_id:
            record = self.get(pending_id)
            if record is None or record.session_key != session_key:
                return None
            if agent_id is not None and record.agent_id != agent_id:
                return None
            return record if record.status == "pending" else None
        latest: HitlPendingRecord | None = None
        for record in self._records.values():
            if record.session_key != session_key or record.status != "pending":
                continue
            if agent_id is not None and record.agent_id != agent_id:
                continue
            if time.time() - record.created_at > self.ttl_seconds:
                record.status = "expired"
                continue
            if latest is None or record.created_at > latest.created_at:
                latest = record
        return latest

    def resolve_pending_for_thread(
        self,
        thread_id: str,
        *,
        agent_id: str,
        user_id: int | None = None,
        pending_id: str | None = None,
    ) -> HitlPendingRecord | None:
        """Return a pending approval for a dashboard thread, if any.

        ``pending_id`` selects that card. Without it, the newest pending card
        on the thread is returned.
        """
        self._gc()
        if pending_id:
            record = self._records.get(pending_id)
            if record is None or record.status != "pending":
                return None
            if not self._thread_pending_match(
                record,
                thread_id=thread_id,
                agent_id=agent_id,
                user_id=user_id,
            ):
                return None
            return record
        latest: HitlPendingRecord | None = None
        for record in self._records.values():
            if record.status != "pending":
                continue
            if not self._thread_pending_match(
                record,
                thread_id=thread_id,
                agent_id=agent_id,
                user_id=user_id,
            ):
                continue
            if latest is None or record.created_at > latest.created_at:
                latest = record
        return latest

    def _thread_pending_match(
        self,
        record: HitlPendingRecord,
        *,
        thread_id: str,
        agent_id: str,
        user_id: int | None,
    ) -> bool:
        if record.status != "pending":
            return False
        room_match = record.thread_id == thread_id and record.agent_id == agent_id
        member_match = bool(
            record.resume_thread_id
            and record.resume_thread_id == thread_id
            and record.resume_agent_id == agent_id
        )
        if not room_match and not member_match:
            return False
        if user_id is not None and record.user_id != user_id:
            return False
        if time.time() - record.created_at > self.ttl_seconds:
            record.status = "expired"
            return False
        return True

    def list_pending_for_session(
        self,
        session_key: str,
        *,
        agent_id: str | None = None,
    ) -> list[HitlPendingRecord]:
        self._gc()
        rows = [
            r
            for r in self._records.values()
            if r.session_key == session_key
            and r.status == "pending"
            and (agent_id is None or r.agent_id == agent_id)
        ]
        rows.sort(key=lambda r: r.created_at, reverse=True)
        return rows

    def pending_thread_ids(self, *, agent_id: str, user_id: int) -> frozenset[str]:
        """Thread ids with a still-pending HITL pause for this agent/user."""
        self._gc()
        now = time.time()
        out: set[str] = set()
        for record in self._records.values():
            if record.status != "pending":
                continue
            if record.user_id != user_id:
                continue
            if now - record.created_at > self.ttl_seconds:
                continue
            if record.agent_id == agent_id:
                tid = (record.thread_id or "").strip()
            elif record.resume_agent_id == agent_id:
                tid = (record.resume_thread_id or "").strip()
            else:
                continue
            if tid:
                out.add(tid)
        return frozenset(out)

    def expire_pending_for_thread(
        self,
        thread_id: str,
        *,
        agent_id: str,
        user_id: int | None = None,
    ) -> None:
        """Drop leftover dashboard pauses so history reload cannot reinject them."""
        self._gc()
        for record in self._records.values():
            if record.thread_id != thread_id or record.status != "pending":
                continue
            if record.agent_id != agent_id:
                continue
            if user_id is not None and record.user_id != user_id:
                continue
            record.status = "expired"

    def attach_task(self, pending_id: str, task_id: str) -> None:
        """Link a pause to a board task discovered from the dispatch job id."""
        record = self._records.get(pending_id)
        if record is None:
            return
        record.task_id = task_id
        if record.row_pk is not None and self.repo is not None:
            with contextlib.suppress(Exception):
                self.repo.set_task_id(record.row_pk, task_id)

    def expire_for_task(self, task_id: str) -> int:
        """Expire pending pauses linked to a board task (wait timeout, §7.2)."""
        expired = 0
        for record in list(self._records.values()):
            if record.task_id == task_id and record.status == "pending":
                self.mark_resolved(record.pending_id, "expired")
                expired += 1
        return expired

    def mark_resolved(
        self,
        pending_id: str,
        status: Literal["approved", "rejected", "expired"],
    ) -> None:
        record = self._records.get(pending_id)
        if record is not None:
            record.status = status
            self._persist_status(record, status)
            if self.event_recorder is not None and record.task_id is not None:
                self.event_recorder.record_hitl_resolved(
                    task_id=record.task_id,
                    pending_id=record.pending_id,
                    status=status,
                    agent_id=record.agent_id,
                )

    def append_ask_answer(self, pending_id: str, answer: str) -> HitlPendingRecord | None:
        """Record one IM answer and advance to the next question."""
        record = self.get(pending_id)
        if record is None or record.status != "pending":
            return None
        record.ask_answers.append(answer)
        record.ask_question_index += 1
        self._persist_payload(record)
        return record

    def load_persisted(self) -> None:
        """Rehydrate still-open pauses from ``hitl_pending`` after a restart.

        Fresh ``pending_id`` nonces are minted per record — the dashboard
        re-fetches the card through history after a restart, so old ids do not
        matter. Rows past the TTL are expired in the table instead.
        """
        if self.repo is None:
            return
        now = time.time()
        for row in self.repo.list_pending():
            age = now - row.created_at
            if age > self.ttl_seconds:
                self.repo.mark_status(row.pk, "expired")
                continue
            # Reuse the persisted nonce: the approval card in the thread
            # history carries this id (the projection bakes it into the
            # message), so minting a new one makes the card unanswerable.
            pending_id = str(row.payload.get("pending_id") or "").strip()
            if not pending_id or pending_id in self._records:
                pending_id = secrets.token_hex(2)
                while pending_id in self._records:
                    pending_id = secrets.token_hex(2)
            record = HitlPendingRecord(
                pending_id=pending_id,
                thread_id=row.thread_id or "",
                agent_id=str(row.payload.get("agent_id") or row.created_by_agent or ""),
                user_id=int(row.payload.get("user_id") or 0),
                session_key=row.session_key,
                channel_type=str(row.payload.get("channel_type") or ""),
                action_requests=list(row.payload.get("action_requests") or []),
                review_configs=row.payload.get("review_configs"),
                created_at=float(row.created_at),
                resume_agent_id=row.payload.get("resume_agent_id"),
                resume_thread_id=row.payload.get("resume_thread_id"),
                task_id=row.task_id,
                row_pk=row.pk,
            )
            record.ask_question_index = int(row.payload.get("ask_question_index") or 0)
            record.ask_answers = list(row.payload.get("ask_answers") or [])
            self._records[pending_id] = record

    def _persist_new(self, record: HitlPendingRecord) -> None:
        if self.repo is None or record.row_pk is not None:
            return
        try:
            row = self.repo.create(
                session_key=record.session_key,
                thread_id=record.thread_id,
                kind=(
                    HITL_KIND_ASK
                    if is_ask_action_requests(record.action_requests)
                    else HITL_KIND_APPROVAL
                ),
                payload=self._record_payload(record),
                created_by_agent=record.agent_id,
                task_id=record.task_id,
                expires_at=int(record.created_at + self.ttl_seconds),
            )
        except Exception:
            return
        record.row_pk = row.pk

    def _persist_status(self, record: HitlPendingRecord, status: str) -> None:
        if self.repo is None or record.row_pk is None:
            return
        with contextlib.suppress(Exception):
            self.repo.mark_status(record.row_pk, status)

    def _persist_payload(self, record: HitlPendingRecord) -> None:
        if self.repo is None or record.row_pk is None:
            return
        with contextlib.suppress(Exception):
            self.repo.update_payload(record.row_pk, self._record_payload(record))

    @staticmethod
    def _record_payload(record: HitlPendingRecord) -> dict[str, Any]:
        return {
            "pending_id": record.pending_id,
            "agent_id": record.agent_id,
            "user_id": record.user_id,
            "channel_type": record.channel_type,
            "action_requests": record.action_requests,
            "review_configs": record.review_configs,
            "resume_agent_id": record.resume_agent_id,
            "resume_thread_id": record.resume_thread_id,
            "ask_question_index": record.ask_question_index,
            "ask_answers": list(record.ask_answers),
        }

    def _gc(self) -> None:
        now = time.time()
        stale_ids: list[str] = []
        for pending_id, record in self._records.items():
            age = now - record.created_at
            if record.status == "pending" and age > self.ttl_seconds:
                record.status = "expired"
                self._persist_status(record, "expired")
            if record.status != "pending" and age > self.ttl_seconds:
                stale_ids.append(pending_id)
        for pending_id in stale_ids:
            del self._records[pending_id]
