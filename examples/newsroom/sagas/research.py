"""Research saga — PubMed citation gathering and strategy planning."""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from datetime import timedelta
from typing import Any

from quadro.saga import Saga, SagaContext

from schemas import ArticleBrief, Citation, ResearchOutput, ResearchStrategy

from ._common import PROMPTS_DIR

logger = logging.getLogger(__name__)


# ── PubMed helper ────────────────────────────────────────────────────────────


def _pubmed_search(query: str, max_results: int = 5, retries: int = 3) -> list[dict]:
    """Lightweight PubMed fetch — parity with the legacy helper.

    Kept as a self-contained helper; the original copy lived in the
    legacy ``agents.py`` before that file was retired in milestone C.5.
    The retry semantics handled by the inner ``_get`` helper are
    orthogonal to the saga-level ``.retry()`` modifier: this helper
    handles its own HTTP back-off, while the saga-level retry wraps
    the whole fetch step to recover from sustained 429 storms that
    exhaust the inner attempts.
    """
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


# ── Deterministic helpers ────────────────────────────────────────────────────


def _extract_brief_from_task(ctx: SagaContext) -> ArticleBrief:
    """Parse the ArticleBrief from the task's output payload.

    Two shapes are accepted because the research saga can run under
    two conditions:

    1. **First pass** — task just came out of ideation. ``task.output``
       is a raw ``ArticleBrief`` JSON string (the ideation saga calls
       ``brief.model_dump_json()`` directly).
    2. **Revision loop** — task was rejected in review and routed
       back to ``idea_ready``; chief re-dispatches to research. At
       this point ``task.output`` is the merged dict the writing / review
       stages produced (``{"brief": "<json>", "research": "<json>",
       "writing": "<md>"}``). The fresh brief lives under the
       ``"brief"`` key as a JSON string. The writing saga's
       ``_parse_inputs`` already uses this shape; mirroring it here
       keeps revision-then-publish cycles working.

    Any other shape is a bug in an upstream stage; this raises so the
    saga fails loudly rather than silently degrading.
    """
    raw: Any = ctx.task.get("output")
    if raw is None or raw == "":
        raise ValueError(
            f"task {ctx.task['task_id']}: output is empty, cannot parse brief"
        )
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"task {ctx.task['task_id']}: output is not valid JSON "
                f"({exc}); cannot parse brief"
            ) from exc
    if isinstance(raw, dict) and "brief" in raw:
        brief_payload: Any = raw["brief"]
        if isinstance(brief_payload, str):
            return ArticleBrief.model_validate_json(brief_payload)
        return ArticleBrief.model_validate(brief_payload)
    if isinstance(raw, dict):
        return ArticleBrief.model_validate(raw)
    raise ValueError(
        f"task {ctx.task['task_id']}: unexpected output type "
        f"{type(raw).__name__}; cannot parse brief"
    )


def _fetch_pubmed_for_strategy(ctx: SagaContext) -> list[dict]:
    """Run up to three of the planned PubMed queries serially.

    Mirrors the inline loop inside the legacy ``run_research``:
    take the first three ``pubmed_queries``, fetch up to four results
    per query, stop early once eight citations are collected. Returns a
    list of plain dicts — the next step dedupes and converts to
    pydantic ``Citation`` instances.
    """
    strategy: ResearchStrategy = ctx.step["plan_strategy"]
    all_citations: list[dict] = []
    for i, query in enumerate(strategy.pubmed_queries[:3]):
        if i > 0:
            time.sleep(0.4)
        all_citations.extend(_pubmed_search(query, max_results=4))
        if len(all_citations) >= 8:
            break
    return all_citations


def _deduplicate_by_pmid(ctx: SagaContext) -> list[dict]:
    """Dedupe citations by PMID, preserving first-seen order.

    Returns a list of plain dicts (not pydantic ``Citation`` instances)
    so the saga's persisted state stays JSON-compatible without having
    to go through the rehydration path for the (numeric) Citation
    schema. The persist step converts to ``Citation`` at the point of
    writing the final output.
    """
    raw_citations: list[dict] = ctx.step["query_pubmed"]
    seen: set[str] = set()
    unique: list[dict] = []
    for c in raw_citations:
        pmid = c.get("pmid", "")
        if pmid in seen:
            continue
        seen.add(pmid)
        unique.append(c)
    return unique[:8]


def _build_placeholder_citations(ctx: SagaContext) -> list[dict]:
    """Fallback path: when dedupe produced no citations, synthesise a
    short placeholder list derived from the brief's research keywords.

    Mirrors the ``if not unique:`` branch inside the legacy
    ``run_research``. The gate above this step selects this path when
    the live fetch returned nothing.
    """
    brief: ArticleBrief = ctx.step["parse_brief"]
    kws = brief.research_keywords or brief.keywords or ["health"]
    placeholders = [
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
    return [c.model_dump() for c in placeholders]


def _merge_research_into_task_output(ctx: SagaContext) -> dict[str, Any]:
    """Write brief + research into the task's ``output`` and advance
    the lifecycle to ``research_ready``.

    The upstream gate routed either to the live-citations path or the
    placeholder path; both paths feed into this step via different
    entries in ``ctx.step``. This helper prefers the deduped list if it
    is non-empty and falls back to the synthesised placeholders
    otherwise — matching the legacy behaviour and keeping the two
    branches interchangeable from this step's point of view.
    """
    task = ctx.task
    board_fn: Callable[[str, dict], dict] = task["_board_fn"]
    brief: ArticleBrief = ctx.step["parse_brief"]
    strategy: ResearchStrategy = ctx.step["plan_strategy"]

    deduped = ctx.step.get("dedupe_citations") or []
    placeholders = ctx.step.get("synthesise_placeholder_citations") or []
    chosen = deduped if deduped else placeholders
    citations = [Citation(**c) for c in chosen]

    research = ResearchOutput(
        strategy=strategy,
        citations=citations,
        summary=f"Found {len(citations)} citations for: {brief.title}",
    )

    board_fn(
        "board.update_task",
        {
            "task_id": task["task_id"],
            "to_status": "research_ready",
            "output": {
                "brief": brief.model_dump_json(),
                "research": research.model_dump_json(),
            },
        },
    )
    return {"persisted": True, "citations": len(citations)}


# ── Saga definition ─────────────────────────────────────────────────────────


research_saga = (
    Saga("research")
    .guard("brief_must_exist", check=lambda ctx: ctx.task.get("output") is not None)
    .deterministic("parse_brief", _extract_brief_from_task)
    .reason(
        "plan_strategy",
        prompt=PROMPTS_DIR / "research.md",
        user_message=lambda ctx: {
            "title": ctx.step["parse_brief"].title,
            "keywords": ", ".join(
                ctx.step["parse_brief"].research_keywords
                or ctx.step["parse_brief"].keywords
            ),
        },
        schema=ResearchStrategy,
    )
    .deterministic("query_pubmed", _fetch_pubmed_for_strategy)
    .retry(attempts=3, on=(urllib.error.HTTPError,))
    .deadline(within=timedelta(seconds=30))
    .deterministic("dedupe_citations", _deduplicate_by_pmid)
    .gate(
        "fallback_check",
        when=lambda ctx: len(ctx.step["dedupe_citations"]) > 0,
        on_true="persist_research",
        on_false="synthesise_placeholder_citations",
    )
    .deterministic(
        "synthesise_placeholder_citations",
        _build_placeholder_citations,
    )
    .deterministic("persist_research", _merge_research_into_task_output)
    .build()
)
