from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from ..errors import ValidationError

FROZEN_EVENT_TYPES = frozenset(
    {
        "task_posted",
        "task_assigned",
        "task_heartbeat",
        "task_completed",
        "task_reviewed",
        "task_stale",
        "task_reassigned",
        "task_failed",
    }
)

# Operational signals (liveness); not chief wakeups. Still stored in the immutable event log.
OPERATIONAL_EVENT_TYPES = frozenset({"task_heartbeat"})

LIFECYCLE_EVENT_TYPES = frozenset(FROZEN_EVENT_TYPES - OPERATIONAL_EVENT_TYPES)

# Chief coordinates only on lifecycle-class events (excludes heartbeats).
CHIEF_WAKEUP_EVENT_TYPES = frozenset(
    {
        "task_posted",
        "task_assigned",
        "task_completed",
        "task_reviewed",
        "task_stale",
        "task_reassigned",
        "task_failed",
    }
)

ALLOWED_INTENTS = {
    "board.post_task",
    "board.update_task",
    "board.get_task",
    "board.get_full_state",
    "board.list_tasks_by_status",
    "board.register_agent",
    "board.post_agent_heartbeat",
    "board.stream_events",
    "board.put_data",
    "board.get_data",
    "board.delete_data",
    "board.get_task_history",
    "board.get_agent_activity",
    "chief.wake",
    "worker.execute_task",
    "worker.post_result",
}


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(slots=True)
class A2ARequest:
    intent: str
    payload: dict[str, Any]
    request_id: str | None = None
    idempotency_key: str | None = None
    timestamp: str | None = None

    def to_dict(self) -> dict[str, Any]:
        request_id = self.request_id or uuid4().hex[:12]
        timestamp = self.timestamp or utc_now_iso()
        return {
            "intent": self.intent,
            "request_id": request_id,
            "idempotency_key": self.idempotency_key,
            "timestamp": timestamp,
            "payload": self.payload,
        }


@dataclass(slots=True)
class A2AResponse:
    request_id: str
    ok: bool
    result: dict[str, Any] | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "ok": self.ok,
            "error": self.error,
            "result": self.result or {},
        }


def validate_request_envelope(envelope: dict[str, Any]) -> None:
    required = {"intent", "request_id", "timestamp", "payload"}
    missing = required - envelope.keys()
    if missing:
        raise ValidationError(f"Missing request fields: {sorted(missing)}")
    if envelope["intent"] not in ALLOWED_INTENTS:
        raise ValidationError(f"Unsupported intent: {envelope['intent']}")
    if not isinstance(envelope["payload"], dict):
        raise ValidationError("payload must be an object")
