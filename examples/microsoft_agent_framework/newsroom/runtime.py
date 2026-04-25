from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from quadro import LifecycleBuilder, QuadroRuntime

from producer import ArticleProducer

HERE = Path(__file__).parent
DB_PATH = HERE / "newsroom.db"
ARTICLES_DIR = HERE / "output"
GOAL_KEY = "newsroom_goal"
CHIEF_URL = "a2a://chief"
WORKER_COUNT = 12
DEFAULT_MAX_CYCLES = 500
TOPIC = "health and wellbeing"

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

ACTIVE_STATUSES = frozenset({"ideating", "researching", "writing", "reviewing"})
PENDING_STATUSES = frozenset(
    {"UNASSIGNED", "idea_ready", "research_ready", "draft_ready"}
)


def build_runtime(
    *,
    target_articles: int,
    max_cycles: int = DEFAULT_MAX_CYCLES,
    lifecycle: object | None = None,
) -> QuadroRuntime:
    active_lifecycle = lifecycle or ARTICLE_LIFECYCLE
    runtime = QuadroRuntime.sqlite(
        DB_PATH,
        profile_resolver={"article": "article"},
        custom_profiles={"article": active_lifecycle},
    )
    runtime.put_data(
        GOAL_KEY,
        {
            "target_articles": target_articles,
            "topic": TOPIC,
        },
    )
    return runtime.max_cycles(max_cycles)


def start_article_producer(
    runtime: QuadroRuntime,
    chief: Any,
    *,
    target_articles: int,
    choreography_name: str | None,
) -> ArticleProducer:
    choreo = CHOREOGRAPHIES.get(choreography_name) if choreography_name else None
    producer = ArticleProducer(
        runtime.client,
        chief,
        target=target_articles,
        choreography=choreo,
    )
    runtime.add_shutdown_hook(producer.stop)
    return producer


def make_done_when(target_articles: int) -> Callable[[dict], bool]:
    def _is_done(state: dict) -> bool:
        return (
            sum(1 for t in state["tasks"] if t["status"] == "published")
            >= target_articles
        )

    return _is_done


def make_cycle_logger(
    logger: logging.Logger,
    *,
    target_articles: int,
    producer: ArticleProducer,
) -> Callable[[dict, int], None]:
    def _log_cycle(state: dict, cycle: int) -> None:
        tasks = state["tasks"]
        published = sum(1 for t in tasks if t["status"] == "published")
        in_flight = sum(1 for t in tasks if t["status"] in ACTIVE_STATUSES)
        pending = sum(1 for t in tasks if t["status"] in PENDING_STATUSES)
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

    return _log_cycle


def mode_label(choreography_name: str | None) -> str:
    return f"choreography={choreography_name!r}" if choreography_name else "default"


def print_final_summary(final_state: dict, *, target_articles: int, mode: str) -> int:
    published_count = sum(1 for t in final_state["tasks"] if t["status"] == "published")

    print(f"\n{'=' * 60}")
    print(f"  Newsroom complete: {published_count}/{target_articles} published")
    print(f"  Mode: {mode}")
    print(f"{'=' * 60}")

    if ARTICLES_DIR.exists():
        files = sorted(ARTICLES_DIR.glob("*.md"))
        print(f"\nArticles ({len(files)}):")
        for f in files:
            print(f"  {f.name}")

    print("\nFinal task states:")
    for t in final_state["tasks"]:
        print(f"  [{t['status']:>16}]  {t['label'][:60]}")

    return published_count


def load_lifecycle_override(path: str) -> object:
    from quadro.board.lifecycle_loader import load_lifecycle

    _name, lifecycle = load_lifecycle(path)
    return lifecycle
