"""
Ordering System — never-ending LLM-token-budgeted pipeline.

The system processes orders continuously until the LLM token budget
(default 1 M tokens) is exhausted.  A ``LlmTokenBudgetSponsor``
governs the runtime lifetime; the ``OrderProducer`` keeps emitting
orders in the background, and the pipeline ships as many as the
budget allows.

All four pipeline stages (validation, inventory, procurement, logistics)
are sagas. The saga DSL's compensation rollback (milestone D) makes
the side-effecting stages reversible: if a later step fails, the
runtime walks each completed step's registered ``.compensate(...)``
undo in reverse order. See ``sagas.py`` for the saga definitions and
the per-step compensation bodies.

Usage:
    python main_pipeline.py
    python main_pipeline.py --token-budget 500000 --profile burst
    python main_pipeline.py --inject-failure dispatch_shipment

When ``--inject-failure <step>`` is provided, the named saga step's
first invocation raises synthetically so the compensation walker can
be demonstrated end to end. Legal values are any step name declared
across the four sagas; ``reserve_units``, ``procure_units``, and
``dispatch_shipment`` are the canonical demo targets because each
has a registered compensation.

Board UI (second terminal):
    python -m quadro.ui examples/ordering/orders.db
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

from agent_framework.openai import OpenAIChatClient                              # noqa: E402
from quadro import LifecycleBuilder, Pipeline, QuadroRuntime                     # noqa: E402
from quadro.board.backends import SqliteBoardBackend                             # noqa: E402
from quadro.sponsor import LlmTokenBudgetSponsor                                # noqa: E402
from quadro_maf import MafChiefRuntime, MafReasoner                              # noqa: E402

from data import INITIAL_WAREHOUSE, PRODUCT_CATALOG                              # noqa: E402
from producer import ChoreographyStep, OrderProducer                             # noqa: E402
from sagas import build_sagas                                                    # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("ordering_system")

HERE = Path(__file__).parent

CHOREOGRAPHIES: dict[str, list[ChoreographyStep]] = {
    "sleep_study": [
        ("steady", 30),
        ("idle", 220),
        ("steady", 60),
        ("idle", 120),
        ("steady", 60),
        ("idle", 120),
    ],
    "wave_study": [
        ("burst", 30),
        ("idle", 90),
        ("slow", 60),
        ("idle", 90),
        ("burst", 30),
        ("idle", 90),
    ],
    "endurance": [
        ("steady", 120),
        ("drought", 180),
        ("burst", 60),
        ("idle", 60),
    ],
}

ORDER_LIFECYCLE = (
    LifecycleBuilder()
    .phase("UNASSIGNED", "validating")
    .phase("validating", "validated")
    .branch("validating", "validation_failed")
    .phase("validated", "checking_stock")
    .phase("checking_stock", "stock_confirmed")
    .branch("checking_stock", "needs_procurement")
    .phase("needs_procurement", "procuring")
    .phase("procuring", "procured")
    .loop("procured", "checking_stock")
    .phase("stock_confirmed", "shipping")
    .phase("shipping", "shipped")
    .build()
)

# Step names across all four sagas that accept ``--inject-failure``.
# Restricted to steps that have a registered ``.compensate(...)`` so
# the flag actually exercises the rollback walker, not a plain
# step-failed termination.
_INJECT_FAILURE_CHOICES = [
    "reserve_units",
    "procure_units",
    "dispatch_shipment",
]


def _format_tokens(n: int) -> str:
    """K/M suffix formatting matching the Board UI's ``formatTokens``."""
    if n < 1000:
        return f"{n:,}"
    if n < 10_000:
        return f"{n / 1000:.1f}K"
    if n < 1_000_000:
        return f"{round(n / 1000)}K"
    return f"{n / 1_000_000:.1f}M"


