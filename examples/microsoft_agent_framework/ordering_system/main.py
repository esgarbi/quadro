"""
LLM Ordering System — entry point.

Usage:
    python main.py
    python main.py --target 10 --profile burst
    python main.py --target 10 --choreography sleep_study
    python main.py --target 20 --choreography wave_study

Profiles (single-mode):
    burst    Rapid batches — Chief wakes constantly
    steady   Regular batches — even cadence (default)
    slow     Occasional orders — long Chief sleeps
    wave     Alternating bursts and silence
    drought  Very rare orders — tests stale task detection
    idle     No orders — Chief stays asleep

Choreographies (automatic cycling, hands-free):
    sleep_study   steady(60s) → idle(120s) × 3 cycles
                  Captures clear before/after sleep pattern change
    wave_study    burst(30s) → idle(90s) → slow(60s) → idle(90s)
                  Shows three distinct Chief rhythms back-to-back
    endurance     steady(120s) → drought(180s) → burst(60s) → idle(60s)
                  Full run for watching stale detection + recovery

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
    run_inventory,
    run_logistics,
    run_procurement,
    run_validation,
)
from data import INITIAL_WAREHOUSE, PRODUCT_CATALOG
from producer import ChoreographyStep, OrderProducer

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("ordering_system")

# ── Named choreographies ───────────────────────────────────────────────────────
# Each is a list of (profile, duration_seconds) steps that cycle automatically.
# Designed to produce visually distinct sleep patterns in the Board UI sparkline.

CHOREOGRAPHIES: dict[str, list[ChoreographyStep]] = {
    "sleep_study": [
        ("steady", 30),  # 1 min: regular orders — Chief wakes rhythmically
        ("idle", 220),  # 2 min: no orders — Chief sleeps, long gap in sparkline
        ("steady", 60),  # 1 min: resume — Chief wakes again
        ("idle", 120),  # 2 min: pause again — second long sleep
        ("steady", 60),  # 1 min: final burst
        ("idle", 120),  # 2 min: final sleep
    ],
    "wave_study": [
        ("burst", 30),  # 30s: rapid orders — many short Chief cycles
        ("idle", 90),  # 90s: silence — long sleep gap
        ("slow", 60),  # 60s: slow trickle — occasional Chief wakes
        ("idle", 90),  # 90s: silence again
        ("burst", 30),  # 30s: another burst
        ("idle", 90),  # 90s: final silence
    ],
    "endurance": [
        ("steady", 120),  # 2 min: normal operation
        ("drought", 180),  # 3 min: near-silence — Ombudsman should fire on stale tasks
        ("burst", 60),  # 1 min: recovery burst
        ("idle", 60),  # 1 min: cooldown
    ],
}

# ── Order lifecycle ────────────────────────────────────────────────────────────
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
    lifecycle: object | None = None,
) -> None:
    HERE = Path(__file__).parent
    db_path = str(HERE / "orders.db")

    active_lifecycle = lifecycle or ORDER_LIFECYCLE

    # ── Board ──────────────────────────────────────────────────────────────────
    network = LocalA2ANetwork()
    board = QuadroBoard(
        SqliteBoardBackend(db_path),
        profile_resolver={"order": "order"},
        custom_profiles={"order": active_lifecycle},
        network=network,
    )
    bc = board.client()

    bc.put_data(
        "order_goal",
        {
            "target_shipped": target_shipped,
            "domain": "electronics e-commerce",
        },
    )
    bc.put_data("product_catalog", PRODUCT_CATALOG)
    bc.put_data("warehouse", dict(INITIAL_WAREHOUSE))

    # ── Worker pool ────────────────────────────────────────────────────────────
    CHIEF_URL = "a2a://chief"
    POOL_SIZE = 4

    pool = (
        WorkerPool(bc)
        .workers(POOL_SIZE)
        .wakes(CHIEF_URL)
        .capacity(2)
        .add(
            "validation",
            run_validation,
            active_status="validating",
            max_working_time=2.0,
        )
        .add(
            "inventory",
            run_inventory,
            active_status="checking_stock",
            max_working_time=0.5,
        )
        .add(
            "procurement",
            run_procurement,
            active_status="procuring",
            max_working_time=10.0,
        )
        .add("logistics", run_logistics, active_status="shipping", max_working_time=5.0)
        .build()
    )

    # ── Chief ──────────────────────────────────────────────────────────────────
    chief_policy = build_chief_policy(bc, pool.registry)
    chief = ChiefAgent.builder(bc).at(CHIEF_URL).policy(chief_policy).build()

    # ── Ombudsman ──────────────────────────────────────────────────────────────
    wd = pool.ombudsman()

    # ── Completion predicate ───────────────────────────────────────────────────
    _TERMINAL = frozenset({"shipped", "validation_failed", "HUMAN_REVIEW", "abandoned"})

    def _is_done(state: dict) -> bool:
        return (
            sum(1 for t in state["tasks"] if t["status"] == "shipped") >= target_shipped
        )

    # ── Per-cycle log ──────────────────────────────────────────────────────────
    def _log_cycle(state: dict, cycle: int) -> None:
        tasks = state["tasks"]
        shipped = sum(1 for t in tasks if t["status"] == "shipped")
        active = sum(1 for t in tasks if t["status"] not in _TERMINAL)
        failed = sum(
            1 for t in tasks if t["status"] in {"HUMAN_REVIEW", "validation_failed"}
        )
        stats = producer.stats
        step_info = (
            f"  choreo_step={stats['choreo_step']}"
            f"  remaining={stats.get('step_remaining_s', 0):.0f}s"
            if choreography_name
            else ""
        )
        logger.info(
            "[cycle %3d]  shipped=%d/%d  active=%d  failed=%d"
            "  producer=[profile=%s orders=%d%s]",
            cycle,
            shipped,
            target_shipped,
            active,
            failed,
            stats["profile"],
            stats["orders"],
            step_info,
        )

    # ── Producer ───────────────────────────────────────────────────────────────
    if choreography_name:
        choreo = CHOREOGRAPHIES[choreography_name]
        producer = OrderProducer(bc, choreography=choreo)
    else:
        producer = OrderProducer(bc, profile=profile)

    # ── Run ────────────────────────────────────────────────────────────────────
    mode = (
        f"choreography={choreography_name!r}"
        if choreography_name
        else f"profile={profile!r}"
    )
    logger.info(
        "Ordering system started — target=%d  %s  warehouse=%d SKUs / %d units",
        target_shipped,
        mode,
        len(INITIAL_WAREHOUSE),
        sum(INITIAL_WAREHOUSE.values()),
    )

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
    tasks = final_state["tasks"]
    shipped_count = sum(1 for t in tasks if t["status"] == "shipped")

    print(f"\n{'═' * 60}")
    print(f"  Ordering system complete")
    print(f"  Shipped: {shipped_count}/{target_shipped}")
    print(f"  Mode: {mode}")
    print(f"  Orders emitted: {producer.stats['orders']}")
    print(f"{'═' * 60}")

    print("\nFinal task states:")
    for t in tasks:
        print(f"  [{t['status']:>20}]  {t['label'][:60]}")

    print("\nFinal warehouse:")
    wh = final_state["data"].get("warehouse", {})
    for sku, qty in sorted(wh.items()):
        initial = INITIAL_WAREHOUSE.get(sku, 0)
        delta = qty - initial
        marker = f" ({'+' if delta >= 0 else ''}{delta})" if delta else ""
        print(f"  {sku:20s}  {qty:4d}{marker}")

    if shipped_count < target_shipped:
        sys.exit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="LLM Ordering System",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--target",
        type=int,
        default=10,
        help="Number of orders to ship before stopping (default 10)",
    )
    parser.add_argument(
        "--cycles",
        type=int,
        default=1000,
        help="Maximum run loop cycles",
    )

    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--profile",
        default="steady",
        choices=["burst", "steady", "slow", "wave", "drought", "idle"],
        help="Single emission profile (default: steady)",
    )
    mode.add_argument(
        "--choreography",
        choices=list(CHOREOGRAPHIES.keys()),
        help="Named choreography — cycles through profiles automatically",
    )
    parser.add_argument(
        "--lifecycle",
        type=str,
        default=None,
        help="Path to a .lifecycle.toml file (overrides built-in lifecycle)",
    )

    args = parser.parse_args()

    lifecycle_override = None
    if args.lifecycle:
        from quadro.board.lifecycle_loader import load_lifecycle

        _name, lifecycle_override = load_lifecycle(args.lifecycle)

    main(
        target_shipped=args.target,
        max_cycles=args.cycles,
        profile=args.profile,
        choreography_name=args.choreography,
        lifecycle=lifecycle_override,
    )
