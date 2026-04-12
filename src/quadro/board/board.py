from __future__ import annotations

from uuid import uuid4

from ..a2a.contracts import A2AResponse, FROZEN_EVENT_TYPES, validate_request_envelope
from ..a2a.dispatch import LocalA2ANetwork
from .backends.base import BoardBackend
from .id_provider import DefaultTaskIdProvider, TaskIdProvider
from .records import (
    AgentRecord,
    AgentStatus,
    EventRecord,
    TaskRecord,
    TaskStatus,
    utc_now,
)
from .state_machine import (
    STANDARD_TERMINAL_STATUSES,
    Lifecycle,
    TransitionError,
    compute_custom_terminal_statuses,
    validate_transition,
)


def _event_for_transition(from_status: TaskStatus | None, to_status: TaskStatus) -> str:
    if from_status is None and to_status == TaskStatus.UNASSIGNED:
        return "task_posted"
    if from_status == TaskStatus.UNASSIGNED and to_status == TaskStatus.IN_PROGRESS:
        return "task_assigned"
    if from_status == TaskStatus.STALE and to_status == TaskStatus.UNASSIGNED:
        return "task_reassigned"
    if to_status == TaskStatus.STALE:
        return "task_stale"
    if to_status == TaskStatus.HUMAN_REVIEW:
        return "task_failed"
    if from_status in {
        TaskStatus.IN_PROGRESS,
        TaskStatus.REVISION_NEEDED,
    } and to_status in {
        TaskStatus.PENDING_REVIEW,
        TaskStatus.COMPLETE,
    }:
        return "task_completed"
    if from_status == TaskStatus.PENDING_REVIEW and to_status == TaskStatus.IN_PROGRESS:
        return "task_assigned"
    if (
        from_status == TaskStatus.REVISION_NEEDED
        and to_status == TaskStatus.IN_PROGRESS
    ):
        return "task_assigned"
    if from_status == TaskStatus.IN_PROGRESS and to_status in {
        TaskStatus.APPROVED,
        TaskStatus.REVISION_NEEDED,
    }:
        return "task_reviewed"
    if from_status == TaskStatus.APPROVED and to_status == TaskStatus.COMPLETE:
        return "task_reviewed"
    raise ValueError(
        f"No event type mapping for transition {from_status} -> {to_status}"
    )


def _update_agent_status(
    agent: AgentRecord,
    task: TaskRecord,
    from_status: TaskStatus | str | None,
    to_status: TaskStatus | str,
    explicitly_assigned: bool,
    custom_terminal_statuses: frozenset[str] = frozenset(),
) -> None:
    """
    Update agent BUSY/IDLE state correctly for both standard and custom profiles.

    Standard profiles use TaskStatus.IN_PROGRESS as the "working" state.
    Custom profiles (newsroom, ordering system) use domain-specific strings like
    "writing", "researching", "delivering" — none of which are IN_PROGRESS.

    Args:
        custom_terminal_statuses: Terminal statuses for the task's profile, derived
            automatically by QuadroBoard from the profile's transition graph via
            compute_custom_terminal_statuses(). Agents transitioning into these
            statuses are released to IDLE. Never hardcode this — let the state
            machine derive it from the transitions you declared.

    Rules:
      BUSY if:
        - to_status is TaskStatus.IN_PROGRESS  (standard profiles)
        - to_status is a custom string AND the caller explicitly set assigned_to
          AND it is not a terminal status for this profile  (custom profiles)

      IDLE if:
        - to_status is a known standard terminal/handoff (COMPLETE, HUMAN_REVIEW,
          PENDING_REVIEW, REVISION_NEEDED, APPROVED, STALE, ON_HOLD)
        - from_status was IN_PROGRESS and to_status is custom (worker posted result)
        - to_status is a terminal status for this profile (derived from transitions)
        - to_status is custom, NOT explicitly assigned, AND from_status was also
          custom — meaning a worker advanced the task without a new assignment
          (e.g., research_worker transitions "researching" → "research_ready")

      Unchanged otherwise (e.g., notes-only updates, minor metadata changes).
    """
    to_is_standard = isinstance(to_status, TaskStatus)
    to_is_custom = not to_is_standard
    to_str = str(to_status) if to_is_custom else None

    from_is_standard = isinstance(from_status, TaskStatus)
    from_is_custom = from_status is not None and not from_is_standard

    _STANDARD_RELEASES: frozenset[TaskStatus] = frozenset(
        {
            TaskStatus.PENDING_REVIEW,
            TaskStatus.REVISION_NEEDED,
            TaskStatus.APPROVED,
            TaskStatus.COMPLETE,
            TaskStatus.STALE,
            TaskStatus.HUMAN_REVIEW,
            TaskStatus.ON_HOLD,
        }
    )

    # ── BUSY conditions ────────────────────────────────────────────────────────
    if to_status == TaskStatus.IN_PROGRESS:
        agent.status = AgentStatus.BUSY
        agent.current_task_id = task.task_id
        return

    if to_is_custom and explicitly_assigned and to_str not in custom_terminal_statuses:
        agent.status = AgentStatus.BUSY
        agent.current_task_id = task.task_id
        return

    # ── IDLE conditions ────────────────────────────────────────────────────────
    if to_is_standard and to_status in _STANDARD_RELEASES:
        agent.status = AgentStatus.IDLE
        agent.current_task_id = None
        return

    if to_is_custom and to_str in custom_terminal_statuses:
        agent.status = AgentStatus.IDLE
        agent.current_task_id = None
        return

    if to_is_custom and from_status == TaskStatus.IN_PROGRESS:
        agent.status = AgentStatus.IDLE
        agent.current_task_id = None
        return

    if to_is_custom and from_is_custom and not explicitly_assigned:
        agent.status = AgentStatus.IDLE
        agent.current_task_id = None
        return

    # Unchanged — metadata update or unrecognised transition; agent state unaffected.


