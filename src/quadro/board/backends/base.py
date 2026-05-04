from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from ..records import AgentRecord, EventRecord, TaskRecord


class BoardBackend(ABC):
    @abstractmethod
    def init(self) -> None:
        pass

    @abstractmethod
    def create_task(self, task: TaskRecord) -> None:
        pass

    @abstractmethod
    def update_task(self, task: TaskRecord) -> None:
        pass

    @abstractmethod
    def get_task(self, task_id: str) -> TaskRecord | None:
        pass

    @abstractmethod
    def list_tasks(self) -> list[TaskRecord]:
        pass

    @abstractmethod
    def list_tasks_by_status(self, statuses: set[str]) -> list[TaskRecord]:
        """Tasks matching any of the given statuses, ordered by priority ASC."""

    @abstractmethod
    def upsert_agent(self, agent: AgentRecord) -> None:
        pass

    @abstractmethod
    def get_agent(self, agent_id: str) -> AgentRecord | None:
        pass

    @abstractmethod
    def list_agents(self) -> list[AgentRecord]:
        pass

    @abstractmethod
    def append_event(self, event: EventRecord) -> int:
        pass

    @abstractmethod
    def list_events_since(self, sequence_id: int) -> list[EventRecord]:
        pass

    @abstractmethod
    def list_events_for_task(self, task_id: str) -> list[EventRecord]:
        """All events for a specific task, ordered by sequence_id ASC."""

    @abstractmethod
    def list_events_for_agent(self, agent_id: str) -> list[EventRecord]:
        """All events involving a specific agent_id, ordered by sequence_id ASC."""

    @abstractmethod
    def put_data(self, key: str, value: Any) -> None: ...

    @abstractmethod
    def get_data(self, key: str) -> Any | None: ...

    @abstractmethod
    def list_data(self, prefix: str | None = None) -> dict[str, Any]: ...

    @abstractmethod
    def delete_data(self, key: str) -> bool:
        """Delete a data entry. Returns True if key existed, False otherwise."""

    @abstractmethod
    def archive_task(self, task_id: str) -> bool:
        """Move a task to the archive. Returns True if task existed."""

    @abstractmethod
    def get_archived_task(self, task_id: str) -> TaskRecord | None:
        """Retrieve an archived task by ID."""
