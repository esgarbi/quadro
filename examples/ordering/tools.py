"""
Chief tools factory for the LLM ordering system.

All tools close over board_fn, network, board_url, and worker_registry so
the chief LLM can read the board and drive orders through the pipeline
without ever touching Quadro internals directly — every action goes through
the A2A boundary.

CRITICAL DESIGN NOTE — fire-and-forget dispatch
────────────────────────────────────────────────
All worker dispatch MUST be fire-and-forget (daemon thread). Never call
network.request(worker_url, ...) synchronously from within a chief tool.

Reason: chief tools run inside the chief's LLM decision cycle, which itself
runs inside a ThreadPoolExecutor thread. A synchronous worker dispatch would
block that thread for the full duration of the worker's LLM work (30-90s),
preventing any other chief activity and collapsing parallel execution to serial.

The correct pattern (used throughout this file):
  1. Do all board writes synchronously first (task state is the source of truth).
  2. Then fire the worker in a daemon thread and return immediately.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from agent_framework import tool

from quadro import (
    acknowledge_task,
    dispatch_batch,
    get_acknowledged,
)

logger = logging.getLogger(__name__)


def create_chief_tools(
    board_fn: Callable[[str, dict], dict],
    network,
    board_url: str,
    worker_registry: dict[str, list[tuple[str, str]]],
) -> list:
    """
    Create the chief's board-aware tool set for the ordering system.

    Args:
        board_fn        : Calls any board intent, raises on error.
        network         : LocalA2ANetwork instance for dispatching workers.
        board_url       : Board A2A URL.
        worker_registry : {capability: [(agent_id, url), ...]}

    Returns:
        List of AF tools the chief LLM can call.
    """

    def _req(intent: str, payload: dict) -> dict:
        return board_fn(intent, payload)

    # ── Tool definitions ───────────────────────────────────────────────────────

    @tool(
        name="advance_to_stock_check",
        description=(
            "Send ALL validated (or procured) orders to the inventory scout. "
            "Pass any task_id — the tool handles all eligible tasks. "
            "Returns immediately — workers run in the background."
        ),
    )
    def advance_to_stock_check(task_id: str) -> str:
        dispatched, skipped = dispatch_batch(
            board_fn,
            network,
            worker_registry,
            {"validated", "procured"},
            "checking_stock",
            "inventory",
        )
        if not dispatched and not skipped:
            return "No orders ready for stock check."
        msg = (
            f"Dispatched {len(dispatched)} to inventory check: {', '.join(dispatched)}"
        )
        if skipped:
            msg += f" | {len(skipped)} skipped (no idle inventory worker): {', '.join(skipped)}"
        return msg

    @tool(
        name="advance_to_procurement",
        description=(
            "Send ALL needs_procurement orders to the procurement negotiator. "
            "Pass any task_id — the tool handles all eligible tasks. "
            "Returns immediately — workers run in the background."
        ),
    )
    def advance_to_procurement(task_id: str) -> str:
        dispatched, skipped = dispatch_batch(
            board_fn,
            network,
            worker_registry,
            "needs_procurement",
            "procuring",
            "procurement",
        )
        if not dispatched and not skipped:
            return "No orders needing procurement."
        msg = f"Dispatched {len(dispatched)} to procurement: {', '.join(dispatched)}"
        if skipped:
            msg += f" | {len(skipped)} skipped (no idle procurement worker): {', '.join(skipped)}"
        return msg

    @tool(
        name="advance_to_shipping",
        description=(
            "Send ALL stock_confirmed orders to the logistics coordinator. "
            "Pass any task_id — the tool handles all eligible tasks. "
            "Returns immediately — workers run in the background."
        ),
    )
    def advance_to_shipping(task_id: str) -> str:
        dispatched, skipped = dispatch_batch(
            board_fn,
            network,
            worker_registry,
            "stock_confirmed",
            "shipping",
            "logistics",
        )
        if not dispatched and not skipped:
            return "No orders ready for shipping."
        msg = f"Dispatched {len(dispatched)} to shipping: {', '.join(dispatched)}"
        if skipped:
            msg += f" | {len(skipped)} skipped (no idle logistics worker): {', '.join(skipped)}"
        return msg

    @tool(
        name="discard_order",
        description=(
            "Acknowledge a FAILED or validation_failed order so it stops appearing "
            "in DISPATCH ALL. The pipeline slot is already freed. "
            "Call this once per failed order. Provide the full task_id."
        ),
    )
    def discard_order(task_id: str) -> str:
        state = _req("board.get_full_state", {})
        task = next(
            (t for t in state.get("tasks", []) if task_id in t["task_id"]), None
        )
        if not task:
            return f"Order {task_id[:8]} not found."

        acknowledged = get_acknowledged(board_fn)
        if task_id in acknowledged:
            return f"Order {task_id[:8]} already acknowledged."

        acknowledge_task(board_fn, task_id)
        task["status"] = "HUMAN_REVIEW"
        _req(
            "board.update_task",
            {
                "task_id": task_id,
                "to_status": "HUMAN_REVIEW",
            },
        )
        return (
            f"Order {task_id[:8]} acknowledged (status: {task['status']!r}). "
            "It will no longer appear in DISPATCH ALL."
        )

    return [
        advance_to_stock_check,
        advance_to_procurement,
        advance_to_shipping,
        discard_order,
    ]
