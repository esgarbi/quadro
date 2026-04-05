from __future__ import annotations

from abc import ABC, abstractmethod

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
    def put_data(self, key: str, value: dict) -> None: ...

    @abstractmethod
    def get_data(self, key: str) -> dict | None: ...

    @abstractmethod
    def list_data(self) -> dict[str, dict]: ...
