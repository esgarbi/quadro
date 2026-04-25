from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .a2a.dispatch import LocalA2ANetwork
from .board.backends.sqlite import SqliteBoardBackend
from .board.board import QuadroBoard
from .board.client import BoardClient
from .runner import RunLoop

logger = logging.getLogger(__name__)


class QuadroRuntime:
    """Framework-agnostic host for a runnable Quadro application.

    The runtime owns board/client setup, run-loop configuration, seed data, and
    shutdown hooks. Pipeline adapters still own framework-specific worker and
    chief composition.
    """

    def __init__(self, board: QuadroBoard) -> None:
        self.board = board
        self.client: BoardClient = board.client()
        self._done_predicate: Callable[[dict], bool] | None = None
        self._cycle_callback: Callable[[dict, int], None] | None = None
        self._complete_callback: Callable[[dict], None] | None = None
        self._poll_interval = 3.0
        self._ombudsman_interval = 30.0
        self._max_cycles = 500
        self._shutdown_hooks: list[Callable[[], None]] = []

    @classmethod
    def sqlite(
        cls,
        db_path: str | Path,
        *,
        profile_resolver: dict[str, str] | None = None,
        custom_profiles: dict[str, Any] | None = None,
        network: Any | None = None,
    ) -> QuadroRuntime:
        """Create a runtime backed by a SQLite board."""
        active_network = network or LocalA2ANetwork()
        board = QuadroBoard(
            SqliteBoardBackend(str(db_path)),
            profile_resolver=profile_resolver,
            custom_profiles=custom_profiles,
            network=active_network,
        )
        return cls(board)

    def put_data(self, key: str, value: Any) -> QuadroRuntime:
        self.client.put_data(key, value)
        return self

    @property
    def network(self) -> Any:
        return self.client.network

    def done_when(self, predicate: Callable[[dict], bool]) -> QuadroRuntime:
        self._done_predicate = predicate
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

    def max_cycles(self, n: int) -> QuadroRuntime:
        self._max_cycles = n
        return self

    def add_shutdown_hook(self, hook: Callable[[], None]) -> QuadroRuntime:
        self._shutdown_hooks.append(hook)
        return self

    def manage(self, resource: Any, stop_method: str = "stop") -> Any:
        """Register a resource cleanup method and return the resource."""
        self.add_shutdown_hook(getattr(resource, stop_method))
        return resource

    def run(self, built_pipeline: Any) -> dict:
        """Run a built pipeline-shaped object through Quadro's RunLoop."""
        if self._done_predicate is None:
            raise ValueError("QuadroRuntime requires .done_when(predicate) before .run()")

        builder = (
            RunLoop(self.board, built_pipeline.chief)
            .done_when(self._done_predicate)
            .poll_every(self._poll_interval)
            .ombudsman_every(self._ombudsman_interval)
            .max_cycles(self._max_cycles)
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