def main(
    token_budget: int = 1_000_000,
    profile: str = "agressive",
    choreography_name: str | None = None,
    inject_failure: str | None = None,
) -> None:
    db_path = str(HERE / "orders.db")

    runtime = QuadroRuntime(SqliteBoardBackend(db_path)).with_profiles(
        profile_resolver={"order": "order"},
        custom_profiles={"order": ORDER_LIFECYCLE},
    ).with_pricing(
        {
            "gpt-5.4": {
                "input": 2.5,
                "output": 15.0,
                "io_ratio": 0.30,
            }
        },
        verify_url="https://openai.com/pricing",
    )
    bc = runtime.client

    runtime.put_data(
        "order_goal",
        {"token_budget": token_budget, "domain": "electronics e-commerce"},
    )
    runtime.put_data("product_catalog", PRODUCT_CATALOG)
    runtime.put_data("warehouse", dict(INITIAL_WAREHOUSE))

    sagas = build_sagas(inject_failure=inject_failure)

    def client_factory():
        return OpenAIChatClient(
            model=os.environ.get("OPENAI_MODEL_ID", ""),
            api_key=os.environ.get("OPENAI_API_KEY", ""),
            base_url=os.environ.get("OPENAI_BASE_URL", ""),
        )

    token_reporter = runtime.meters.report_llm_tokens

    # ── Pipeline — all four stages are sagas now ─────────────────────────────
    #
    # The saga's final deterministic step performs its own
    # ``board.update_task`` with the appropriate lifecycle transition
    # (validated / stock_confirmed / procured / shipped). No
    # ``success_status`` on the stage specs — the pipeline skips its
    # post-stage update (milestone-B post-run fix in
    # ``Pipeline._make_runtime_execute_fn``), leaving each saga as the
    # single commit point for its stage.
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
        .workers(4)
        .capacity(2)
        .wakes("a2a://chief")
        .stage(
            "validation",
            saga=sagas["validation"],
            active_status="validating",
            max_working_time=2.0,
        )
        .stage(
            "inventory",
            saga=sagas["inventory"],
            active_status="checking_stock",
            max_working_time=0.5,
        )
        .stage(
            "procurement",
            saga=sagas["procurement"],
            active_status="procuring",
            max_working_time=10.0,
        )
        .stage(
            "logistics",
            saga=sagas["logistics"],
            active_status="shipping",
            max_working_time=5.0,
        )
        .chief(prompt=HERE / "prompts" / "chief.md", goal_key="order_goal")
        .build()
    )

    # ── Producer ──────────────────────────────────────────────────────────────
    if choreography_name:
        choreo = CHOREOGRAPHIES[choreography_name]
        producer = OrderProducer(bc, choreography=choreo)
    else:
        producer = OrderProducer(bc, profile=profile)
    runtime.add_shutdown_hook(producer.stop)

    _TERMINAL = frozenset({"shipped", "validation_failed", "HUMAN_REVIEW", "abandoned"})

    def _log_cycle(state: dict, cycle: int) -> None:
        tasks = state["tasks"]
        shipped = sum(1 for t in tasks if t["status"] == "shipped")
        active = sum(1 for t in tasks if t["status"] not in _TERMINAL)
        status = state["data"].get("_sponsor_status") or {}
        meters = status.get("meters") or {}
        tokens = int(meters.get("llm_tokens") or 0)
        logger.info(
            "[cycle %3d]  tokens=%s/%s  shipped=%d  active=%d",
            cycle,
            _format_tokens(tokens),
            _format_tokens(token_budget),
            shipped,
            active,
        )

    mode = (
        f"choreography={choreography_name!r}"
        if choreography_name
        else f"profile={profile!r}"
    )
    logger.info(
        "Ordering system started — token_budget=%s  %s%s",
        _format_tokens(token_budget),
        mode,
        f"  inject_failure={inject_failure!r}" if inject_failure else "",
    )

    final_state = (
        runtime.sponsor(
            LlmTokenBudgetSponsor(token_budget)
        )
        .on_cycle(_log_cycle)
        .poll_every(3.0)
        .ombudsman_every(30.0)
        .run(pipeline)
    )

    tasks = final_state["tasks"]
    shipped_count = sum(1 for t in tasks if t["status"] == "shipped")
    tokens_final = runtime.meters.snapshot().llm_tokens

    log = final_state["data"].get("_sponsor_log") or []
    last_reason = log[-1].get("reason", "") if log else ""

    print(f"\n{'=' * 60}")
    print("  Ordering system complete")
    print(f"  Tokens used: {_format_tokens(tokens_final)} / {_format_tokens(token_budget)}")
    print(f"  Orders shipped: {shipped_count}")
    print(f"  Stop reason: {last_reason}")
    print(f"  Mode: {mode}")
    if inject_failure:
        print(f"  Failure injection: {inject_failure}")
    print(f"{'=' * 60}")

    for t in tasks:
        print(f"  [{t['status']:>20}]  {t['label'][:60]}")

    if inject_failure:
        print("\n── Compensation rollback summary ──────────────────────────────")
        for task in tasks:
            state_key = f"_saga:{task['task_id']}"
            saga_state = final_state["data"].get(state_key)
            if not isinstance(saga_state, dict):
                continue
            comp_log = saga_state.get("compensations_run") or []
            if not comp_log:
                continue
            print(f"  Task {task['task_id'][:8]}  ({task['status']})")
            for entry in comp_log:
                step = entry.get("step", "?")
                outcome = entry.get("outcome", "?")
                ms = entry.get("duration_ms", 0)
                print(f"    - {step:<20}  outcome={outcome}  {ms}ms")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="LLM Ordering System — token-budgeted pipeline",
    )
    parser.add_argument(
        "--token-budget",
        type=int,
        default=1_000_000,
        help="Total LLM token budget (default: 1,000,000)",
    )

    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument(
        "--profile",
        default="steady",
        choices=["burst", "steady", "slow", "wave", "drought", "idle"],
    )
    mode_group.add_argument("--choreography", choices=list(CHOREOGRAPHIES.keys()))

    parser.add_argument(
        "--inject-failure",
        type=str,
        default=None,
        choices=_INJECT_FAILURE_CHOICES,
        help=(
            "Synthetically fail the named saga step's first invocation; "
            "exercises compensation rollback end-to-end."
        ),
    )

    args = parser.parse_args()
    main(
        token_budget=args.token_budget,
        profile=args.profile,
        choreography_name=args.choreography,
        inject_failure=args.inject_failure,
    )
