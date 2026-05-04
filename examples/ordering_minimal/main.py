"""
Ordering system — Quadro saga reference example.

Demonstrates the saga DSL's compensation rollback machinery on a
multi-step business workflow with concrete side effects: a customer
posts an order, a salesperson accepts it, a stock handler reserves
inventory, and a delivery worker ships the package. When a step
fails mid-saga, the runtime walks registered compensations in
reverse order — the shipment is recalled, the inventory is
returned, the acceptance is cancelled — leaving the system in a
consistent state.

This is the framework-neutral (Quadro core) version of the ordering
example. The MAF-backed version at ``examples/ordering/`` shows the
same pattern with LLM-backed workers and a four-stage pipeline.

Run::

    python examples/ordering_minimal/main.py
    python examples/ordering_minimal/main.py --orders 4
    python examples/ordering_minimal/main.py --orders 4 --inject-failure reserve_inventory

When ``--inject-failure <step_name>`` is provided, the named saga
step raises a synthetic exception on its first invocation, triggering
compensation rollback. The remaining orders complete normally. The
final warehouse inventory reflects that the failed order's partial
side effects were undone — no stock was leaked, no acceptance left
dangling.

Saga shape::

    order_saga:
        accept_order         .compensate(undo=cancel_acceptance)
        reserve_inventory    .compensate(undo=release_inventory)
        ship_package         .compensate(undo=recall_shipment)

Each compensation is idempotent — the runtime may re-invoke it on
resume after a worker crash mid-rollback, so the undo bodies check a
board-level marker before re-applying their side effects.

A note on the final task states. The producer's ``order_queue``
carries six orders by design; ``--orders N`` is a **goal
threshold**, not an order count. The run terminates as soon as N
orders reach ``delivered``, so the last one or two orders may show
``[placed]``, ``[accepted]``, or ``[stock_ready]`` in the final
summary instead of ``[delivered]``. That is working as designed —
the goal sponsor fires stop as soon as the target is met, and
in-flight orders are left where they are rather than forced to
completion. To see every order through to ``delivered``, pass
``--orders 6`` (or any value ≥ the queue length).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from quadro import (                                                            # noqa: E402
    LifecycleBuilder,
    Pipeline,
    QuadroBoard,
)
from quadro.a2a.dispatch import LocalA2ANetwork                                 # noqa: E402
from quadro.board.backends.sqlite import SqliteBoardBackend                     # noqa: E402
from quadro.runner import RunLoop                                               # noqa: E402
from quadro.runtime_plugins.saga import QuadroSagaRuntime                       # noqa: E402
from quadro.saga import BuiltSaga, Saga, SagaContext                            # noqa: E402
from quadro.sponsor import AllOf, GoalSponsor, TickBudgetSponsor                # noqa: E402


# ─── Order lifecycle profile ──────────────────────────────────────────────────
#
# Saga-friendly shape — each phase is a direct transition the saga's
# deterministic steps take the task through. The milestone-D brief
# preserves ``ordering_system.lifecycle.toml`` unchanged as a legacy
# artefact, but the default profile below matches the milestone-C
# newsroom pattern: direct transitions, branches for cancellation
# (the compensation target) and awaiting_stock (unused today but kept
# for optionality).

ORDER_PROFILE = (
    LifecycleBuilder()
    .phase("UNASSIGNED", "placed")
    .phase("placed", "accepted")
    .phase("accepted", "stock_ready")
    .phase("stock_ready", "delivered")
    .branch("placed", "cancelled")
    .branch("accepted", "cancelled")
    .branch("stock_ready", "cancelled")
    .branch("accepted", "awaiting_stock")
    .loop("awaiting_stock", "accepted")
    .build()
)


# ─── Saga step bodies ─────────────────────────────────────────────────────────
#
# Every side-effecting step returns a dict describing what it did, so
# its compensation can read the record from ``ctx.step[<step_name>]``
# and undo precisely. Each compensation is idempotent via a
# ``_comp_marker:{task_id}:{step}`` board key — on resume after a
# mid-rollback worker crash, a re-attempted compensation reads the
# marker and returns early if the undo already completed.


def _accept_order(ctx: SagaContext) -> dict[str, Any]:
    """Transition the task from ``placed`` to ``accepted``."""
    task = ctx.task
    board_fn = task["_board_fn"]
    board_fn("board.update_task", {
        "task_id": task["task_id"],
        "to_status": "accepted",
    })
    return {
        "accepted_at": (ctx.now.isoformat() if ctx.now else None),
        "task_id": task["task_id"],
    }


def _cancel_acceptance(ctx: SagaContext) -> None:
    """Compensation for ``_accept_order``: revert to ``cancelled``.

    Idempotent: if the task is already in a terminal state we skip
    (the compensation-marker check protects against re-entry on
    resume, too).
    """
    task = ctx.task
    board_fn = task["_board_fn"]

    marker_key = f"_comp_marker:{task['task_id']}:accept_order"
    marker = (board_fn("board.get_data", {"key": marker_key}) or {}).get("value")
    if marker == "cancelled":
        return  # already applied on a previous attempt

    current = board_fn("board.get_task", {"task_id": task["task_id"]})["task"]
    current_status = current.get("status")
    if current_status in ("cancelled", "delivered"):
        # Already terminal — no transition to make, just mark the compensation done.
        board_fn("board.put_data", {"key": marker_key, "value": "cancelled"})
        return

    board_fn("board.update_task", {
        "task_id": task["task_id"],
        "to_status": "cancelled",
        "notes_append": "Acceptance cancelled (compensation rollback)",
    })
    board_fn("board.put_data", {"key": marker_key, "value": "cancelled"})


def _reserve_inventory(ctx: SagaContext) -> dict[str, Any]:
    """Debit ``WH-MAIN`` for the ordered SKU and transition the task
    from ``accepted`` to ``stock_ready``.

    Parses the order payload from the task's first note (the producer
    writes it as JSON). If the warehouse doesn't carry enough stock
    for the SKU, raises ``RuntimeError`` so the saga fails and its
    compensation walker runs.
    """
    task = ctx.task
    board_fn = task["_board_fn"]
    order = _parse_order(task)
    sku, qty = order["sku"], int(order["quantity"])

    wh = (board_fn("board.get_data", {"key": "WH-MAIN"}) or {}).get("value") or {}
    available = int(wh.get(sku, 0))
    if available < qty:
        raise RuntimeError(
            f"insufficient stock for {sku}: available={available}, requested={qty}"
        )

    wh[sku] = available - qty
    board_fn("board.put_data", {"key": "WH-MAIN", "value": wh})
    board_fn("board.update_task", {
        "task_id": task["task_id"],
        "to_status": "stock_ready",
    })
    return {"sku": sku, "qty": qty, "reserved_from": "WH-MAIN"}


def _release_inventory(ctx: SagaContext) -> None:
    """Compensation for ``_reserve_inventory``: credit the warehouse back.

    Idempotent: reads a marker before acting so a resume doesn't
    double-credit.
    """
    task = ctx.task
    board_fn = task["_board_fn"]
    reservation = ctx.step.get("reserve_inventory") or {}
    if not reservation.get("sku"):
        return  # reserve_inventory never completed; nothing to undo

    marker_key = f"_comp_marker:{task['task_id']}:reserve_inventory"
    marker = (board_fn("board.get_data", {"key": marker_key}) or {}).get("value")
    if marker == "released":
        return

    sku = reservation["sku"]
    qty = int(reservation["qty"])
    wh = (board_fn("board.get_data", {"key": "WH-MAIN"}) or {}).get("value") or {}
    wh[sku] = int(wh.get(sku, 0)) + qty
    board_fn("board.put_data", {"key": "WH-MAIN", "value": wh})
    board_fn("board.put_data", {"key": marker_key, "value": "released"})


def _ship_package(ctx: SagaContext) -> dict[str, Any]:
    """Transition the task from ``stock_ready`` to ``delivered`` and
    record a shipment marker.
    """
    task = ctx.task
    board_fn = task["_board_fn"]
    reservation = ctx.step.get("reserve_inventory") or {}

    shipment_key = f"_shipment:{task['task_id']}"
    shipment = {
        "sku": reservation.get("sku", ""),
        "qty": int(reservation.get("qty", 0)),
        "dispatched_at": (ctx.now.isoformat() if ctx.now else None),
    }
    board_fn("board.put_data", {"key": shipment_key, "value": shipment})
    board_fn("board.update_task", {
        "task_id": task["task_id"],
        "to_status": "delivered",
    })
    return {"dispatched_at": shipment["dispatched_at"]}


def _recall_shipment(ctx: SagaContext) -> None:
    """Compensation for ``_ship_package``: record a recall intent on
    the shipment marker. In a real system you can't physically
    'unship' a package — the compensation is the operator's recall /
    return-to-warehouse instruction, persisted on the board for audit.
    """
    task = ctx.task
    board_fn = task["_board_fn"]
    ship = ctx.step.get("ship_package") or {}
    if not ship.get("dispatched_at"):
        return

    marker_key = f"_comp_marker:{task['task_id']}:ship_package"
    marker = (board_fn("board.get_data", {"key": marker_key}) or {}).get("value")
    if marker == "recalled":
        return

    shipment_key = f"_shipment:{task['task_id']}"
    existing = (board_fn("board.get_data", {"key": shipment_key}) or {}).get("value") or {}
    existing["recalled_at"] = (ctx.now.isoformat() if ctx.now else None)
    existing["recall_reason"] = "compensation rollback"
    board_fn("board.put_data", {"key": shipment_key, "value": existing})
    board_fn("board.put_data", {"key": marker_key, "value": "recalled"})


def _parse_order(task: dict[str, Any]) -> dict[str, Any]:
    notes = task.get("notes") or []
    if not notes:
        return {"sku": "", "quantity": 0}
    try:
        return json.loads(notes[0]) if isinstance(notes[0], str) else notes[0]
    except Exception:
        return {"sku": "", "quantity": 0}


# ─── Failure injection ────────────────────────────────────────────────────────


def _maybe_inject_failure(
    step_name: str,
    fn,
    inject_target: str | None,
):
    """Wrap ``fn`` so its first invocation raises iff ``inject_target``
    equals ``step_name``. Subsequent calls fall through to ``fn``.

    Used by ``build_order_saga`` to thread the CLI's
    ``--inject-failure`` flag into the saga construction without a
    module-level global.
    """
    if inject_target != step_name:
        return fn

    seen = {"count": 0}

    def _wrapped(ctx):
        seen["count"] += 1
        if seen["count"] == 1:
            raise RuntimeError(
                f"--inject-failure {step_name}: synthetic failure on first "
                f"invocation (compensation rollback will be exercised)"
            )
        return fn(ctx)

    return _wrapped


# ─── Saga construction ────────────────────────────────────────────────────────


def build_order_saga(inject_failure: str | None = None) -> BuiltSaga:
    """Return a freshly-built ``BuiltSaga``, optionally with one of
    its steps wrapped in a synthetic-failure injector.

    Called at pipeline construction time (once per task batch in the
    integration test; once per main() call in the CLI) so the
    ``--inject-failure`` flag can be threaded in without a mutable
    module-level global. The sagas are frozen after ``.build()``, so
    rebuilding per invocation is cheap and isolates the failure
    injection to a single task's run.
    """
    return (
        Saga("order")
        .deterministic(
            "accept_order",
            _maybe_inject_failure("accept_order", _accept_order, inject_failure),
        )
        .compensate("accept_order", undo=_cancel_acceptance)
        .deterministic(
            "reserve_inventory",
            _maybe_inject_failure(
                "reserve_inventory", _reserve_inventory, inject_failure
            ),
        )
        .compensate("reserve_inventory", undo=_release_inventory)
        .deterministic(
            "ship_package",
            _maybe_inject_failure("ship_package", _ship_package, inject_failure),
        )
        .compensate("ship_package", undo=_recall_shipment)
        .build()
    )


# ─── Main ─────────────────────────────────────────────────────────────────────


_INJECT_FAILURE_CHOICES = ["accept_order", "reserve_inventory", "ship_package"]


def main() -> None:
    parser = argparse.ArgumentParser(description="Quadro ordering system example")
    parser.add_argument(
        "--orders", type=int, default=4, help="Stop after N orders delivered"
    )
    parser.add_argument("--cycles", type=int, default=60, help="Maximum run cycles")
    parser.add_argument(
        "--lifecycle", type=str, default=None, help="Path to a .lifecycle.toml file"
    )
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

    if args.lifecycle:
        from quadro.board.lifecycle_loader import load_lifecycle
        _name, profile = load_lifecycle(args.lifecycle)
    else:
        profile = ORDER_PROFILE

    # Board
    network = LocalA2ANetwork()
    board = QuadroBoard(
        SqliteBoardBackend(),
        profile_resolver={"order": "order"},
        custom_profiles={"order": profile},
        network=network,
    )
    bc = board.client()

    # Warehouse seed — generous enough to fulfil the default 4-order run
    # plus the re-run when a task is compensated (stock returned).
    bc.put_data("WH-MAIN", {"SKU-A": 10, "SKU-B": 10, "SKU-C": 10})
    bc.put_data("WH-RESERVE", {"SKU-A": 0, "SKU-B": 0, "SKU-C": 0})

    # Pipeline — saga stage driven by the QuadroSagaRuntime.
    order_saga = build_order_saga(inject_failure=args.inject_failure)
    saga_runtime = QuadroSagaRuntime()

    built = (
        Pipeline(board)
        .workers(1)
        .wakes("a2a://chief")
        .with_framework_runtime(saga_runtime)
        .stage(
            "order",
            saga=order_saga,
            active_status="placed",
            max_working_time=10.0,
        )
        .build()
    )

    # Order sequence posted progressively by the on_cycle callback below.
    order_queue = [
        ("SKU-A", 2),
        ("SKU-B", 1),
        ("SKU-C", 2),
        ("SKU-A", 3),
        ("SKU-B", 2),
        ("SKU-A", 1),
    ]
    order_num = 0

    print(
        f"Ordering system started. Target: {args.orders} delivered orders."
        + (f"  inject_failure={args.inject_failure!r}" if args.inject_failure else "")
        + "\n"
    )

    def _is_done(state: dict) -> bool:
        return (
            sum(1 for t in state["tasks"] if t["status"] == "delivered") >= args.orders
        )

    def _log_cycle(state: dict, cycle: int) -> None:
        nonlocal order_num
        if order_num < len(order_queue) and order_num < args.orders + 2:
            sku, qty = order_queue[order_num]
            bc.post_task(
                "order",
                f"Order #{order_num + 1}: {sku} x{qty}",
                notes=[json.dumps({"sku": sku, "quantity": qty})],
            )
            # Task is left in ``UNASSIGNED`` — the pipeline's chief
            # policy moves it to the first active_status ("placed")
            # on its next turn, which also assigns a worker and fires
            # the saga. Pre-transitioning here would short-circuit
            # that dispatch because the chief's ``dispatch_batch(
            # UNASSIGNED → placed)`` would find zero eligible tasks.
            order_num += 1

        tasks = state["tasks"]
        delivered = sum(1 for t in tasks if t["status"] == "delivered")
        cancelled = sum(1 for t in tasks if t["status"] == "cancelled")
        pending = sum(
            1 for t in tasks if t["status"] not in ("delivered", "cancelled")
        )
        wh = state["data"].get("WH-MAIN", {})
        print(
            f"[cycle {cycle:3d}]  delivered={delivered}  "
            f"cancelled={cancelled}  pending={pending}  WH-MAIN={wh}"
        )

    final_state = (
        RunLoop(board, built.chief)
        .sponsor(AllOf(GoalSponsor(_is_done), TickBudgetSponsor(args.cycles)))
        .on_cycle(_log_cycle)
        .poll_every(0.0)
        .ombudsman_every(0.0)
        .run()
    )

    # ── Final summary ─────────────────────────────────────────────────────────
    print("\n── Final task states ──────────────────────────────────────────")
    for task in final_state["tasks"]:
        print(f"  [{task['status']:>12}]  {task['label']}")

    print("\n── Final warehouse ────────────────────────────────────────────")
    print(f"  WH-MAIN:    {final_state['data'].get('WH-MAIN', {})}")
    print(f"  WH-RESERVE: {final_state['data'].get('WH-RESERVE', {})}")

    if args.inject_failure:
        # Surface the compensation log for any task whose saga rolled back.
        print("\n── Compensation rollback summary ──────────────────────────────")
        for task in final_state["tasks"]:
            state_key = f"_saga:{task['task_id']}"
            saga_state = final_state["data"].get(state_key)
            if not isinstance(saga_state, dict):
                continue
            comp_log = saga_state.get("compensations_run") or []
            if not comp_log:
                continue
            print(f"  Task {task['task_id'][:8]}  ({task['status']})")
            for entry in comp_log:
                outcome = entry.get("outcome", "?")
                step = entry.get("step", "?")
                ms = entry.get("duration_ms", 0)
                print(f"    - {step:<20}  outcome={outcome}  {ms}ms")


if __name__ == "__main__":
    main()
