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
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # shared.py
sys.path.insert(0, str(Path(__file__).parent))

from quadro import (
    ChiefAgent,
    WorkerPool,
)

from agents import (
    build_chief_policy,
    run_ideation,
    run_research,
    run_review,
    run_writing,
)
from runtime import (
    CHIEF_URL,
    CHOREOGRAPHIES,
    DEFAULT_MAX_CYCLES,
    WORKER_COUNT,
    build_default_sponsor,
    build_runtime,
    load_lifecycle_override,
    make_cycle_logger,
    mode_label,
    print_final_summary,
    start_article_producer,
)

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("newsroom")


def main(
    target_articles: int = 5,
    max_cycles: int = DEFAULT_MAX_CYCLES,
    choreography_name: str | None = None,
    lifecycle: object | None = None,
) -> None:
    # ── Board ──────────────────────────────────────────────────────────────────
    runtime = build_runtime(
        target_articles=target_articles,
        max_cycles=max_cycles,
        lifecycle=lifecycle,
    )

    # ── Worker pool ────────────────────────────────────────────────────────────
    pool = (
        WorkerPool(runtime.client)
        .workers(WORKER_COUNT)
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
    chief_policy = build_chief_policy(runtime.client, pool.registry, pool.capacity())
    chief = ChiefAgent.builder(runtime.client).at(CHIEF_URL).policy(chief_policy).build()

    # ── Ombudsman ──────────────────────────────────────────────────────────────
    wd = pool.ombudsman()

    # ── Producer ───────────────────────────────────────────────────────────────
    producer = start_article_producer(
        runtime,
        chief,
        target_articles=target_articles,
        choreography_name=choreography_name,
    )

    # ── Run ────────────────────────────────────────────────────────────────────
    mode = mode_label(choreography_name)
    logger.info("Newsroom started — target=%d  %s", target_articles, mode)

    manual_pipeline = SimpleNamespace(chief=chief, ombudsman=wd)
    final_state = (
        runtime.sponsor(
            build_default_sponsor(
                target_articles=target_articles, max_cycles=max_cycles
            )
        )
        .on_cycle(
            make_cycle_logger(
                logger,
                target_articles=target_articles,
                producer=producer,
            )
        )
        .poll_every(3.0)
        .ombudsman_every(30.0)
        .run(manual_pipeline)
    )

    # ── Final summary ──────────────────────────────────────────────────────────
    published_count = print_final_summary(
        final_state,
        target_articles=target_articles,
        mode=mode,
    )

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
        "--cycles", type=int, default=DEFAULT_MAX_CYCLES, help="Maximum run loop cycles"
    )
    parser.add_argument(
        "--choreography",
        choices=list(CHOREOGRAPHIES.keys()),
        help="Named choreography — overrides default uniform batching",
    )
    parser.add_argument(
        "--lifecycle",
        type=str,
        default=None,
        help="Path to a .lifecycle.toml file (overrides built-in lifecycle)",
    )

    args = parser.parse_args()

    lifecycle_override = (
        load_lifecycle_override(args.lifecycle) if args.lifecycle else None
    )

    main(
        target_articles=args.target,
        max_cycles=args.cycles,
        choreography_name=args.choreography,
        lifecycle=lifecycle_override,
    )
