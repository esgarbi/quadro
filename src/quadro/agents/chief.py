from __future__ import annotations

import asyncio
import concurrent.futures
import threading
import time as _time
from collections.abc import Callable
from datetime import datetime, timezone
from uuid import uuid4

from ..a2a.contracts import A2ARequest, A2AResponse
from ..a2a.dispatch import LocalA2ANetwork
from ..board.client import BoardClient
from ..board.records import AgentStatus
from .hydration import hydrate_chief_context


def _task_status_snapshot(state: dict) -> frozenset[tuple[str, str, str | None]]:
    """Hashable snapshot of (task_id, status, assigned_to) for cheap before/after comparison."""
    return frozenset(
        (t["task_id"], t["status"], t.get("assigned_to"))
        for t in state.get("tasks", [])
    )


class ChiefAgent:
    def __init__(
        self,
        *,
        network: LocalA2ANetwork,
        board_url: str,
        chief_url: str | None = None,
        policy: Callable[[dict], None] | None = None,
    ) -> None:
        self.network = network
        self.board_url = board_url
        self._chief_url = chief_url
        self._policy = policy
        self._lock = threading.Lock()
        self._decision_running: bool = False
        self._pending_wake: bool = False

        self._telemetry_lock = threading.Lock()
        self._telem: dict = {
            "status": "sleeping",
            "last_woke_at": None,
            "last_slept_at": None,
            "last_cycle_duration_ms": None,
            "last_trigger": None,
            "cycles_run": 0,
            "consecutive_noops": 0,
            "recent_durations_ms": [],
        }

        if chief_url:
            network.register_endpoint(chief_url, self.handle_wake_request)

    # ── Reactive wakeup ────────────────────────────────────────────────────────

    def handle_wake_request(self, envelope: dict) -> dict:
        """A2A endpoint. Workers call this after completing their board writes."""
        request_id = envelope.get("request_id", uuid4().hex[:12])
        self.wake(trigger="worker")
        return A2AResponse(request_id=request_id, ok=True, result={}).to_dict()

    def wake(self, trigger: str = "worker") -> int:
        """
        Reactive wakeup.  Reads the full board, makes all pending decisions in
        a single pass, dispatches fire-and-forget.  Concurrent calls queue a
        _pending_wake flag rather than running a second concurrent cycle.
        Returns the number of decision cycles run.

        trigger: "worker" | "ombudsman" | "seed" — why this wake was requested.
        """
        with self._lock:
            if self._decision_running:
                self._pending_wake = True
                return 0
            self._decision_running = True

        cycles = 0
        try:
            self._run_decision_cycle(trigger=trigger)
            cycles += 1
            while True:
                with self._lock:
                    if not self._pending_wake:
                        break
                    self._pending_wake = False
                self._run_decision_cycle(trigger="worker")
                cycles += 1
        finally:
            with self._lock:
                self._decision_running = False
        return cycles

    def nudge(self, trigger: str = "seed") -> int:
        """Delegates to wake(). trigger defaults to 'seed' for explicit nudges."""
        return self.wake(trigger=trigger)

    @property
    def cycles_run(self) -> int:
        return self._telem.get("cycles_run", 0)

    @cycles_run.setter
    def cycles_run(self, value: int) -> None:
        self._telem["cycles_run"] = value

    # ── Decision cycle ─────────────────────────────────────────────────────────

    def _run_decision_cycle(self, trigger: str = "worker") -> None:
        """Single board read → hydration → policy → default routing."""
        t0 = _time.monotonic()
        woke_at = datetime.now(timezone.utc).isoformat()

        self._write_telemetry(status="thinking", last_woke_at=woke_at, trigger=trigger)

        state = self._get_state()
        chief_context = hydrate_chief_context(state, None)

        policy_changed_board = False
        if self._policy is not None:
            pre_policy_tasks = _task_status_snapshot(state)
            raw = self._policy(chief_context)
            if asyncio.iscoroutine(raw):
                self._write_telemetry(status="acting")
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                    pool.submit(asyncio.run, raw).result()
            state = self._get_state()
            chief_context = hydrate_chief_context(state, None)
            policy_changed_board = _task_status_snapshot(state) != pre_policy_tasks

        self._write_telemetry(status="acting")

        routing_actions = self._apply_default_routing(chief_context["payload"])
        actions_taken = routing_actions + (1 if policy_changed_board else 0)

        duration_ms = int((_time.monotonic() - t0) * 1000)
        slept_at = datetime.now(timezone.utc).isoformat()
        self._write_telemetry(
            status="sleeping",
            last_slept_at=slept_at,
            duration_ms=duration_ms,
            actions_taken=actions_taken,
        )

    def _apply_default_routing(self, payload: dict) -> int:
        """
        Generic fallback routing for standard lifecycle profiles.

        Returns the number of actions taken (board mutations + dispatches).
        Silently skips tasks whose profiles don't support standard transitions
        (e.g. custom profiles where UNASSIGNED → IN_PROGRESS is not valid).
        """
        tasks = sorted(payload["tasks"], key=lambda t: t.get("priority", 5))
        agents = payload["agents"]
        actions = 0

        for task in tasks:
            status = task["status"]

            if status == "UNASSIGNED":
                capability = task["task_type"]
                worker = self._find_idle_agent(agents, capability)
                if not worker:
                    continue
                try:
                    self._request_board(
                        "board.update_task",
                        {
                            "task_id": task["task_id"],
                            "to_status": "IN_PROGRESS",
                            "assigned_to": worker["agent_id"],
                        },
                    )
                except RuntimeError:
                    continue
                self._dispatch_worker(worker, task["task_id"])
                actions += 1

            elif status == "PENDING_REVIEW":
                reviewer = self._find_idle_agent(agents, "review")
                if not reviewer:
                    continue
                self._request_board(
                    "board.update_task",
                    {
                        "task_id": task["task_id"],
                        "to_status": "IN_PROGRESS",
                        "assigned_to": reviewer["agent_id"],
                    },
                )
                self._dispatch_worker(reviewer, task["task_id"])
                actions += 1

            elif status == "REVISION_NEEDED":
                capability = task["task_type"]
                writer = self._find_idle_agent(agents, capability)
                if not writer:
                    continue
                self._request_board(
                    "board.update_task",
                    {
                        "task_id": task["task_id"],
                        "to_status": "IN_PROGRESS",
                        "assigned_to": writer["agent_id"],
                    },
                )
                self._dispatch_worker(writer, task["task_id"])
                actions += 1

            elif status == "APPROVED":
                self._request_board(
                    "board.update_task",
                    {"task_id": task["task_id"], "to_status": "COMPLETE"},
                )
                actions += 1

            elif status == "STALE":
                self._request_board(
                    "board.update_task",
                    {"task_id": task["task_id"], "to_status": "UNASSIGNED"},
                )
                actions += 1

        return actions

    # ── Telemetry ──────────────────────────────────────────────────────────────

    def _write_telemetry(
        self,
        *,
        status: str | None = None,
        last_woke_at: str | None = None,
        last_slept_at: str | None = None,
        trigger: str | None = None,
        duration_ms: int | None = None,
        actions_taken: int | None = None,
    ) -> None:
        """Update in-memory telemetry and persist to board data (best-effort)."""
        with self._telemetry_lock:
            t = self._telem
            if status is not None:
                t["status"] = status
            if last_woke_at is not None:
                t["last_woke_at"] = last_woke_at
            if last_slept_at is not None:
                t["last_slept_at"] = last_slept_at
            if trigger is not None:
                t["last_trigger"] = trigger
            if duration_ms is not None:
                t["last_cycle_duration_ms"] = duration_ms
                t["cycles_run"] += 1
                buf = t["recent_durations_ms"]
                buf.append(duration_ms)
                if len(buf) > 20:
                    buf.pop(0)
            if actions_taken is not None:
                if actions_taken == 0 and t.get("last_trigger") == "worker":
                    t["consecutive_noops"] += 1
                elif actions_taken > 0:
                    t["consecutive_noops"] = 0
            snapshot = dict(t)

        try:
            self._request_board(
                "board.put_data",
                {
                    "key": "_chief_telemetry",
                    "value": snapshot,
                },
            )
        except Exception:  # noqa: BLE001
            pass

    # ── Board helpers ──────────────────────────────────────────────────────────

    def _request_board(self, intent: str, payload: dict) -> dict:
        response = self.network.request(
            self.board_url,
            A2ARequest(intent=intent, payload=payload).to_dict(),
        )
        if not response["ok"]:
            raise RuntimeError(response["error"])
        return response["result"]

    def _get_state(self) -> dict:
        return self._request_board("board.get_full_state", {})

    def _find_idle_agent(self, agents: list[dict], capability: str) -> dict | None:
        for agent in agents:
            if (
                capability in agent["capabilities"]
                and agent["status"] == AgentStatus.IDLE.value
            ):
                return agent
        return None

    def _dispatch_worker(self, worker: dict, task_id: str) -> None:
        url = worker.get("a2a_url")
        if not url:
            raise RuntimeError(
                f"Agent {worker.get('agent_id')} has no a2a_url in board registry"
            )

        import logging as _logging

        _log = _logging.getLogger(__name__)

        def _fire() -> None:
            try:
                # Fire-and-forget. Worker runs independently and wakes the Chief on completion.
                self.network.request(
                    url,
                    A2ARequest(
                        intent="worker.execute_task", payload={"task_id": task_id}
                    ).to_dict(),
                )
            except Exception as exc:  # noqa: BLE001
                _log.warning("Dispatch error task=%s url=%s: %s", task_id[:8], url, exc)

        t = threading.Thread(target=_fire, daemon=True)
        t.start()

    # ── Backward-compat shim ───────────────────────────────────────────────────

    @property
    def max_concurrent_loops(self) -> int:
        """Retained for backward compatibility. Reactive model never runs concurrent cycles."""
        return 1

    # ── Builder factory ────────────────────────────────────────────────────────

    @classmethod
    def builder(cls, board_client: "BoardClient") -> "ChiefAgentBuilder":
        return ChiefAgentBuilder(board_client)


class ChiefAgentBuilder:
    """
    Fluent builder for ChiefAgent.

    Usage:
        chief = (
            ChiefAgent.builder(board_client)
            .at("a2a://chief")
            .policy(chief_policy)
            .build()
        )
    """

    def __init__(self, board_client: "BoardClient") -> None:
        self._board_client = board_client
        self._chief_url: str | None = None
        self._policy = None

    def at(self, chief_url: str) -> "ChiefAgentBuilder":
        """Set the chief's A2A URL (enables reactive wakeup endpoint)."""
        self._chief_url = chief_url
        return self

    def policy(self, policy_fn) -> "ChiefAgentBuilder":
        """Set the policy callback (sync or async)."""
        self._policy = policy_fn
        return self

    def build(self) -> "ChiefAgent":
        return ChiefAgent(
            network=self._board_client._network,
            board_url=self._board_client._board_url,
            chief_url=self._chief_url,
            policy=self._policy,
        )
