"""Ideation saga — headline generation and brief authoring."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from quadro.saga import Saga, SagaContext

from schemas import ArticleBrief, Headline

from ._common import PROMPTS_DIR


def _collect_existing_titles(ctx: SagaContext) -> list[str]:
    """Gather published article titles to avoid duplicating.

    Mirrors the first phase of the legacy ``run_ideation`` execute_fn
    (retired in milestone C.5):
    read the full board state (pre-fetched by the saga runtime into
    ``ctx.task["_board_state"]``), collect titles from tasks that have
    output (i.e. have progressed past ideation), exclude the current
    task. Returns a list of strings.
    """
    task = ctx.task
    board_state = task.get("_board_state") or {}
    titles: list[str] = []
    for t in board_state.get("tasks", []):
        if t["task_id"] == task["task_id"]:
            continue
        if t.get("output"):
            titles.append(t.get("label", ""))
    return [t for t in titles if t]


def _persist_brief(ctx: SagaContext) -> dict[str, Any]:
    """Write the brief back to the task and advance to ``idea_ready``."""
    task = ctx.task
    board_fn: Callable[[str, dict], dict] = task["_board_fn"]
    brief: ArticleBrief = ctx.step["flesh_out_brief"]
    board_fn(
        "board.update_task",
        {
            "task_id": task["task_id"],
            "label": brief.title,
            "to_status": "idea_ready",
            "output": brief.model_dump_json(),
        },
    )
    return {"persisted": True, "title": brief.title}


ideation_saga = (
    Saga("ideation")

    .deterministic("collect_avoid_list", _collect_existing_titles)

    .reason(
        "propose_headline",
        prompt=PROMPTS_DIR / "headline.md",
        user_message=lambda ctx: {
            "topic": ctx.task.get("label", "health and wellbeing"),
            "avoid_titles": ctx.step["collect_avoid_list"],
        },
        schema=Headline,
    )

    .reason(
        "flesh_out_brief",
        prompt=PROMPTS_DIR / "ideation.md",
        user_message=lambda ctx: ctx.step["propose_headline"].headline,
        schema=ArticleBrief,
    )

    .deterministic("persist_brief", _persist_brief)

    .build()
)