class QuadroBoard:
    _DEFAULT_URL = "a2a://board"

    def __init__(
        self,
        backend: BoardBackend,
        profile_resolver: dict[str, str] | None = None,
        custom_profiles: dict[str, set[tuple[str, str]]] | None = None,
        *,
        network: LocalA2ANetwork | None = None,
        url: str | None = None,
        id_provider: TaskIdProvider | None = None,
    ) -> None:
        self._backend = backend
        self._backend.init()
        self._id_provider: TaskIdProvider = id_provider or DefaultTaskIdProvider()
        self._profile_resolver = profile_resolver or {}
        self._custom_profiles = custom_profiles or {}

        self._custom_terminal_statuses: dict[str, frozenset[str]] = {
            name: compute_custom_terminal_statuses(transitions)
            for name, transitions in self._custom_profiles.items()
        }

        self._url: str = url or self._DEFAULT_URL
        self._network: LocalA2ANetwork | None = network
        if network is not None:
            network.register_endpoint(self._url, self.handle_request)

        for name, profile in self._custom_profiles.items():
            if isinstance(profile, Lifecycle):
                self._backend.put_data(
                    "_col_order",
                    list(profile.col_order),
                )
                break

    def client(self) -> BoardClient:
        """
        Return a BoardClient for this board.

        Requires that the board was constructed with network= so it has a
        transport to offer. Raises RuntimeError if network was not provided.
        """
        if self._network is None:
            raise RuntimeError(
                "QuadroBoard.client() requires the board to be constructed with "
                "network=... — without a network, the board has no transport to "
                "vend a client for."
            )
        from .client import BoardClient as _BoardClient

        return _BoardClient(self._network, self._url)

    def _profile_for_task_type(self, task_type: str) -> str:
        return self._profile_resolver.get(task_type, "review_required")

    def _terminal_statuses_for_profile(self, profile: str) -> frozenset[str]:
        return self._custom_terminal_statuses.get(profile, frozenset())

    def _append_event(
        self,
        *,
        event_type: str,
        task_id: str,
        agent_id: str | None,
        from_status: TaskStatus | None,
        to_status: TaskStatus | None,
        payload: dict,
        idempotency_key: str | None = None,
    ) -> EventRecord:
        if event_type not in FROZEN_EVENT_TYPES:
            raise ValueError(f"Unsupported frozen event type: {event_type}")
        record = EventRecord(
            sequence_id=0,
            event_type=event_type,
            task_id=task_id,
            agent_id=agent_id,
            from_status=from_status,
            to_status=to_status,
            payload=payload,
            idempotency_key=idempotency_key,
        )
        sequence_id = self._backend.append_event(record)
        record.sequence_id = sequence_id
        return record

    def handle_request(self, envelope: dict) -> dict:
        request_id = envelope.get("request_id", uuid4().hex[:12])
        try:
            validate_request_envelope(envelope)
            intent = envelope["intent"]
            payload = envelope["payload"]
            if intent == "board.post_task":
                result = self._post_task(payload, envelope.get("idempotency_key"))
            elif intent == "board.update_task":
                result = self._update_task(payload, envelope.get("idempotency_key"))
            elif intent == "board.get_task":
                result = self._get_task(payload)
            elif intent == "board.get_full_state":
                result = self._get_full_state()
            elif intent == "board.register_agent":
                result = self._register_agent(payload)
            elif intent == "board.post_agent_heartbeat":
                result = self._post_agent_heartbeat(
                    payload, envelope.get("idempotency_key")
                )
            elif intent == "board.stream_events":
                result = self._stream_events(payload)
            elif intent == "board.put_data":
                result = self._put_data(payload)
            elif intent == "board.get_data":
                result = self._get_data(payload)
            elif intent == "board.get_task_history":
                result = self._get_task_history(payload)
            elif intent == "board.get_agent_activity":
                result = self._get_agent_activity(payload)
            elif intent == "worker.post_result":
                result = self._worker_post_result(
                    payload, envelope.get("idempotency_key")
                )
            else:
                raise ValueError(f"Unsupported board intent: {intent}")
            return A2AResponse(request_id=request_id, ok=True, result=result).to_dict()
        except Exception as exc:  # noqa: BLE001
            return A2AResponse(
                request_id=request_id, ok=False, error=str(exc)
            ).to_dict()

    def _post_task(self, payload: dict, idempotency_key: str | None) -> dict:
        if "task_id" in payload:
            task_id = payload["task_id"]
        else:
            existing = {t.task_id for t in self._backend.list_tasks()}
            task_id = self._id_provider.generate(existing)
        task = TaskRecord(
            task_id=task_id,
            task_type=payload["task_type"],
            label=payload["label"],
            priority=int(payload.get("priority", 5)),
            notes=list(payload.get("notes", [])),
        )
        self._backend.create_task(task)
        event = self._append_event(
            event_type="task_posted",
            task_id=task.task_id,
            agent_id=None,
            from_status=None,
            to_status=task.status,
            payload={"task_type": task.task_type},
            idempotency_key=idempotency_key,
        )
        return {"task": task.to_dict(), "event": event.to_dict()}

    def _update_task(self, payload: dict, idempotency_key: str | None) -> dict:
        task_id = payload["task_id"]
        task = self._backend.get_task(task_id)
        if not task:
            raise KeyError(f"Task not found: {task_id}")
        raw_to_status = payload["to_status"]
        try:
            to_status: TaskStatus | str = TaskStatus(raw_to_status)
        except ValueError:
            to_status = raw_to_status
        profile = self._profile_for_task_type(task.task_type)
        validate_transition(
            profile, task.status, to_status, custom_profiles=self._custom_profiles
        )
        from_status = task.status
        task.status = to_status
        task.label = payload.get("label", task.label)
        task.assigned_to = payload.get("assigned_to", task.assigned_to)
        task.output = payload.get("output", task.output)
        if "notes_append" in payload:
            task.notes.append(payload["notes_append"])
        task.context_snapshot_hash = payload.get(
            "context_snapshot_hash", task.context_snapshot_hash
        )
        task.updated_at = utc_now()
        if task.output is not None:
            if isinstance(task.output, dict):
                import json

                task.output = json.dumps(task.output)
            else:
                task.output = str(task.output)

        self._backend.update_task(task)

        if task.assigned_to:
            agent = self._backend.get_agent(task.assigned_to)
            if agent:
                _update_agent_status(
                    agent,
                    task,
                    from_status=from_status,
                    to_status=to_status,
                    explicitly_assigned="assigned_to" in payload,
                    custom_terminal_statuses=self._terminal_statuses_for_profile(
                        profile
                    ),
                )
                agent.last_seen_at = utc_now()
                self._backend.upsert_agent(agent)

        if not isinstance(to_status, TaskStatus):
            event_type = "task_completed"
        elif not isinstance(from_status, TaskStatus):
            if to_status == TaskStatus.IN_PROGRESS:
                event_type = "task_assigned"
            elif to_status == TaskStatus.STALE:
                event_type = "task_stale"
            elif to_status == TaskStatus.HUMAN_REVIEW:
                event_type = "task_failed"
            elif to_status == TaskStatus.UNASSIGNED:
                event_type = "task_reassigned"
            else:
                event_type = "task_completed"
        else:
            event_type = _event_for_transition(from_status, to_status)

        event = self._append_event(
            event_type=event_type,
            task_id=task.task_id,
            agent_id=task.assigned_to,
            from_status=from_status,
            to_status=to_status,
            payload={"profile": profile},
            idempotency_key=idempotency_key,
        )
        return {"task": task.to_dict(), "event": event.to_dict()}

    def _get_task(self, payload: dict) -> dict:
        task = self._backend.get_task(payload["task_id"])
        if not task:
            raise KeyError(f"Task not found: {payload['task_id']}")
        return {"task": task.to_dict()}

    def _all_terminal_statuses(self) -> list[str]:
        terminals: set[str] = {s.value for s in STANDARD_TERMINAL_STATUSES}
        for custom_terminals in self._custom_terminal_statuses.values():
            terminals.update(custom_terminals)
        return sorted(terminals)

    def _get_full_state(self) -> dict:
        tasks = [task.to_dict() for task in self._backend.list_tasks()]
        agents = [agent.to_dict() for agent in self._backend.list_agents()]
        data = self._backend.list_data()
        return {
            "tasks": tasks,
            "agents": agents,
            "data": data,
            "_terminal_statuses": self._all_terminal_statuses(),
        }

    def _register_agent(self, payload: dict) -> dict:
        required = {"agent_id", "name", "url", "version", "capabilities", "description"}
        missing = required - payload.keys()
        if missing:
            raise ValueError(f"Missing AgentCard fields: {sorted(missing)}")
        raw_status = payload.get("status", AgentStatus.IDLE.value)
        agent = AgentRecord(
            agent_id=payload["agent_id"],
            name=payload["name"],
            status=(
                AgentStatus(raw_status) if isinstance(raw_status, str) else raw_status
            ),
            capabilities=list(payload["capabilities"]),
            a2a_url=payload["url"],
            agent_card=dict(payload),
            current_task_id=payload.get("current_task_id"),
            version=payload["version"],
        )
        self._backend.upsert_agent(agent)
        return {"agent": agent.to_dict()}

    def _post_agent_heartbeat(self, payload: dict, idempotency_key: str | None) -> dict:
        agent = self._backend.get_agent(payload["agent_id"])
        if not agent:
            raise KeyError(f"Agent not found: {payload['agent_id']}")
        agent.last_seen_at = utc_now()
        self._backend.upsert_agent(agent)

        task_id = payload.get("task_id")
        updated_task = None
        event = None
        if task_id:
            task = self._backend.get_task(task_id)
            if not task:
                raise KeyError(f"Task not found: {task_id}")
            task.heartbeat_at = utc_now()
            task.updated_at = utc_now()
            self._backend.update_task(task)
            updated_task = task.to_dict()
            event = self._append_event(
                event_type="task_heartbeat",
                task_id=task.task_id,
                agent_id=agent.agent_id,
                from_status=task.status,
                to_status=task.status,
                payload={"heartbeat_at": task.heartbeat_at.isoformat()},
                idempotency_key=idempotency_key,
            ).to_dict()
        return {"agent": agent.to_dict(), "task": updated_task, "event": event}

    def _stream_events(self, payload: dict) -> dict:
        since = int(payload.get("since_sequence", 0))
        events = [event.to_dict() for event in self._backend.list_events_since(since)]
        return {"events": events}

    def _put_data(self, payload: dict) -> dict:
        key = payload["key"]
        value = payload["value"]
        self._backend.put_data(key, value)
        return {"key": key}

    def _get_data(self, payload: dict) -> dict:
        key = payload["key"]
        value = self._backend.get_data(key)
        return {"key": key, "value": value}

    def _get_task_history(self, payload: dict) -> dict:
        task_id = payload["task_id"]
        events = [e.to_dict() for e in self._backend.list_events_for_task(task_id)]
        return {"task_id": task_id, "events": events}

    def _get_agent_activity(self, payload: dict) -> dict:
        agent_id = payload["agent_id"]
        events = [e.to_dict() for e in self._backend.list_events_for_agent(agent_id)]
        return {"agent_id": agent_id, "events": events}

    def _worker_post_result(self, payload: dict, idempotency_key: str | None) -> dict:
        task = self._backend.get_task(payload["task_id"])
        if not task:
            raise KeyError(f"Task not found: {payload['task_id']}")
        if task.status != TaskStatus.IN_PROGRESS:
            raise TransitionError(
                f"Task {task.task_id} must be IN_PROGRESS to post result"
            )

        profile = self._profile_for_task_type(task.task_type)
        target = (
            TaskStatus.PENDING_REVIEW
            if profile == "review_required"
            else TaskStatus.COMPLETE
        )
        task.output = payload["output"]
        task.updated_at = utc_now()
        task.status = target
        self._backend.update_task(task)
        if task.assigned_to:
            agent = self._backend.get_agent(task.assigned_to)
            if agent:
                agent.status = AgentStatus.IDLE
                agent.current_task_id = None
                agent.last_seen_at = utc_now()
                self._backend.upsert_agent(agent)
        event = self._append_event(
            event_type="task_completed",
            task_id=task.task_id,
            agent_id=payload.get("agent_id"),
            from_status=TaskStatus.IN_PROGRESS,
            to_status=target,
            payload={"profile": profile},
            idempotency_key=idempotency_key,
        )
        return {"task": task.to_dict(), "event": event.to_dict()}
