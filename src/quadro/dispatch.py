"""
Dispatch and acknowledgement helpers for Quadro pipelines.

Provides fire-and-forget worker dispatch, idle-worker selection, batch
dispatch, and task acknowledgement tracking.  All functions are pure
Quadro -- they depend only on ``board_fn``, ``A2ARequest``, and stdlib.
"""

from __future__ import annotations

import logging
import random
import threading
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)

# ── Worker dispatch ───────────────────────────────────────────────────────────


def find_idle_worker(
    board_fn: Callable[[str, dict], dict],
    worker_registry: dict[str, list[tuple[str, str]]],
    capability: str,
) -> tuple[str, str] | None:
    """Return ``(agent_id, url)`` for a random IDLE worker, or ``None``."""
    workers = worker_registry.get(capability, [])
    if not workers:
        return None
    state = board_fn("board.get_full_state", {})
    idle_ids = {
        a["agent_id"] for a in state.get("agents", []) if a.get("status") == "IDLE"
    }
    idle = [(aid, url) for aid, url in workers if aid in idle_ids]
    if not idle:
        return None
    return random.choice(idle)


def fire_worker(network: Any, url: str, task_id: str) -> None:
    """Dispatch a task to a worker in a daemon thread -- fire and forget.

    The board transition MUST be written before calling this function.
    """
    from .a2a.contracts import A2ARequest

    def _run() -> None:
        try:
            network.request(
                url,
                A2ARequest(
                    intent="worker.execute_task",
                    payload={"task_id": task_id},
                ).to_dict(),
            )
        except Exception as exc:
            logger.warning("Worker dispatch error task=%s: %s", task_id[:8], exc)

    threading.Thread(target=_run, daemon=True, name=f"worker-{task_id[:8]}").start()


def dispatch_batch(
    board_fn: Callable[[str, dict], dict],
    network: Any,
    worker_registry: dict[str, list[tuple[str, str]]],
    status_filter: str | set[str],
    target_status: str,
    capability: str,
) -> tuple[list[str], list[str]]:
    """Advance all tasks matching *status_filter* to *target_status*.

    Returns ``(dispatched_ids_short, skipped_ids_short)`` where each id is
    truncated to 8 chars for log-friendly output.

    *status_filter* may be a single status string or a set of statuses.
    """
    if isinstance(status_filter, str):
        status_filter = {status_filter}
    state = board_fn("board.get_full_state", {})
    eligible = [t for t in state.get("tasks", []) if t["status"] in status_filter]
    dispatched: list[str] = []
    skipped: list[str] = []
    for t in eligible:
        w = find_idle_worker(board_fn, worker_registry, capability)
        if w:
            agent_id, url = w
            board_fn(
                "board.update_task",
                {
                    "task_id": t["task_id"],
                    "to_status": target_status,
                    "assigned_to": agent_id,
                },
            )
            fire_worker(network, url, t["task_id"])
            dispatched.append(t["task_id"][:8])
        else:
            skipped.append(t["task_id"][:8])
    return dispatched, skipped


# ── Acknowledgement tracking ──────────────────────────────────────────────────

ACKNOWLEDGED_KEY = "_acknowledged_failures"


def get_acknowledged(board_fn: Callable[[str, dict], dict]) -> set[str]:
    """Return the set of task_ids the chief has already acknowledged as failed."""
    try:
        result = board_fn("board.get_data", {"key": ACKNOWLEDGED_KEY})
        val = result.get("value") or []
        return set(val) if isinstance(val, list) else set()
    except Exception:
        return set()


def acknowledge_task(
    board_fn: Callable[[str, dict], dict],
    task_id: str,
) -> None:
    """Mark a *task_id* as acknowledged (stored in board data, not task state)."""
    try:
        acked = get_acknowledged(board_fn)
        acked.add(task_id)
        board_fn(
            "board.put_data",
            {"key": ACKNOWLEDGED_KEY, "value": list(acked)},
        )
    except Exception as exc:
        logger.warning("Failed to acknowledge task %s: %s", task_id[:8], exc)
