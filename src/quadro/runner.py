from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any

from .agents.chief import ChiefAgent
from .board.client import BoardClient

logger = logging.getLogger(__name__)

_DEFAULT_POLL_INTERVAL = 3.0  # seconds; examples use 0.0 for speed
_DEFAULT_OMBUDSMAN_INTERVAL = 30.0  # seconds
_DEFAULT_MAX_CYCLES = 500


class RunLoop:
    """
    Runs a Quadro system until a completion predicate fires.

    Handles the loop mechanics every Quadro application needs:
    seeding, polling, ombudsman, and callbacks. The application
    provides only domain-specific predicates and callbacks.

    Usage:
        state = (
            RunLoop(board_client, chief)
            .done_when(lambda s: count_done(s) >= target)
            .on_cycle(log_status)
            .on_complete(print_summary)
            .run()
        )
    """

    def __init__(
        self, board_or_client: BoardClient | QuadroBoard, chief: ChiefAgent
    ) -> None:
        from .board.board import QuadroBoard as _QuadroBoard

        if isinstance(board_or_client, _QuadroBoard):
            self._board_client = board_or_client.client()
        else:
            self._board_client = board_or_client
        self._chief = chief
        self._done_predicate: Callable[[dict], bool] | None = None
        self._cycle_callback: Callable[[dict, int], None] | None = None
        self._complete_callback: Callable[[dict], None] | None = None
        self._poll_interval = _DEFAULT_POLL_INTERVAL
        self._ombudsman_interval = _DEFAULT_OMBUDSMAN_INTERVAL
        self._max_cycles = _DEFAULT_MAX_CYCLES
        self._ombudsman_instance = None

    def done_when(self, predicate: Callable[[dict], bool]) -> RunLoop:
        self._done_predicate = predicate
        return self

    def on_cycle(self, callback: Callable[[dict, int], None]) -> RunLoop:
        self._cycle_callback = callback
        return self

    def on_complete(self, callback: Callable[[dict], None]) -> RunLoop:
        self._complete_callback = callback
        return self

    def poll_every(self, seconds: float) -> RunLoop:
        self._poll_interval = seconds
        return self

    def ombudsman_every(self, seconds: float) -> RunLoop:
        self._ombudsman_interval = seconds
        return self

    def max_cycles(self, n: int) -> RunLoop:
        self._max_cycles = n
        return self

    def ombudsman(self, ombudsman_instance: Any) -> RunLoop:
        """Optional Ombudsman instance. nudge() is called alongside the safety-net chief nudge."""
        self._ombudsman_instance = ombudsman_instance
        return self

    def run(self) -> dict:
        if self._done_predicate is None:
            raise ValueError("RunLoop requires .done_when(predicate) before .run()")

        logger.debug("RunLoop: seeding chief")
        self._chief.nudge(trigger="seed")

        last_ombudsman = time.monotonic()
        final_state: dict = {}

        for cycle in range(self._max_cycles):
            time.sleep(self._poll_interval)

            state = self._board_client.full_state()
            final_state = state

            if self._cycle_callback is not None:
                try:
                    self._cycle_callback(state, cycle)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("RunLoop on_cycle error: %s", exc)

            if self._done_predicate(state):
                if self._complete_callback is not None:
                    try:
                        self._complete_callback(state)
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("RunLoop on_complete error: %s", exc)
                break

            now = time.monotonic()
            if now - last_ombudsman >= self._ombudsman_interval:
                logger.debug("RunLoop ombudsman: nudging chief")
                self._chief.nudge(trigger="ombudsman")
                if self._ombudsman_instance is not None:
                    self._ombudsman_instance.nudge()
                last_ombudsman = now

        return final_state
