"""
Chief tools factory for the LLM newsroom.

The chief routes articles through the pipeline — it does NOT create them.
ArticleProducer posts UNASSIGNED tasks; the chief dispatches and advances them.

Tools:
  advance_to_ideation  — picks up UNASSIGNED articles → ideating
  advance_to_research  — idea_ready → researching
  advance_to_writing   — research_ready → writing
  advance_to_review    — draft_ready → reviewing
  discard_task         — acknowledges HUMAN_REVIEW tasks

CRITICAL: all dispatch is fire-and-forget (daemon thread). Never dispatch
workers synchronously from inside a tool — it blocks the chief's LLM cycle.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from agent_framework import tool
from quadro import (
    acknowledge_task,
    dispatch_batch,
    find_idle_worker,
    fire_worker,
    get_acknowledged,
)

import dotenv

dotenv.load_dotenv()

logger = logging.getLogger(__name__)

_TERMINAL = frozenset({"published", "HUMAN_REVIEW", "COMPLETE", "abandoned"})


def create_chief_tools(
    board_fn: Callable[[str, dict], dict],
    network,
    board_url: str,
    worker_registry: dict[str, list[tuple[str, str]]],
    capacity: int,
) -> list:

    def _req(intent: str, payload: dict) -> dict:
        return board_fn(intent, payload)

    @tool(
        name="advance_to_ideation",
        description=(
            "Dispatch ALL UNASSIGNED articles to ideation workers. "
            "Call this whenever you see UNASSIGNED articles on the board. "
            "Respects pipeline capacity — will not exceed the slot limit. "
            "Returns immediately — workers run in the background."
        ),
    )
    def advance_to_ideation(task_id: str = "") -> str:
        state = _req("board.get_full_state", {})
        tasks = state.get("tasks", [])
        active = sum(1 for t in tasks if t["status"] not in _TERMINAL)
        slots = max(0, capacity - active)

        if slots == 0:
            return f"Pipeline full ({active}/{capacity}). No slots available."

        unassigned = [t for t in tasks if t["status"] == "UNASSIGNED"][:slots]
        if not unassigned:
            return "No UNASSIGNED articles found."

        dispatched = []
        queued = []
        for t in unassigned:
            w = find_idle_worker(board_fn, worker_registry, "ideation")
            if w:
                agent_id, url = w
                _req(
                    "board.update_task",
                    {
                        "task_id": t["task_id"],
                        "to_status": "ideating",
                        "assigned_to": agent_id,
                    },
                )
                fire_worker(network, url, t["task_id"])
                dispatched.append(t["task_id"][:8])
            else:
                queued.append(t["task_id"][:8])

        msg = f"Dispatched {len(dispatched)} to ideation: {', '.join(dispatched)}"
        if queued:
            msg += f" | {len(queued)} queued (no idle ideation worker): {', '.join(queued)}"
        return msg

    @tool(
        name="advance_to_research",
        description=(
            "Send ALL idea_ready articles to the research desk. "
            "Pass any idea_ready task_id — the tool handles all of them. "
            "Returns immediately — workers run in the background."
        ),
    )
    def advance_to_research(task_id: str) -> str:
        dispatched, skipped = dispatch_batch(
            board_fn,
            network,
            worker_registry,
            "idea_ready",
            "researching",
            "research",
        )
        if not dispatched and not skipped:
            return "No idea_ready articles found."
        msg = f"Dispatched {len(dispatched)} to research: {', '.join(dispatched)}"
        if skipped:
            msg += f" | {len(skipped)} skipped (no idle worker): {', '.join(skipped)}"
        return msg

    @tool(
        name="advance_to_writing",
        description=(
            "Send ALL research_ready articles to the writing desk. "
            "Pass any research_ready task_id — the tool handles all of them. "
            "Returns immediately — workers run in the background."
        ),
    )
    def advance_to_writing(task_id: str) -> str:
        dispatched, skipped = dispatch_batch(
            board_fn,
            network,
            worker_registry,
            "research_ready",
            "writing",
            "writing",
        )
        if not dispatched and not skipped:
            return "No research_ready articles found."
        msg = f"Dispatched {len(dispatched)} to writing: {', '.join(dispatched)}"
        if skipped:
            msg += f" | {len(skipped)} skipped (no idle worker): {', '.join(skipped)}"
        return msg

    @tool(
        name="advance_to_review",
        description=(
            "Send ALL draft_ready articles to the review desk. "
            "Pass any draft_ready task_id — the tool handles all of them. "
            "Returns immediately — workers run in the background."
        ),
    )
    def advance_to_review(task_id: str) -> str:
        dispatched, skipped = dispatch_batch(
            board_fn,
            network,
            worker_registry,
            "draft_ready",
            "reviewing",
            "review",
        )
        if not dispatched and not skipped:
            return "No draft_ready articles found."
        msg = f"Dispatched {len(dispatched)} to review: {', '.join(dispatched)}"
        if skipped:
            msg += f" | {len(skipped)} skipped (no idle worker): {', '.join(skipped)}"
        return msg

    @tool(
        name="discard_task",
        description=(
            "Acknowledge a HUMAN_REVIEW task so it stops appearing in the board summary. "
            "Call once per failed task. Provide the full task_id."
        ),
    )
    def discard_task(task_id: str) -> str:
        state = _req("board.get_full_state", {})
        task = next(
            (t for t in state.get("tasks", []) if t["task_id"] == task_id), None
        )
        if not task:
            return f"Task {task_id[:8]} not found."
        acknowledged = get_acknowledged(board_fn)
        if task_id in acknowledged:
            return f"Task {task_id[:8]} already acknowledged."
        acknowledge_task(board_fn, task_id)
        return f"Task {task_id[:8]} acknowledged."

    return [
        advance_to_ideation,
        advance_to_research,
        advance_to_writing,
        advance_to_review,
        discard_task,
    ]
