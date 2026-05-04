"""Writing saga — article drafting from brief and research."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from quadro.saga import Saga, SagaContext

from schemas import ArticleBrief, ResearchOutput

from ._common import PROMPTS_DIR


def _writing_inputs_present(task: dict[str, Any]) -> bool:
    """Return True if task.output has both a ``brief`` and a ``research`` entry."""
    output = task.get("output")
    if output is None:
        return False
    if isinstance(output, str):
        try:
            output = json.loads(output or "{}")
        except Exception:
            return False
    if not isinstance(output, dict):
        return False
    return "brief" in output and "research" in output


def _parse_inputs(ctx: SagaContext) -> dict[str, Any]:
    """Extract brief + research from the task's output payload."""
    output = ctx.task.get("output") or {}
    if isinstance(output, str):
        output = json.loads(output or "{}")
    brief = ArticleBrief.model_validate_json(output.get("brief", "{}"))
    research = ResearchOutput.model_validate_json(output.get("research", "{}"))
    return {"brief": brief, "research": research, "output": output}


def _build_writing_input_payload(ctx: SagaContext) -> str:
    """Build the user-message payload the writing LLM call consumes."""
    parsed = ctx.step["parse_inputs"]
    brief: ArticleBrief = parsed["brief"]
    research: ResearchOutput = parsed["research"]
    citations_block = "\n".join(
        f"- {c.authors} ({c.year}). {c.title}. {c.journal}." for c in research.citations
    )
    return (
        f"## Article Brief\n"
        f"Title: {brief.title}\n"
        f"Writer persona: {brief.writer}\n"
        f"Thesis: {brief.thesis}\n"
        f"Sections: {', '.join(brief.sections)}\n\n"
        f"## Research Citations\n{citations_block or '(none)'}"
    )


def _merge_draft_into_task_output(ctx: SagaContext) -> dict[str, Any]:
    """Write the draft markdown into task.output and transition to draft_ready."""
    task = ctx.task
    board_fn: Callable[[str, dict], dict] = task["_board_fn"]
    parsed = ctx.step["parse_inputs"]
    output_payload: dict[str, Any] = parsed["output"] or {}
    article_md: str = ctx.step["draft_article"]

    board_fn(
        "board.update_task",
        {
            "task_id": task["task_id"],
            "to_status": "draft_ready",
            "output": {
                "brief": output_payload.get("brief", "{}"),
                "research": output_payload.get("research", "{}"),
                "writing": article_md,
            },
        },
    )
    return {"persisted": True, "length": len(article_md)}


writing_saga = (
    Saga("writing")
    .guard(
        "brief_and_research_present",
        check=lambda ctx: _writing_inputs_present(ctx.task),
    )
    .deterministic("parse_inputs", _parse_inputs)
    .deterministic("compose_input", _build_writing_input_payload)
    .reason(
        "draft_article",
        prompt=PROMPTS_DIR / "writing.md",
        user_message=lambda ctx: ctx.step["compose_input"],
    )
    .expect(
        "draft_is_non_empty",
        invariant=lambda ctx: bool(str(ctx.step["draft_article"]).strip()),
    )
    .deterministic("persist_draft", _merge_draft_into_task_output)
    .build()
)
