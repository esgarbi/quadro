"""
LLM agent execute_fn implementations for the newsroom pipeline.

Each function is an async coroutine with the standard Quadro worker signature:
    (context: dict, board_fn: Callable[[str, dict], dict]) -> str

Article creation is handled by ArticleProducer (see producer.py).
The chief policy dispatches UNASSIGNED tasks mechanically, then uses the LLM
to route articles through the pipeline stages.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from schemas import (
    ApprovedOutput,
    ArticleBrief,
    Citation,
    ResearchOutput,
    ResearchStrategy,
)

from shared import (
    create_llm_client,
    find_idle_worker,
    fire_worker,
    load_prompt,
    run_chief_workflow,
    run_single_agent,
)

logger = logging.getLogger(__name__)

PROMPTS_DIR = Path(__file__).parent / "prompts"
ARTICLES_DIR = Path(__file__).parent / "output"

_client = create_llm_client


def _prompt(name: str) -> str:
    return load_prompt(PROMPTS_DIR, name)


# ── Per-task token accounting ──────────────────────────────────────────────────
#
# Each stage's execute_fn accumulates the LLM tokens it spent into a
# stage-local counter (via the ``token_reporter`` hook in
# ``shared.run_single_agent``). At end-of-stage it persists the delta onto
# a per-task board data key ``_tokens:<task_id>``. ``run_review`` reads
# that record when it publishes the article JSON and surfaces the
# per-stage + total breakdown in the output file. Cumulative semantics:
# if a draft is rejected and re-written, later stage runs add onto the
# same bucket, so the final JSON reflects the true cost (including any
# rewrites).

_TOKENS_KEY_PREFIX = "_tokens:"


def _bump_stage_tokens(
    board_fn: Callable[[str, dict], dict],
    task_id: str,
    stage: str,
    delta: int,
) -> None:
    """Merge-add ``delta`` tokens onto ``_tokens:<task_id>.by_stage[stage]``."""
    if delta <= 0:
        return
    key = f"{_TOKENS_KEY_PREFIX}{task_id}"
    try:
        existing = (board_fn("board.get_data", {"key": key}) or {}).get("value") or {}
    except Exception as exc:  # noqa: BLE001
        logger.debug("tokens: failed to read %s: %s", key, exc)
        existing = {}
    by_stage = dict(existing.get("by_stage") or {})
    by_stage[stage] = int(by_stage.get(stage, 0)) + int(delta)
    try:
        board_fn("board.put_data", {"key": key, "value": {"by_stage": by_stage}})
    except Exception as exc:  # noqa: BLE001
        logger.debug("tokens: failed to write %s: %s", key, exc)


def _make_stage_reporter() -> tuple[Callable[[int], None], Callable[[], int]]:
    """Return ``(reporter, read_total)`` — a closure-backed token accumulator."""
    total = 0

    def reporter(n: int) -> None:
        nonlocal total
        if n > 0:
            total += int(n)

    def read_total() -> int:
        return total

    return reporter, read_total


# ── PubMed helper ──────────────────────────────────────────────────────────────


def _pubmed_search(query: str, max_results: int = 5, retries: int = 3) -> list[dict]:
    base = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"

    def _get(url: str) -> bytes:
        delay = 1.0
        for attempt in range(retries):
            try:
                with urllib.request.urlopen(url, timeout=15) as r:
                    return r.read()
            except urllib.error.HTTPError as exc:
                if exc.code == 429 and attempt < retries - 1:
                    logger.warning("PubMed 429 — retrying in %.1fs", delay)
                    time.sleep(delay)
                    delay *= 2
                    continue
                raise
        raise RuntimeError("PubMed retries exhausted")

    try:
        ids = json.loads(
            _get(
                f"{base}/esearch.fcgi?db=pubmed&retmax={max_results}"
                f"&retmode=json&term={urllib.parse.quote(query)}"
            )
        )["esearchresult"]["idlist"]
        if not ids:
            return []
        time.sleep(0.4)
        data = json.loads(
            _get(f"{base}/esummary.fcgi?db=pubmed&retmode=json&id={','.join(ids)}")
        )["result"]
        results = []
        for pmid in ids:
            rec = data.get(pmid, {})
            authors = ", ".join(a.get("name", "") for a in rec.get("authors", [])[:3])
            year = rec.get("pubdate", "")[:4]
            results.append(
                {
                    "pmid": pmid,
                    "title": rec.get("title", ""),
                    "authors": authors,
                    "year": int(year) if year.isdigit() else 2023,
                    "journal": rec.get("source", ""),
                    "abstract": "",
                }
            )
        return results
    except Exception as exc:
        logger.warning("PubMed fetch failed: %s", exc)
        return []


# ── Worker execute_fn functions ────────────────────────────────────────────────


async def run_ideation(context: dict, board_fn: Callable[[str, dict], dict]) -> str:
    """Generate ArticleBrief from topic hint → transition to idea_ready."""
    state = board_fn("board.get_full_state", {})
    task = context["payload"]["task"]

    existing_titles = [
        t.get("label", "")
        for t in state.get("tasks", [])
        if t["task_id"] != task["task_id"] and t.get("output")
    ]
    avoid_block = (
        "Already published titles to avoid:\n- " + "\n- ".join(existing_titles)
        if existing_titles
        else "No published titles yet. Be creative!"
    )

    topic_hint = task.get("label", "health and wellbeing topic")

    report, read_total = _make_stage_reporter()

    headline_raw = await run_single_agent(
        instructions=f"""You are a senior editorial writer for a health publication.
