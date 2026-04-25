from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import timedelta
from typing import Any

from .a2a.dispatch import LocalA2ANetwork
from .board.backends.base import BoardBackend
from .board.board import QuadroBoard
from .board.client import BoardClient
from .runner import RunLoop
from .sponsor.types import Sponsor

logger = logging.getLogger(__name__)


class QuadroRuntime:
    """Framework-agnostic host for a runnable Quadro application.

    The runtime owns board/client setup, run-loop configuration, seed data,
    and shutdown hooks. Pipeline adapters still own framework-specific
    worker and chief composition.

    Lifetime is governed by a :class:`~quadro.sponsor.Sponsor`, installed
    via :meth:`sponsor`. The Sponsor is the single source of truth for
    "should the runtime keep working?" — the old ``done_when`` predicate
    and ``max_cycles`` safety net are replaced by composable Sponsors. See
    ``docs/design/sponsor.md`` for the full model.
    """

    def __init__(self, backend: BoardBackend, *, network: Any | None = None) -> None:
        self._backend = backend
        self._network = network or LocalA2ANetwork()
        self._profile_resolver: dict[str, str] | None = None
        self._custom_profiles: dict[str, Any] | None = None
        self._board: QuadroBoard | None = None
        self._client: BoardClient | None = None
        self._sponsor: Sponsor | None = None
        self._cycle_callback: Callable[[dict, int], None] | None = None
        self._complete_callback: Callable[[dict], None] | None = None
        self._poll_interval = 3.0
        self._ombudsman_interval = 30.0
        self._drain_max_duration = timedelta(minutes=5)
        self._shutdown_hooks: list[Callable[[], None]] = []

    def _assert_not_started(self) -> None:
        if self._board is not None:
            raise RuntimeError(
                "QuadroRuntime configuration cannot change after board creation"
            )

    def _ensure_board(self) -> QuadroBoard:
        if self._board is None:
            self._board = QuadroBoard(
                self._backend,
                profile_resolver=self._profile_resolver,
                custom_profiles=self._custom_profiles,
                network=self._network,
            )
            self._client = self._board.client()
        return self._board

    def with_profiles(
        self,
        profile_resolver: dict[str, str] | None = None,
        custom_profiles: dict[str, Any] | None = None,
    ) -> QuadroRuntime:
        self._assert_not_started()
        self._profile_resolver = profile_resolver
        self._custom_profiles = custom_profiles
        return self

    def with_network(self, network: Any) -> QuadroRuntime:
        self._assert_not_started()
        self._network = network
        return self

    def put_data(self, key: str, value: Any) -> QuadroRuntime:
        self.client.put_data(key, value)
        return self

    @property
    def board(self) -> QuadroBoard:
        return self._ensure_board()

    @property
    def client(self) -> BoardClient:
        self._ensure_board()
        if self._client is None:
            raise RuntimeError("QuadroRuntime failed to create a board client")
        return self._client

    @property
    def network(self) -> Any:
        return self._network

    # ── Lifetime configuration ────────────────────────────────────────────────

    def sponsor(self, sponsor: Sponsor) -> QuadroRuntime:
        """Install the Sponsor that governs this runtime's lifetime.

        Required before :meth:`run`. Replaces the old ``done_when`` and
        ``max_cycles`` knobs — goal-based termination moves into a
        :class:`~quadro.sponsor.GoalSponsor`, tick/time/token limits into
        their respective budget Sponsors, and composition uses
        :class:`~quadro.sponsor.AllOf` / :class:`~quadro.sponsor.AnyOf` /
        :class:`~quadro.sponsor.Priority`.
        """
        self._sponsor = sponsor
        return self

    def on_cycle(self, callback: Callable[[dict, int], None]) -> QuadroRuntime:
        self._cycle_callback = callback
        return self

    def on_complete(self, callback: Callable[[dict], None]) -> QuadroRuntime:
        self._complete_callback = callback
        return self

    def poll_every(self, seconds: float) -> QuadroRuntime:
        self._poll_interval = seconds
        return self

    def ombudsman_every(self, seconds: float) -> QuadroRuntime:
        self._ombudsman_interval = seconds
        return self

    def drain_max_duration(self, td: timedelta) -> QuadroRuntime:
        """Override the default 5-minute fallback drain deadline.

        When a Sponsor returns ``Drain(deadline=None)``, the runtime uses
        this value to compute the implicit drain deadline.
        """
        self._drain_max_duration = td
        return self

    def add_shutdown_hook(self, hook: Callable[[], None]) -> QuadroRuntime:
        self._shutdown_hooks.append(hook)
        return self

    def manage(self, resource: Any, stop_method: str = "stop") -> Any:
        """Register a resource cleanup method and return the resource."""
        self.add_shutdown_hook(getattr(resource, stop_method))
        return resource

    # ── Run ───────────────────────────────────────────────────────────────────

    def run(self, built_pipeline: Any) -> dict:
        """Run a built pipeline-shaped object through Quadro's RunLoop."""
        if self._sponsor is None:
            raise ValueError(
                "QuadroRuntime requires .sponsor(sponsor) before .run(). "
                "Use GoalSponsor(predicate) as a drop-in replacement for the "
                "old .done_when(predicate)."
            )

        builder = (
            RunLoop(self.board, built_pipeline.chief)
            .sponsor(self._sponsor)
            .poll_every(self._poll_interval)
            .ombudsman_every(self._ombudsman_interval)
            .drain_max_duration(self._drain_max_duration)
        )

        ombudsman = getattr(built_pipeline, "ombudsman", None)
        if ombudsman is not None:
            builder = builder.ombudsman(ombudsman)
        if self._cycle_callback is not None:
            builder = builder.on_cycle(self._cycle_callback)
        if self._complete_callback is not None:
            builder = builder.on_complete(self._complete_callback)

        try:
            return builder.run()
        finally:
            self.shutdown()

    def shutdown(self) -> None:
        while self._shutdown_hooks:
            hook = self._shutdown_hooks.pop()
            try:
                hook()
            except Exception as exc:  # noqa: BLE001
                logger.warning("QuadroRuntime shutdown hook error: %s", exc)
