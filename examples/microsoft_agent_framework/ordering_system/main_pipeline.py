"""
Ordering System — streamlined entry point using the MafPipeline adapter.

Demonstrates the same ordering pipeline as main.py but with ~70% less
wiring code. The adapter auto-generates worker execute_fns, chief tools,
and the chief policy from the lifecycle graph and stage declarations.

Usage:
    python main_pipeline.py
    python main_pipeline.py --target 10 --profile burst

For the full-control version with custom execute_fns and hand-written
chief tools, see main.py (the "advanced" example).

Board UI (second terminal):
    python -m quadro.ui examples/microsoft_agent_framework/ordering_system/orders.db
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

from quadro import LifecycleBuilder, QuadroRuntime
from quadro.board.backends import SqliteBoardBackend
from quadro.integrations.maf import MafPipeline
from quadro.sponsor import AllOf, GoalSponsor, TickBudgetSponsor

from data import INITIAL_WAREHOUSE, PRODUCT_CATALOG
from producer import ChoreographyStep, OrderProducer
from schemas import InventoryCheck, OrderValidation, ProcurementResult, ShippingLabel

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
    .step("UNASSIGNED", "validating")
    .step("validating", "validated")
    .branch("validating", "validation_failed")
    .step("validated", "checking_stock")
    .step("checking_stock", "stock_confirmed")
    .branch("checking_stock", "needs_procurement")
    .step("needs_procurement", "procuring")
    .step("procuring", "procured")
    .loop("procured", "checking_stock")
    .step("stock_confirmed", "shipping")
    .step("shipping", "shipped")
    .build()
)


def main(
    target_shipped: int = 10,
    max_cycles: int = 1000,
    profile: str = "steady",
    choreography_name: str | None = None,
) -> None:
    db_path = str(HERE / "orders.db")

    runtime = QuadroRuntime(SqliteBoardBackend(db_path)).with_profiles(
        profile_resolver={"order": "order"},
        custom_profiles={"order": ORDER_LIFECYCLE},
    )
    bc = runtime.client

    runtime.put_data(
        "order_goal",
        {"target_shipped": target_shipped, "domain": "electronics e-commerce"},
    )
    runtime.put_data("product_catalog", PRODUCT_CATALOG)
    runtime.put_data("warehouse", dict(INITIAL_WAREHOUSE))

    # ── Pipeline — all wiring in one fluent chain ─────────────────────────────
    pipeline = (
        MafPipeline(runtime.board)
        .llm(api_key_env="OPENAI_API_KEY", model_env="OPENAI_MODEL_ID")
        .workers(4)
        .capacity(2)
        .wakes("a2a://chief")
        .stage(
            "validation",
            prompt=HERE / "prompts" / "validation.md",
            output_schema=OrderValidation,
            active_status="validating",
            success_status="validated",
            failure_status="validation_failed",
            max_working_time=2.0,
        )
        .stage(
            "inventory",
            prompt=HERE / "prompts" / "inventory.md",
            output_schema=InventoryCheck,
            active_status="checking_stock",
            success_status="stock_confirmed",
            failure_status="needs_procurement",
            max_working_time=0.5,
        )
        .stage(
            "procurement",
            prompt=HERE / "prompts" / "procurement.md",
            output_schema=ProcurementResult,
            active_status="procuring",
            success_status="procured",
            max_working_time=10.0,
        )
        .stage(
            "logistics",
            prompt=HERE / "prompts" / "logistics.md",
            output_schema=ShippingLabel,
            active_status="shipping",
            success_status="shipped",
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

    def _is_done(state: dict) -> bool:
        return (
            sum(1 for t in state["tasks"] if t["status"] == "shipped") >= target_shipped
        )

    def _log_cycle(state: dict, cycle: int) -> None:
        tasks = state["tasks"]
        shipped = sum(1 for t in tasks if t["status"] == "shipped")
        active = sum(1 for t in tasks if t["status"] not in _TERMINAL)
        logger.info(
            "[cycle %3d]  shipped=%d/%d  active=%d",
            cycle,
            shipped,
            target_shipped,
            active,
        )

    mode = (
        f"choreography={choreography_name!r}"
        if choreography_name
        else f"profile={profile!r}"
    )
    logger.info(
        "Ordering system (pipeline) started — target=%d  %s",
        target_shipped,
        mode,
    )

    final_state = (
        runtime.sponsor(
            AllOf(
                GoalSponsor(_is_done),
                TickBudgetSponsor(max_cycles),
            )
        )
        .on_cycle(_log_cycle)
        .poll_every(3.0)
        .ombudsman_every(30.0)
        .run(pipeline)
    )

    tasks = final_state["tasks"]
    shipped_count = sum(1 for t in tasks if t["status"] == "shipped")

    print(f"\n{'=' * 60}")
    print("  Ordering system complete")
    print(f"  Shipped: {shipped_count}/{target_shipped}")
    print(f"  Mode: {mode}")
    print(f"{'=' * 60}")

    for t in tasks:
        print(f"  [{t['status']:>20}]  {t['label'][:60]}")

    if shipped_count < target_shipped:
        sys.exit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="LLM Ordering System (Pipeline)")
    parser.add_argument("--target", type=int, default=10)
    parser.add_argument("--cycles", type=int, default=1000)

    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--profile",
        default="steady",
        choices=["burst", "steady", "slow", "wave", "drought", "idle"],
    )
    mode.add_argument("--choreography", choices=list(CHOREOGRAPHIES.keys()))

    args = parser.parse_args()
    main(
        target_shipped=args.target,
        max_cycles=args.cycles,
        profile=args.profile,
        choreography_name=args.choreography,
    )
