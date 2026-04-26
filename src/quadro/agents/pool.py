from __future__ import annotations

import logging
import warnings
from collections.abc import Callable
from typing import TYPE_CHECKING

from ..board.client import BoardClient
from .worker import WorkerAgent

if TYPE_CHECKING:
    from ..ombudsman import Ombudsman

logger = logging.getLogger(__name__)

_DEFAULT_MAX_WORKING_MINUTES: float = 30.0


class WorkerPool:
    """
    Fluent builder for a pool of WorkerAgents grouped by capability.

    Creates N workers per capability, registers them all, and exposes
    the registry and working_statuses needed by downstream components.

    Usage:
        pool = (
            WorkerPool(bc)
            .workers(3)           # 3 agents per capability (parallelism)
            .capacity(8)          # max 8 tasks active on board at once (throughput)
            .wakes("a2a://chief")
            .add("ideation", run_ideation, active_status="ideating")
            .add("research", run_research, active_status="researching")
            .add("writing",  run_writing,  active_status="writing")
            .add("review",   run_review,   active_status="reviewing")
            .build()
        )

    workers(n)  — how many agents exist per capability (parallelism ceiling)
    capacity(n) — how many tasks may be active on the board at once (throughput control)

    If .capacity() is not called, capacity defaults to workers × len(capabilities).
    """

    def __init__(self, board_client: BoardClient) -> None:
        self._bc = board_client
        self._workers_per_cap: int = 1
        self._capacity_override: int | None = None
        self._chief_url: str | None = None
        self._specs: list[tuple[str, Callable, bool, str | None, int | None]] = []
        self._registry: dict[str, list[tuple[str, str]]] = {}
        self._working_statuses: set[str] = set()
        self._status_timeouts: dict[str, int] = {}
        self._agents: list[WorkerAgent] = []
        self._built: bool = False

    def workers(self, n: int) -> WorkerPool:
        """Set the number of worker agents created per capability."""
        self._workers_per_cap = n
        return self

    def pool_size(self, n: int) -> WorkerPool:
        """Deprecated: use .workers(n) instead."""
        warnings.warn(
            "WorkerPool.pool_size() is deprecated, use .workers() instead",
            DeprecationWarning,
            stacklevel=2,
        )
        return self.workers(n)

    def capacity(self, n: int | None = None) -> "WorkerPool | int":
        """
        Dual-purpose: called with n sets the pipeline capacity and returns self
        for chaining. Called without args returns the current capacity value.

        Default when not explicitly set = workers_per_cap × len(capabilities).
        """
        if n is None:
            if self._capacity_override is not None:
                return self._capacity_override
            return self._workers_per_cap * len(self._specs)
        self._capacity_override = n
        return self

    def wakes(self, chief_url: str) -> WorkerPool:
        self._chief_url = chief_url
        return self

    def add(
        self,
        capability: str,
        execute_fn: Callable,
        *,
        reviewer: bool = False,
        active_status: str | None = None,
        max_working_time: float | None = None,
        stale_timeout_seconds: int | None = None,
    ) -> WorkerPool:
        resolved_seconds: int | None = None
        if max_working_time is not None:
            resolved_seconds = int(max_working_time * 60)
        elif stale_timeout_seconds is not None:
            warnings.warn(
                "stale_timeout_seconds is deprecated — use max_working_time (minutes) instead",
                DeprecationWarning,
                stacklevel=2,
            )
            resolved_seconds = stale_timeout_seconds
        self._specs.append(
            (capability, execute_fn, reviewer, active_status, resolved_seconds)
        )
        return self

    def build(self) -> WorkerPool:
        if self._built:
            return self
        for (
            capability,
            execute_fn,
            reviewer_mode,
            active_status,
            stale_timeout_seconds,
        ) in self._specs:
            pool: list[tuple[str, str]] = []
            for i in range(1, self._workers_per_cap + 1):
                agent_id = f"{capability}_worker_{i}"
                url = f"a2a://workers/{capability}_{i}"
                b = (
                    WorkerAgent.builder(agent_id, self._bc)
                    .name(f"{capability.title()} Worker {i}")
                    .capability(capability)
                    .at(url)
                    .execute(execute_fn)
                )
                if reviewer_mode:
                    b = b.reviewer()
                if self._chief_url:
                    b = b.wakes(self._chief_url)
                worker = b.build()
                worker.register()
                logger.info("Registered: %s", worker.name)
                pool.append((agent_id, url))
                self._agents.append(worker)
            self._registry[capability] = pool
            if active_status:
                self._working_statuses.add(active_status)
                effective_timeout = stale_timeout_seconds
                if effective_timeout is None:
                    effective_timeout = int(_DEFAULT_MAX_WORKING_MINUTES * 60)
                self._status_timeouts[active_status] = effective_timeout
        self._built = True
        return self

    @property
    def registry(self) -> dict[str, list[tuple[str, str]]]:
        return dict(self._registry)

    @property
    def working_statuses(self) -> frozenset[str]:
        return frozenset(self._working_statuses)

    @property
    def status_timeouts(self) -> dict[str, int]:
        """
        Maps active_status → stale_timeout_seconds for capabilities that declared
        a custom timeout. Pass to Ombudsman(status_timeouts=pool.status_timeouts).
        Statuses not in this dict use the Ombudsman's global heartbeat_timeout_seconds.
        """
        return dict(self._status_timeouts)

    @property
    def agents(self) -> list[WorkerAgent]:
        return list(self._agents)

    @property
    def capabilities(self) -> frozenset[str]:
        return frozenset(self._registry.keys())

    def ombudsman(
        self,
        default_timeout_minutes: float | None = None,
    ) -> "Ombudsman":
        """
        Create an Ombudsman configured for this pool.

        Reads network and board_url from the pool's BoardClient.
        Uses the per-capability timeouts declared via max_working_time on .add().

        Args:
            default_timeout_minutes: Global fallback timeout in minutes for any
                working_status not covered by a per-capability timeout. Defaults
                to _DEFAULT_MAX_WORKING_MINUTES (30 minutes).

        Returns:
            A configured Ombudsman ready to pass to RunLoop.ombudsman().
        """
        from ..ombudsman import Ombudsman

        if not self._built:
            raise RuntimeError("WorkerPool.ombudsman() must be called after .build()")

        global_seconds = int(
            (default_timeout_minutes or _DEFAULT_MAX_WORKING_MINUTES) * 60
        )

        return Ombudsman(
            network=self._bc.network,
            board_url=self._bc.board_url,
            heartbeat_timeout_seconds=global_seconds,
            working_statuses=self.working_statuses,
            status_timeouts=self.status_timeouts,
        )
