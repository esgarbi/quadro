from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

from quadro import LifecycleBuilder, QuadroRuntime
from quadro.board.backends import SqliteBoardBackend

from producer import ArticleProducer


import dotenv

dotenv.load_dotenv()

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
    .phase("UNASSIGNED", "ideating")
    .phase("ideating", "idea_ready")
    .phase("idea_ready", "researching")
    .phase("researching", "research_ready")
    .phase("research_ready", "writing")
    .phase("writing", "draft_ready")
    .phase("draft_ready", "reviewing")
    .phase("reviewing", "published")
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
    """Build a runtime pre-wired with the article profile and goal data.

    ``max_cycles`` is retained on the signature for backward-compatible call
    sites; callers should wrap their Sponsor with ``TickBudgetSponsor(max_cycles)``
    (see :func:`build_default_sponsor`) — ``QuadroRuntime`` no longer consumes
    ``max_cycles`` directly.
    """
    # ── Silence non-actionable log noise from upstream dependencies ──────────
    # Milestone B's run note flagged three residual log-noise items. Two of
    # them are demoted here (the third — chief "no actionable work" WARNING
    # — is a real operator signal during development and stays at WARNING).

    # MAF's workflow validator INFO-logs "Dead-end executors detected" on
    # every saga.reason mini-workflow. Each such workflow is a single-executor
    # by design (saga-reason is a one-agent LLM call wrapped in a workflow),
    # so the "dead-end" flag is correct but fires on every reason step.
    # Demote to WARNING so it doesn't drown out the per-cycle summary.
    logging.getLogger("agent_framework._workflows._validation").setLevel(
        logging.WARNING
    )

    # httpx's AsyncClient.aclose logs "Event loop is closed" tracebacks
    # when its background cleanup tasks fire after the worker's one-shot
    # event loop (src/quadro/agents/worker.py:108-124) has already been
    # closed. The HTTP requests themselves succeed (the matching 200 OK
    # line is right before the traceback); this is purely cosmetic. A
    # proper fix is a shared long-lived loop for async execute_fn
    # callables — deferred to a future worker/runtime milestone.
    logging.getLogger("asyncio").setLevel(logging.CRITICAL)

    active_lifecycle = lifecycle or ARTICLE_LIFECYCLE
    runtime = (
        QuadroRuntime(SqliteBoardBackend(str(DB_PATH)))
        .with_profiles(
            profile_resolver={"article": "article"},
            custom_profiles={"article": active_lifecycle},
        )
        .with_pricing(
            {
                "gpt-5.4": {
                    "input": 2.5,
                    "output": 15.0,
                    "io_ratio": 0.30,
                }
            },
            verify_url="https://openai.com/pricing",
        )
    )
    runtime.put_data(
        GOAL_KEY,
        {
            "target_articles": target_articles,
            "topic": TOPIC,
        },
    )
    return runtime


def build_default_sponsor(
    *, target_articles: int, max_cycles: int = DEFAULT_MAX_CYCLES
):
    """Canonical Sponsor for the newsroom examples.

    Composes a :class:`GoalSponsor` (published-count target) with a
    :class:`TickBudgetSponsor` as a safety cap.
    """
    from quadro.sponsor import AllOf, GoalSponsor, TickBudgetSponsor

    return AllOf(
        GoalSponsor(make_done_when(target_articles)),
        TickBudgetSponsor(max_cycles),
    )


def start_article_producer(
    runtime: QuadroRuntime,
    *,
    target_articles: int,
    choreography_name: str | None,
) -> ArticleProducer:
    choreo = CHOREOGRAPHIES.get(choreography_name) if choreography_name else None
    producer = ArticleProducer(
        runtime.client,
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


def print_final_summary(
    final_state: dict,
    *,
    target_articles: int,
    mode: str,
    articles_dir: Path = ARTICLES_DIR,
) -> int:
    published_count = sum(1 for t in final_state["tasks"] if t["status"] == "published")

    print(f"\n{'=' * 60}")
    print(f"  Newsroom complete: {published_count}/{target_articles} published")
    print(f"  Mode: {mode}")
    print(f"{'=' * 60}")

    if articles_dir.exists():
        files = sorted(articles_dir.glob("*.md"))
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
