"""
LLM Newsroom — streamlined entry point using the Quadro substrate.

Saga definitions live in sagas.py; deterministic helpers live alongside
them. This file composes a plain :class:`quadro.Pipeline` with the MAF
adapter (reasoner + chief runtime) to assemble the board, workers,
chief, producer, and run loop.

Usage:
    python main_pipeline.py
    python main_pipeline.py --target 5
    python main_pipeline.py --target 10 --choreography sleep_study

Board UI (second terminal):
    python -m quadro.ui examples/newsroom/newsroom.db
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

import dotenv

# Load the .env sitting next to this script, regardless of the process CWD.
# `load_dotenv()` with no args only searches from CWD upward, so running the
# script from the repo root would silently miss the newsroom-specific .env.
dotenv.load_dotenv(Path(__file__).resolve().parent / ".env")

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

from agent_framework.openai import OpenAIChatClient  # noqa: E402
from quadro import Pipeline  # noqa: E402
from quadro_maf import MafChiefRuntime, MafReasoner  # noqa: E402

from runtime import (  # noqa: E402
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
from sagas import (  # noqa: E402
    ARTICLES_DIR as RUN_ARTICLES_DIR,
    ideation_saga,
    research_saga,
    review_saga,
    writing_saga,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("newsroom")


# ── Main ──────────────────────────────────────────────────────────────────────


def main(
    target_articles: int = 500,
    max_cycles: int = DEFAULT_MAX_CYCLES,
    choreography_name: str | None = None,
    lifecycle: object | None = None,
) -> None:
    runtime = build_runtime(
        target_articles=target_articles,
        max_cycles=max_cycles,
        lifecycle=lifecycle,
    )

    # ── LLM client factory — user-owned construction ─────────────────────────
    def client_factory():
        return OpenAIChatClient(
            model=os.environ.get("OPENAI_MODEL_ID", ""),
            api_key=os.environ.get("OPENAI_API_KEY", ""),
            base_url=os.environ.get("OPENAI_BASE_URL", ""),
        )

    token_reporter = runtime.meters.report_llm_tokens

    # ── Pipeline — saga stages via QuadroSagaRuntime (auto-registered),
    #    chief turns via MafChiefRuntime, saga reason steps via MafReasoner.
    pipeline = (
        Pipeline(runtime.board)
        .reasoner(
            MafReasoner(
                client_factory=client_factory,
                token_reporter=token_reporter,
            )
        )
        .with_framework_runtime(
            MafChiefRuntime(
                client_factory=client_factory,
                token_reporter=token_reporter,
            )
        )
        .runtime_observability(token_reporter=token_reporter)
        .workers(WORKER_COUNT)
        .wakes(CHIEF_URL)
        # Each saga's final deterministic step calls ``board.update_task``
        # itself (see ``_persist_brief`` / ``_merge_research_into_task_output``
        # / ``_merge_draft_into_task_output`` / ``_write_files_and_mark_published``
        # / ``_route_back_to_idea_ready`` in sagas.py). None of the stages
        # set ``success_status``, so the pipeline's post-stage update is
        # skipped (milestone-B post-run fix to
        # ``Pipeline._make_runtime_execute_fn``) and each saga owns its
        # own commit point. The alternative — pipeline-managed transitions
        # driven by ``success_status`` — would cause a self-transition
        # collision with the saga's own write and is left for a later
        # refactor. See the milestone-C run note for the brief-vs-code
        # divergence on ``success_status``.
        .stage(
            "ideation",
            saga=ideation_saga,
            active_status="ideating",
            tool_name="advance_to_ideation",
            max_working_time=5.0,
        )
        .stage(
            "research",
            saga=research_saga,
            active_status="researching",
            tool_name="advance_to_research",
            max_working_time=15.0,
        )
        .stage(
            "writing",
            saga=writing_saga,
            active_status="writing",
            tool_name="advance_to_writing",
            max_working_time=10.0,
        )
        .stage(
            "review",
            saga=review_saga,
            active_status="reviewing",
            tool_name="advance_to_review",
            max_working_time=10.0,
        )
        .chief(prompt=HERE / "prompts" / "chief.md", goal_key=GOAL_KEY)
        .build()
    )

    # ── Producer ──────────────────────────────────────────────────────────────
    producer = start_article_producer(
        runtime,
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
        articles_dir=RUN_ARTICLES_DIR,
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
