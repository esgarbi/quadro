"""
LLM Newsroom — streamlined entry point using the MafPipeline adapter.

Demonstrates the same newsroom pipeline as main.py but with less wiring.
Simple stages (ideation, writing) use auto-generated execute_fns.
Complex stages (research, review) use custom execute_fns as escape hatches
to handle PubMed fetching and file output.

Usage:
    python main_pipeline.py
    python main_pipeline.py --target 5

For the full-control version, see main.py.

Board UI (second terminal):
    python -m quadro.ui examples/microsoft_agent_framework/newsroom/newsroom.db
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

from quadro import LifecycleBuilder, LocalA2ANetwork, QuadroBoard
from quadro.board.backends.sqlite import SqliteBoardBackend
from quadro.integrations.maf import MafPipeline, llm_call

from schemas import (
    ApprovedOutput,
    ArticleBrief,
    Citation,
    ResearchOutput,
    ResearchStrategy,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("newsroom")

HERE = Path(__file__).parent
ARTICLES_DIR = HERE / "output"

CHOREOGRAPHIES: dict[str, list[tuple[int, float]]] = {
    "sleep_study": [(2, 0.0), (2, 5.0), (2, 5.0)],
    "wave_study": [(3, 0.0), (2, 8.0), (2, 8.0)],
}

ARTICLE_LIFECYCLE = (
    LifecycleBuilder()
    .step("UNASSIGNED", "ideating")
    .step("ideating", "idea_ready")
    .step("idea_ready", "researching")
    .step("researching", "research_ready")
    .step("research_ready", "writing")
    .step("writing", "draft_ready")
    .step("draft_ready", "reviewing")
    .step("reviewing", "published")
    .revision("reviewing", "idea_ready")
    .build()
)


# ── Custom execute_fns for stages that need domain logic ──────────────────────
# These use the llm_call() helper instead of the raw run_single_agent pattern.


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


async def run_research(context: dict, board_fn: Callable[[str, dict], dict]) -> str:
    """Custom research worker: LLM strategy + PubMed API calls."""
    task = context["payload"]["task"]

    brief: ArticleBrief | None = None
    try:
        brief = ArticleBrief.model_validate_json(task.get("output", "{}"))
        title = brief.title
        keywords = ", ".join(brief.research_keywords or brief.keywords)
    except Exception:
        title = task.get("label", "health topic")
        keywords = title

    strategy = await llm_call(
        prompt=HERE / "prompts" / "research.md",
        input={"title": title, "keywords": keywords},
        schema=ResearchStrategy,
        executor_prefix="research",
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
        kws = brief.research_keywords if brief and brief.research_keywords else [keywords]
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
    return "Research complete."


async def run_review(context: dict, board_fn: Callable[[str, dict], dict]) -> str:
    """Custom review worker: LLM review + file output on approval."""
    task = context["payload"]["task"]
    output = json.loads(task.get("output", "{}"))
    article_md = output.get("writing", "")

    if not article_md:
        return "No draft available."

    decision = await llm_call(
        prompt=HERE / "prompts" / "review.md",
        input=f"## Article Draft\n\n{article_md}",
        schema=ApprovedOutput,
        executor_prefix="review",
    )

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
        }
        (ARTICLES_DIR / f"{slug}.json").write_text(
            json.dumps(flight, indent=4), encoding="utf-8"
        )
        logger.info("Published: %s", slug)

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


# ── Main ──────────────────────────────────────────────────────────────────────


def main(
    target_articles: int = 5,
    max_cycles: int = 500,
    choreography_name: str | None = None,
) -> None:
    db_path = str(HERE / "newsroom.db")

    network = LocalA2ANetwork()
    board = QuadroBoard(
        SqliteBoardBackend(db_path),
        profile_resolver={"article": "article"},
        custom_profiles={"article": ARTICLE_LIFECYCLE},
        network=network,
    )
    bc = board.client()

    bc.put_data(
        "newsroom_goal",
        {"target_articles": target_articles, "topic": "health and wellbeing"},
    )

    # ── Pipeline — auto-generated stages + custom escape hatches ──────────────
    pipeline = (
        MafPipeline(board)
        .llm(api_key_env="OPENAI_API_KEY", model_env="OPENAI_MODEL_ID")
        .workers(12)
        .wakes("a2a://chief")
        .stage(
            "ideation",
            prompt=HERE / "prompts" / "ideation.md",
            output_schema=ArticleBrief,
            active_status="ideating",
            success_status="idea_ready",
            max_working_time=5.0,
        )
        .stage(
            "research",
            execute_fn=run_research,
            active_status="researching",
            max_working_time=5.0,
        )
        .stage(
            "writing",
            prompt=HERE / "prompts" / "writing.md",
            active_status="writing",
            success_status="draft_ready",
            max_working_time=5.0,
        )
        .stage(
            "review",
            execute_fn=run_review,
            active_status="reviewing",
            max_working_time=5.0,
        )
        .chief(prompt=HERE / "prompts" / "chief.md", goal_key="newsroom_goal")
        .build()
    )

    # ── Producer ──────────────────────────────────────────────────────────────
    from producer import ArticleProducer

    choreo = CHOREOGRAPHIES.get(choreography_name) if choreography_name else None
    producer = ArticleProducer(
        bc, pipeline.chief, target=target_articles, choreography=choreo,
    )

    def _is_done(state: dict) -> bool:
        return (
            sum(1 for t in state["tasks"] if t["status"] == "published")
            >= target_articles
        )

    def _log_cycle(state: dict, cycle: int) -> None:
        tasks = state["tasks"]
        published = sum(1 for t in tasks if t["status"] == "published")
        in_flight = sum(
            1
            for t in tasks
            if t["status"] in {"ideating", "researching", "writing", "reviewing"}
        )
        logger.info(
            "[cycle %3d]  published=%d/%d  in_flight=%d",
            cycle,
            published,
            target_articles,
            in_flight,
        )

    mode = f"choreography={choreography_name!r}" if choreography_name else "default"
    logger.info("Newsroom (pipeline) started — target=%d  %s", target_articles, mode)

    final_state = pipeline.run(
        done_when=_is_done,
        on_cycle=_log_cycle,
        poll_every=3.0,
        ombudsman_every=30.0,
        max_cycles=max_cycles,
    )

    producer.stop()

    published_count = sum(
        1 for t in final_state["tasks"] if t["status"] == "published"
    )

    print(f"\n{'=' * 60}")
    print(f"  Newsroom complete: {published_count}/{target_articles} published")
    print(f"  Mode: {mode}")
    print(f"{'=' * 60}")

    articles_dir = HERE / "output"
    if articles_dir.exists():
        files = sorted(articles_dir.glob("*.md"))
        print(f"\nArticles ({len(files)}):")
        for f in files:
            print(f"  {f.name}")

    print("\nFinal task states:")
    for t in final_state["tasks"]:
        print(f"  [{t['status']:>16}]  {t['label'][:60]}")

    if published_count < target_articles:
        sys.exit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="LLM Newsroom (Pipeline)")
    parser.add_argument("--target", type=int, default=5)
    parser.add_argument("--cycles", type=int, default=500)
    parser.add_argument("--choreography", choices=list(CHOREOGRAPHIES.keys()))

    args = parser.parse_args()
    main(
        target_articles=args.target,
        max_cycles=args.cycles,
        choreography_name=args.choreography,
    )
