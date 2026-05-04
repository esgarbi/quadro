"""Review saga — editorial decision, publish, or revision routing."""

from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from quadro.saga import Saga, SagaContext

from schemas import ApprovedOutput

from ._common import ARTICLES_DIR, PROMPTS_DIR, _TOKENS_KEY_PREFIX

logger = logging.getLogger(__name__)


def _extract_article_md(task: dict[str, Any]) -> str:
    """Pull the article markdown out of the task's output payload."""
    output = task.get("output")
    if output is None:
        return ""
    if isinstance(output, str):
        try:
            output = json.loads(output or "{}")
        except Exception:
            return ""
    if not isinstance(output, dict):
        return ""
    return str(output.get("writing") or "")


def _extract_article_md_from_task(ctx: SagaContext) -> str:
    return _extract_article_md(ctx.task)


def _build_tokens_section(
    board_fn: Callable[[str, dict], dict], task_id: str
) -> dict[str, Any]:
    """Read cumulative per-stage tokens and build the output JSON section.

    Mirrors the legacy ``_build_tokens_section`` (from the retired
    ``agents.py``) so the published JSON carries the same shape whether
    the stages run as sagas or as the legacy workflow adapter.
    """
    try:
        raw = board_fn(
            "board.get_data", {"key": f"{_TOKENS_KEY_PREFIX}{task_id}"}
        )
        existing = (raw or {}).get("value") or {}
    except Exception as exc:  # noqa: BLE001
        logger.debug("tokens: failed to read final tokens for %s: %s", task_id, exc)
        existing = {}
    by_stage = {k: int(v) for k, v in (existing.get("by_stage") or {}).items()}
    return {
        "by_stage": by_stage,
        "total": sum(by_stage.values()),
        "model": os.environ.get("OPENAI_MODEL_ID", "<unset>"),
        "measured_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def _maybe_parse_json(value: Any) -> Any:
    """JSON-parse if the value is a string; passthrough otherwise."""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except Exception:
            return value
    return value


def _write_files_and_mark_published(ctx: SagaContext) -> dict[str, Any]:
    """Approved branch: derive a slug, write the .md and .json artefacts,
    transition the task to ``published``.

    Returns a small dict carrying the derived slug and published
    timestamp — the ``publication_record`` evidence step reads the slug
    from here to build its audit row.
    """
    task = ctx.task
    board_fn: Callable[[str, dict], dict] = task["_board_fn"]
    article_md: str = ctx.step["parse_draft"]

    ARTICLES_DIR.mkdir(parents=True, exist_ok=True)

    title_match = re.search(r"^#\s+(.+)$", article_md, re.MULTILINE)
    if title_match:
        raw = title_match.group(1).strip()
        slug = re.sub(r"[^\w\s-]", "", raw.lower())
        slug = re.sub(r"[\s_-]+", "-", slug)[:80].strip("-")
    else:
        slug = task["task_id"][:12]

    (ARTICLES_DIR / f"{slug}.md").write_text(article_md, encoding="utf-8")

    output = task.get("output")
    if isinstance(output, str):
        try:
            output = json.loads(output or "{}")
        except Exception:
            output = {}

    decision: ApprovedOutput = ctx.step["editorial_decision"]

    flight = {
        "brief": _maybe_parse_json((output or {}).get("brief", "{}")),
        "research": _maybe_parse_json((output or {}).get("research", "{}")),
        "article_md": article_md,
        "review_decision": decision.model_dump(),
        "tokens": _build_tokens_section(board_fn, task["task_id"]),
    }
    (ARTICLES_DIR / f"{slug}.json").write_text(
        json.dumps(flight, indent=4), encoding="utf-8"
    )
    logger.info("Published: %s (tokens: %s)", slug, flight["tokens"].get("total", 0))

    board_fn(
        "board.update_task",
        {
            "task_id": task["task_id"],
            "to_status": "published",
            "notes_append": f"Published: {slug}.md",
        },
    )
    return {"slug": slug, "path": str(ARTICLES_DIR / f"{slug}.md")}


def _route_back_to_idea_ready(ctx: SagaContext) -> dict[str, Any]:
    """Revision branch: route the task back to ``idea_ready`` with the
    reviewer's reason as a note.

    Structural note: in the saga as declared, this step appears after
    ``publish`` / ``publication_record`` in declaration order, so the
    publish branch's linear ``next_after`` advancement naturally walks
    into this step. The short-circuit at the top of the function reads
    the ``decision_routing`` gate's recorded choice and becomes a
    no-op if the approved path was taken. Without this check, every
    approved article would also trigger the revision transition (which
    the lifecycle would rightly reject). Surfaced in the milestone-C
    run note — the brief's review saga shape is ambiguous on this
    point and this is the most local resolution.
    """
    chosen = (ctx.step.get("decision_routing") or {}).get("chosen")
    if chosen != "request_revision":
        return {"skipped": True, "reason": f"chosen_branch_was_{chosen!r}"}

    task = ctx.task
    board_fn: Callable[[str, dict], dict] = task["_board_fn"]
    decision: ApprovedOutput = ctx.step["editorial_decision"]
    board_fn(
        "board.update_task",
        {
            "task_id": task["task_id"],
            "to_status": "idea_ready",
            "notes_append": decision.reason or "(no reason given)",
        },
    )
    return {"routed_back": True}


review_saga = (
    Saga("review")

    .guard("draft_present", check=lambda ctx: bool(_extract_article_md(ctx.task)))

    .deterministic("parse_draft", _extract_article_md_from_task)

    .reason(
        "editorial_decision",
        prompt=PROMPTS_DIR / "review.md",
        user_message=lambda ctx: f"## Article Draft\n\n{ctx.step['parse_draft']}",
        schema=ApprovedOutput,
    )

    .gate(
        "decision_routing",
        when=lambda ctx: ctx.step["editorial_decision"].approved,
        on_true="publish",
        on_false="request_revision",
    )

    .deterministic("publish", _write_files_and_mark_published)

    .evidence(
        "publication_record",
        capture=lambda ctx: {
            "slug": ctx.step["publish"]["slug"],
            "published_at": ctx.now.isoformat() if ctx.now else None,
            "attempt": int(ctx.task.get("revision_count", 0) or 0) + 1,
        },
    )

    .deterministic("request_revision", _route_back_to_idea_ready)

    .build()
)
