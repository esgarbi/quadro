"""
LLM Newsroom — streamlined entry point using the MafPipeline adapter.

Demonstrates the same newsroom behavior as main.py with less framework wiring.
Domain behavior lives in agents.py; this file shows how MafPipeline assembles
the board, workers, chief, producer, and run loop.

Usage:
    python main_pipeline.py
    python main_pipeline.py --target 5
    python main_pipeline.py --target 10 --choreography sleep_study

For the full-control version with manual WorkerPool/ChiefAgent wiring, see main.py.

Board UI (second terminal):
    python -m quadro.ui examples/microsoft_agent_framework/newsroom/newsroom.db
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

from quadro.integrations.maf import MafPipeline

from agents import run_ideation, run_research, run_review, run_writing
from runtime import (
    CHIEF_URL,
    CHOREOGRAPHIES,
    DEFAULT_MAX_CYCLES,
    GOAL_KEY,
    HERE,
    WORKER_COUNT,
    build_default_sponsor,
    build_runtime,
    load_lifecycle_override,
    make_cycle_logger,
    mode_label,
    print_final_summary,
    start_article_producer,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("newsroom")


# ── Main ──────────────────────────────────────────────────────────────────────


def main(
    target_articles: int = 5,
    max_cycles: int = DEFAULT_MAX_CYCLES,
    choreography_name: str | None = None,
    lifecycle: object | None = None,
) -> None:
    runtime = build_runtime(
        target_articles=target_articles,
        max_cycles=max_cycles,
        lifecycle=lifecycle,
    )

    # ── Pipeline — same worker behavior as main.py, less framework wiring ─────
    # Explicit execute_fns own their LLM calls in agents.py. MafPipeline.llm()
    # configures the MAF chief and any prompt/schema auto-stages added later.
    pipeline = (
        MafPipeline(runtime.board)
        .llm(api_key_env="OPENAI_API_KEY", model_env="OPENAI_MODEL_ID")
        .workers(WORKER_COUNT)
        .wakes(CHIEF_URL)
        .stage(
            "ideation",
            execute_fn=run_ideation,
            active_status="ideating",
            tool_name="advance_to_ideation",
            max_working_time=5.0,
        )
        .stage(
            "research",
            execute_fn=run_research,
            active_status="researching",
            tool_name="advance_to_research",
            max_working_time=5.0,
        )
        .stage(
            "writing",
            execute_fn=run_writing,
            active_status="writing",
            tool_name="advance_to_writing",
            max_working_time=5.0,
        )
        .stage(
            "review",
            execute_fn=run_review,
            active_status="reviewing",
            tool_name="advance_to_review",
            max_working_time=5.0,
        )
        .chief(prompt=HERE / "prompts" / "chief.md", goal_key=GOAL_KEY)
        .build()
    )

    # ── Producer ──────────────────────────────────────────────────────────────
    producer = start_article_producer(
        runtime,
        pipeline.chief,
        target_articles=target_articles,
        choreography_name=choreography_name,
    )
    mode = mode_label(choreography_name)
    logger.info("Newsroom (pipeline) started — target=%d  %s", target_articles, mode)

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
        .run(pipeline)
    )

    published_count = print_final_summary(
        final_state,
        target_articles=target_articles,
        mode=mode,
    )

    if published_count < target_articles:
        sys.exit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="LLM Newsroom (Pipeline)")
    parser.add_argument("--target", type=int, default=5)
    parser.add_argument("--cycles", type=int, default=DEFAULT_MAX_CYCLES)
    parser.add_argument("--choreography", choices=list(CHOREOGRAPHIES.keys()))
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
