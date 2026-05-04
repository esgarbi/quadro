"""
Persisted runner state for sagas in flight.

A ``SagaState`` is the mutable counterpart to a frozen ``Saga``. It
holds the program counter (the next step to execute), outputs of
completed steps, and any cross-cutting state the runner needs to
resume after a worker invocation ends.

The state is persisted to the Board between worker invocations under
the data-store key ``_saga:{task_id}``. Re-dispatching the same task
to a new worker — for any reason — produces an identical resumption
because the worker rehydrates state from the Board, never from
in-memory continuations.

Milestone A uses only ``saga_name``, ``pc``, and ``completed_steps``.
The remaining fields are declared here so subsequent milestones can
populate them without a migration. Round-trip serialization
(``to_board_data`` / ``from_board_data``) is JSON-friendly: every
field's value is a JSON primitive, a JSON-serializable dict, or a
JSON-serializable list. Deterministic steps are required to return
JSON-compatible values; this constraint is relaxed in milestone B
where ``reason`` steps add pydantic-aware serialization.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


def _to_json_compatible(value: Any) -> Any:
    """Best-effort conversion of a step output to a JSON-compatible value.

    Handles three cases:

    1. **Top-level pydantic ``BaseModel``** — duck-typed via ``model_dump``
       (so this module stays free of a hard pydantic import; quadro
       core ships with no runtime deps).
    2. **Dict / list / tuple containers** — recurses into members so
       pydantic instances nested inside a dict or list (e.g. a
       deterministic step's ``{"brief": ArticleBrief(...), "research":
       ResearchOutput(...)}`` payload) are dumped too. Without the
       recursion, the top-level dict passes through unchanged and the
       sqlite backend's ``json.dumps`` raises on the nested
       ``BaseModel`` — exactly the "Object of type X is not JSON
       serializable" warning this helper was meant to prevent.
    3. **Everything else** — pass through. JSON primitives (str / int /
       float / bool / None) are already serializable; exotic types
       (datetimes, Paths, etc.) are the caller's responsibility.
    """
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        try:
            return model_dump(mode="json")
        except TypeError:
            return model_dump()
    if isinstance(value, dict):
        return {k: _to_json_compatible(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_json_compatible(v) for v in value]
    return value


@dataclass
class SagaState:
    """Mutable runner state for a single saga in flight.

    Read by the runner at the start of every ``run_stage`` invocation
    and written back after every step completion. The persistence key
    convention is ``_saga:{task_id}``.
    """

    saga_name: str
    pc: str | None  # next step name, or None if saga is complete
    idempotency_key: str | None = None
    completed_steps: dict[str, Any] = field(default_factory=dict)
    evidence: dict[str, Any] = field(default_factory=dict)  # used in milestone C+
    stamps: list[dict[str, Any]] = field(default_factory=list)  # used in milestone C+
    fork_children: dict[str, str] = field(default_factory=dict)  # used in milestone F+
    waiting_for: str | None = None  # used in milestone F+
    started_at: str | None = None
    sla_deadline: str | None = None  # used in milestone C+
    # Milestone D — compensation rollback attempt log. Each entry is
    # a dict with ``step``, ``outcome`` ("ok" | "failed"),
    # ``duration_ms``, ``timestamp``, and (when outcome="failed")
    # ``error_type`` / ``error_message``. Written by
    # ``QuadroSagaRuntime._apply_compensations`` in reverse completion
    # order; read on resume so a crash mid-rollback doesn't re-invoke
    # a compensation that already completed cleanly.
    compensations_run: list[dict[str, Any]] = field(default_factory=list)
    # Milestone G — per-step reasoner selection audit trail. Maps
    # reason-step name to the ``reasoner_id`` of whichever registered
    # ``Reasoner`` ran it. Populated by
    # ``QuadroSagaRuntime._run_reason`` immediately after each reason
    # dispatch returns. Same flat-dict-of-strings shape as
    # ``compensations_run`` — round-trips through the existing
    # ``_to_json_compatible`` pipeline without changes. Consumers
    # (flight-plan JSON, audit queries) read this map to observe which
    # reasoner ran which step in a polyglot saga.
    reasoners_by_step: dict[str, str] = field(default_factory=dict)
    # Milestone E — completed branch state for parallel steps. The public
    # step output remains ``completed_steps[parallel_name]``; this sibling
    # field preserves each successful branch's internal completed steps so
    # rollback can descend into branch-local compensations.
    branch_states: dict[str, dict[str, SagaState]] = field(default_factory=dict)

    @classmethod
    def from_board_data(cls, data: dict | None) -> SagaState | None:
        """Reconstruct a state from the dict stored in board data, or
        return ``None`` if no state has been persisted yet."""
        if not data:
            return None
        branch_states: dict[str, dict[str, SagaState]] = {}
        for parallel_name, branches in dict(data.get("branch_states") or {}).items():
            if not isinstance(branches, dict):
                continue
            branch_states[str(parallel_name)] = {}
            for branch_name, branch_data in branches.items():
                if isinstance(branch_data, dict):
                    branch_state = cls.from_board_data(branch_data)
                    if branch_state is not None:
                        branch_states[str(parallel_name)][str(branch_name)] = (
                            branch_state
                        )

        return cls(
            saga_name=data["saga_name"],
            pc=data.get("pc"),
            idempotency_key=data.get("idempotency_key"),
            completed_steps=dict(data.get("completed_steps") or {}),
            evidence=dict(data.get("evidence") or {}),
            stamps=list(data.get("stamps") or []),
            fork_children=dict(data.get("fork_children") or {}),
            waiting_for=data.get("waiting_for"),
            started_at=data.get("started_at"),
            sla_deadline=data.get("sla_deadline"),
            compensations_run=list(data.get("compensations_run") or []),
            reasoners_by_step=dict(data.get("reasoners_by_step") or {}),
            branch_states=branch_states,
        )

    def to_board_data(self) -> dict[str, Any]:
        """Serialize to a JSON-compatible dict for storage via
        ``board.put_data``. The result is round-trippable through
        ``from_board_data`` — with the caveat that pydantic outputs of
        reason steps are stored as their ``model_dump()`` dict form and
        must be re-materialized by the caller (the saga runtime plugin
        does this in ``_load_or_init_state`` using each step's declared
        schema)."""
        return {
            "saga_name": self.saga_name,
            "pc": self.pc,
            "idempotency_key": self.idempotency_key,
            "completed_steps": {
                name: _to_json_compatible(value)
                for name, value in self.completed_steps.items()
            },
            "evidence": {
                name: _to_json_compatible(value)
                for name, value in self.evidence.items()
            },
            "stamps": list(self.stamps),
            "fork_children": dict(self.fork_children),
            "waiting_for": self.waiting_for,
            "started_at": self.started_at,
            "sla_deadline": self.sla_deadline,
            "compensations_run": list(self.compensations_run),
            "reasoners_by_step": dict(self.reasoners_by_step),
            "branch_states": {
                parallel_name: {
                    branch_name: branch_state.to_board_data()
                    for branch_name, branch_state in branches.items()
                }
                for parallel_name, branches in self.branch_states.items()
            },
        }

    def is_complete(self) -> bool:
        """Convenience: a saga is complete when its program counter is None."""
        return self.pc is None
