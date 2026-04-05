from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class TaskStatus(StrEnum):
    UNASSIGNED = "UNASSIGNED"
    IN_PROGRESS = "IN_PROGRESS"
    PENDING_REVIEW = "PENDING_REVIEW"
    REVISION_NEEDED = "REVISION_NEEDED"
    APPROVED = "APPROVED"
    COMPLETE = "COMPLETE"
    STALE = "STALE"
    HUMAN_REVIEW = "HUMAN_REVIEW"
    ON_HOLD = "ON_HOLD"


class AgentStatus(StrEnum):
    IDLE = "IDLE"
    BUSY = "BUSY"
    OFFLINE = "OFFLINE"


@dataclass(slots=True)
class TaskRecord:
    task_id: str
    task_type: str
    label: str
    priority: int = 5
    status: TaskStatus | str = TaskStatus.UNASSIGNED
    assigned_to: str | None = None
    output: str | dict[str, Any] | None = None
    notes: list[str] = field(default_factory=list)
    continuation_token: str | None = None
    heartbeat_at: datetime | None = None
    context_snapshot_hash: str | None = None
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["status"] = (
            self.status.value if isinstance(self.status, TaskStatus) else self.status
        )
        payload["priority"] = self.priority
        payload["created_at"] = self.created_at.isoformat()
        payload["updated_at"] = self.updated_at.isoformat()
        payload["heartbeat_at"] = (
            self.heartbeat_at.isoformat() if self.heartbeat_at else None
        )
        return payload


@dataclass(slots=True)
class AgentRecord:
    agent_id: str
    name: str
    status: AgentStatus
    capabilities: list[str]
    a2a_url: str
    agent_card: dict[str, Any]
    current_task_id: str | None = None
    version: str | None = None
    last_seen_at: datetime = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["status"] = self.status.value
        payload["last_seen_at"] = self.last_seen_at.isoformat()
        return payload


@dataclass(slots=True)
class EventRecord:
    sequence_id: int
    event_type: str
    task_id: str
    agent_id: str | None
    from_status: TaskStatus | str | None
    to_status: TaskStatus | str | None
    payload: dict[str, Any]
    idempotency_key: str | None = None
    timestamp: datetime = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        def _status_str(s: TaskStatus | str | None) -> str | None:
            if s is None:
                return None
            return s.value if isinstance(s, TaskStatus) else s

        return {
            "sequence_id": self.sequence_id,
            "event_type": self.event_type,
            "task_id": self.task_id,
            "agent_id": self.agent_id,
            "from_status": _status_str(self.from_status),
            "to_status": _status_str(self.to_status),
            "payload": self.payload,
            "idempotency_key": self.idempotency_key,
            "timestamp": self.timestamp.isoformat(),
        }