Generate one compelling article headline for this topic hint.

{avoid_block}

Respond ONLY with a valid JSON object containing a single "headline" key.
No markdown, no preamble.""",
        user_message=f"Topic: {topic_hint}",
        default_options={"response_format": {"type": "json_object"}},
        executor_prefix="ideation",
        token_reporter=report,
    )

    brief_raw = await run_single_agent(
        instructions=_prompt("ideation"),
        user_message=headline_raw,
        default_options={"response_format": ArticleBrief},
        executor_prefix="ideation",
        token_reporter=report,
    )

    try:
        brief = ArticleBrief.model_validate_json(brief_raw)
        board_fn(
            "board.update_task",
            {
                "task_id": task["task_id"],
                "label": brief.title,
                "to_status": "idea_ready",
                "output": brief.model_dump_json(),
            },
        )
    except Exception as exc:
        logger.warning("Ideation parse error: %s", exc)
        board_fn(
            "board.update_task",
            {
                "task_id": task["task_id"],
                "to_status": "idea_ready",
                "output": brief_raw,
            },
        )

    _bump_stage_tokens(board_fn, task["task_id"], "ideation", read_total())
    return "Ideation complete."


async def run_research(context: dict, board_fn: Callable[[str, dict], dict]) -> str:
    """Generate research strategy + fetch PubMed → transition to research_ready."""
    task = context["payload"]["task"]

    brief: ArticleBrief | None = None
    try:
        brief = ArticleBrief.model_validate_json(task.get("output", "{}"))
        title = brief.title
        keywords = ", ".join(brief.research_keywords or brief.keywords)
    except Exception:
        title = task.get("label", "health topic")
        keywords = title

    report, read_total = _make_stage_reporter()
    strategy_raw = await run_single_agent(
        instructions=_prompt("research"),
        user_message=f"Article title: {title}\nKeywords: {keywords}",
        default_options={"response_format": {"type": "json_object"}},
        executor_prefix="research",
        token_reporter=report,
    )

    try:
        strategy = ResearchStrategy.model_validate_json(strategy_raw)
    except Exception:
        strategy = ResearchStrategy(
            core_concepts=[],
            pubmed_queries=[keywords],
            gap_angle="",
            suggested_filters={},
        )

    all_citations: list[dict] = []
    for i, query in enumerate(strategy.pubmed_queries[:3]):
        if i > 0:
            time.sleep(0.4)
        all_citations.extend(_pubmed_search(query, max_results=4))
        if len(all_citations) >= 8:
            break

    seen: set[str] = set()
    unique: list[Citation] = []
    for c in all_citations:
        if c["pmid"] not in seen:
            seen.add(c["pmid"])
            unique.append(Citation(**c))

    if not unique:
        kws = (
            brief.research_keywords if brief and brief.research_keywords else [keywords]
        )
        unique = [
            Citation(
                pmid="",
                title=f"Research on {kw}",
                authors="et al.",
                year=2023,
                journal="Journal of Health Sciences",
                abstract="",
            )
            for kw in kws[:4]
        ]

    output = ResearchOutput(
        strategy=strategy,
        citations=unique[:8],
        summary=f"Found {len(unique)} citations for: {title}",
    )

    board_fn(
        "board.update_task",
        {
            "task_id": task["task_id"],
            "to_status": "research_ready",
            "output": {
                "brief": brief.model_dump_json() if brief else "{}",
                "research": output.model_dump_json(),
            },
        },
    )
    _bump_stage_tokens(board_fn, task["task_id"], "research", read_total())
    return "Research complete."


async def run_writing(context: dict, board_fn: Callable[[str, dict], dict]) -> str:
    """Produce full markdown article → transition to draft_ready."""
    task = context["payload"]["task"]
    output = json.loads(task.get("output", "{}"))

    brief = ArticleBrief.model_validate_json(output.get("brief", "{}"))
    research = ResearchOutput.model_validate_json(output.get("research", "{}"))

    citations_block = "\n".join(
        f"- {c.authors} ({c.year}). {c.title}. {c.journal}." for c in research.citations
    )

    writing_input = (
        f"## Article Brief\n"
        f"Title: {brief.title}\n"
        f"Writer persona: {brief.writer}\n"
        f"Thesis: {brief.thesis}\n"
        f"Sections: {', '.join(brief.sections)}\n\n"
        f"## Research Citations\n{citations_block or '(none)'}"
    )

    report, read_total = _make_stage_reporter()
    article_md = await run_single_agent(
        instructions=_prompt("writing"),
        user_message=writing_input,
        executor_prefix="writing",
        token_reporter=report,
    )

    board_fn(
        "board.update_task",
        {
            "task_id": task["task_id"],
            "to_status": "draft_ready",
            "output": {
                "brief": output.get("brief", "{}"),
                "research": output.get("research", "{}"),
                "writing": article_md,
            },
        },
    )
    _bump_stage_tokens(board_fn, task["task_id"], "writing", read_total())
    return "Writing complete."


async def run_review(context: dict, board_fn: Callable[[str, dict], dict]) -> str:
    """Approve or revise draft; publish to articles/ if approved."""
    task = context["payload"]["task"]
    output = json.loads(task.get("output", "{}"))
    article_md = output.get("writing", "")

    if not article_md:
        return "No draft available."

    report, read_total = _make_stage_reporter()
    decision_raw = await run_single_agent(
        instructions=_prompt("review"),
        user_message=f"## Article Draft\n\n{article_md}",
        default_options={"response_format": ApprovedOutput},
        executor_prefix="review",
        token_reporter=report,
    )

    decision = ApprovedOutput.model_validate_json(decision_raw)

    # Record the review stage's tokens first so the per-article JSON
    # below sees its own cost (and so rejected drafts still accrue the
    # rewriter-visible total for the eventual approval).
    _bump_stage_tokens(board_fn, task["task_id"], "review", read_total())

    if decision.approved:
        ARTICLES_DIR.mkdir(parents=True, exist_ok=True)
        title_match = re.search(r"^#\s+(.+)$", article_md, re.MULTILINE)
        if title_match:
            raw = title_match.group(1).strip()
            slug = re.sub(r"[^\w\s-]", "", raw.lower())
            slug = re.sub(r"[\s_-]+", "-", slug)[:80].strip("-")
        else:
            slug = task["task_id"][:12]

        (ARTICLES_DIR / f"{slug}.md").write_text(article_md, encoding="utf-8")

        flight = {
            "brief": json.loads(output.get("brief", "{}")),
            "research": json.loads(output.get("research", "{}")),
            "article_md": article_md,
            "review_decision": decision.model_dump(),
            "tokens": _build_tokens_section(board_fn, task["task_id"]),
        }
        import json as _json

        (ARTICLES_DIR / f"{slug}.json").write_text(
            _json.dumps(flight, indent=4), encoding="utf-8"
        )
        logger.info(
            "Published: %s  (tokens: %s)", slug, flight["tokens"].get("total", 0)
        )

        board_fn(
            "board.update_task",
            {
                "task_id": task["task_id"],
                "to_status": "published",
                "notes_append": f"Published: {slug}.md",
            },
        )
    else:
        board_fn(
            "board.update_task",
            {
                "task_id": task["task_id"],
                "to_status": "idea_ready",
                "notes_append": decision.reason,
            },
        )

    return decision.reason


def _build_tokens_section(
    board_fn: Callable[[str, dict], dict], task_id: str
) -> dict:
    """Read cumulative per-stage tokens and build the output JSON section."""
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


# ── Chief policy ───────────────────────────────────────────────────────────────


def build_chief_policy(
    board_client,
    worker_registry: dict[str, list[tuple[str, str]]],
    capacity: int = 4,
) -> Callable:
    """
    Chief routes articles through the pipeline. ArticleProducer creates tasks.

    Two-step dispatch:
      1. UNASSIGNED → ideating: mechanical, no LLM. A task arrived, send it.
      2. Rest of pipeline: LLM routes idea_ready, research_ready, draft_ready.
    """
    from tools import create_chief_tools

    network = board_client.network
    board_url = board_client.board_url

    async def chief_policy(chief_context: dict) -> None:
        def board_fn(intent: str, p: dict) -> dict:
            return board_client.request(intent, p)

        # ── Step 1: Dispatch UNASSIGNED tasks mechanically ─────────────────────
        # No LLM call. A task arrived on the board — dispatch it to ideation.
        # Uses hydrated context so no extra board read needed.
        payload = chief_context["payload"]
        tasks = payload.get("tasks", [])
        _terminal = {"published", "HUMAN_REVIEW", "COMPLETE", "abandoned"}
        active = sum(1 for t in tasks if t["status"] not in _terminal)
        slots = max(0, capacity - active)
        unassigned = [t for t in tasks if t["status"] == "UNASSIGNED"]

        dispatched = 0
        for task in unassigned[:slots]:
            w = find_idle_worker(board_fn, worker_registry, "ideation")
            if w:
                agent_id, url = w
                board_fn(
                    "board.update_task",
                    {
                        "task_id": task["task_id"],
                        "to_status": "ideating",
                        "assigned_to": agent_id,
                    },
                )
                fire_worker(network, url, task["task_id"])
                dispatched += 1
                logger.info("Chief: dispatched %s → ideating", task["task_id"][:8])

        if dispatched:
            logger.info(
                "Chief: dispatched %d UNASSIGNED article(s) to ideation", dispatched
            )

        # ── Step 2: LLM routes the rest of the pipeline ────────────────────────
        tools = create_chief_tools(
            board_fn, network, board_url, worker_registry, capacity
        )

        board_summary = board_client.snapshot(tools, goal_key="newsroom_goal")
        if board_summary is None:
            logger.debug("Chief: nothing actionable for LLM — sleeping")
            return

        try:
            output = await run_chief_workflow(
                board_summary=board_summary,
                instructions=_prompt("chief"),
                tools=tools,
                client_factory=_client,
                agent_name_prefix="managing_director",
            )
            if output:
                logger.info("Chief: %s", output[:200])
        except Exception as exc:
            logger.error("Chief policy error: %s", exc)

    return chief_policy
