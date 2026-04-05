"""
LLM Newsroom — entry point.

Usage:
    python main.py
    python main.py --target 5
    python main.py --target 10 --choreography sleep_study

Named choreographies (batch_size, wait_minutes):
    sleep_study   6 articles in 3 waves, 5 min apart
    wave_study    7 articles — front-loaded burst then slower waves

Board UI (second terminal):
    python -m quadro.ui examples/microsoft_agent_framework/newsroom/newsroom.db
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # shared.py
sys.path.insert(0, str(Path(__file__).parent))

from quadro import (
    ChiefAgent,
    LifecycleBuilder,
    LocalA2ANetwork,
    QuadroBoard,
    RunLoop,
    WorkerPool,
)
from quadro.board.backends.sqlite import SqliteBoardBackend

from agents import (
    build_chief_policy,
    run_ideation,
    run_research,
    run_review,
    run_writing,
)
from producer import ArticleProducer

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("newsroom")

# ── Named choreographies: list of (batch_size, wait_minutes) ──────────────────
CHOREOGRAPHIES: dict[str, list[tuple[int, float]]] = {
    "sleep_study": [(2, 0.0), (2, 5.0), (2, 5.0)],
    "wave_study": [(3, 0.0), (2, 8.0), (2, 8.0)],
}

# ── Article lifecycle ──────────────────────────────────────────────────────────
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


def main(
    target_articles: int = 20,
    max_cycles: int = 1500,
    choreography_name: str | None = None,
) -> None:
    HERE = Path(__file__).parent
    db_path = str(HERE / "newsroom.db")

    # ── Board ──────────────────────────────────────────────────────────────────
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
        {
            "target_articles": target_articles,
            "topic": "health and wellbeing",
        },
    )

    # ── Worker pool ────────────────────────────────────────────────────────────
    CHIEF_URL = "a2a://chief"
    POOL_SIZE = 12

    pool = (
        WorkerPool(bc)
        .workers(POOL_SIZE)
        .wakes(CHIEF_URL)
        .add("ideation", run_ideation, active_status="ideating", max_working_time=5.0)
        .add(
            "research", run_research, active_status="researching", max_working_time=5.0
        )
        .add("writing", run_writing, active_status="writing", max_working_time=5.0)
        .add("review", run_review, active_status="reviewing", max_working_time=5.0)
        .build()
    )

    # ── Chief ──────────────────────────────────────────────────────────────────
    chief_policy = build_chief_policy(bc, pool.registry, pool.capacity())
    chief = ChiefAgent.builder(bc).at(CHIEF_URL).policy(chief_policy).build()

    # ── Ombudsman ──────────────────────────────────────────────────────────────
    wd = pool.ombudsman()

    # ── Producer ───────────────────────────────────────────────────────────────
    choreo = CHOREOGRAPHIES.get(choreography_name) if choreography_name else None
    producer = ArticleProducer(
        bc,
        chief,
        target=target_articles,
        choreography=choreo,
    )

    # ── Completion predicate ───────────────────────────────────────────────────
    def _is_done(state: dict) -> bool:
        return (
            sum(1 for t in state["tasks"] if t["status"] == "published")
            >= target_articles
        )

    # ── Per-cycle log ──────────────────────────────────────────────────────────
    def _log_cycle(state: dict, cycle: int) -> None:
        tasks = state["tasks"]
        published = sum(1 for t in tasks if t["status"] == "published")
        in_flight = sum(
            1
            for t in tasks
            if t["status"] in {"ideating", "researching", "writing", "reviewing"}
        )
        pending = sum(
            1
            for t in tasks
            if t["status"]
            in {"UNASSIGNED", "idea_ready", "research_ready", "draft_ready"}
        )
        stats = producer.stats
        logger.info(
            "[cycle %3d]  published=%d/%d  in_flight=%d  pending=%d"
            "  producer=[posted=%d/%d]",
            cycle,
            published,
            target_articles,
            in_flight,
            pending,
            stats["posted"],
            stats["target"],
        )

    # ── Run ────────────────────────────────────────────────────────────────────
    mode = f"choreography={choreography_name!r}" if choreography_name else "default"
    logger.info("Newsroom started — target=%d  %s", target_articles, mode)

    final_state = (
        RunLoop(board, chief)
        .done_when(_is_done)
        .on_cycle(_log_cycle)
        .ombudsman(wd)
        .poll_every(3.0)
        .ombudsman_every(30.0)
        .max_cycles(max_cycles)
        .run()
    )

    producer.stop()

    # ── Final summary ──────────────────────────────────────────────────────────
    published_count = sum(1 for t in final_state["tasks"] if t["status"] == "published")

    print(f"\n{'═' * 60}")
    print(f"  Newsroom complete: {published_count}/{target_articles} published")
    print(f"  Mode: {mode}")
    print(f"{'═' * 60}")

    articles_dir = HERE / "articles"
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
    parser = argparse.ArgumentParser(
        description="LLM Health Newsroom",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--target", type=int, default=5, help="Target number of published articles"
    )
    parser.add_argument(
        "--cycles", type=int, default=500, help="Maximum run loop cycles"
    )
    parser.add_argument(
        "--choreography",
        choices=list(CHOREOGRAPHIES.keys()),
        help="Named choreography — overrides default uniform batching",
    )

    args = parser.parse_args()
    main(
        target_articles=args.target,
        max_cycles=args.cycles,
        choreography_name=args.choreography,
    )
