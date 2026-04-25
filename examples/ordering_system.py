"""
Ordering system — Quadro reference example.

Five participants coordinate through the Board:

    Customer     -- posts new order tasks to the board every cycle
    Salesperson  -- accepts each placed order
    StockHandler -- checks warehouse inventory; fulfils or queues the order
    Delivery     -- ships stock-ready orders
    Chief        -- routes tasks between workers based on lifecycle state

Order lifecycle (custom profile):
    placed → accepted → awaiting_stock → stock_ready → delivering → delivered
    (→ cancelled from any state)

Warehouse state (board data, not tasks):
    WH-MAIN    -- main warehouse inventory  {SKU: quantity}
    WH-RESERVE -- reserve inventory         {SKU: quantity}

Run:
    python examples/ordering_system.py
    python examples/ordering_system.py --orders 5   # stop after N delivered
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from quadro import (
    BoardClient,
    ChiefAgent,
    LocalA2ANetwork,
    QuadroBoard,
    RunLoop,
    WorkerAgent,
)
from quadro.a2a.contracts import A2ARequest
from quadro.board.backends.sqlite import SqliteBoardBackend
from quadro.board.state_machine import LifecycleBuilder
from quadro.sponsor import AllOf, GoalSponsor, TickBudgetSponsor

# ─── Order lifecycle profile ──────────────────────────────────────────────────

ORDER_PROFILE = (
    LifecycleBuilder()
    .step("UNASSIGNED", "placed")
    .step("placed", "IN_PROGRESS")
    .step("IN_PROGRESS", "accepted")
    .branch("IN_PROGRESS", "cancelled")
    .step("accepted", "IN_PROGRESS")
    .step("IN_PROGRESS", "stock_ready")
    .branch("IN_PROGRESS", "awaiting_stock")
    .loop("awaiting_stock", "accepted")
    .step("stock_ready", "delivering")
    .step("delivering", "delivered")
    .build()
)

# ─── Chief policy ─────────────────────────────────────────────────────────────


def make_order_policy(board_client: BoardClient, network: LocalA2ANetwork):
    """Returns a chief policy that routes order tasks through their lifecycle."""

    def _idle(agents: list[dict], cap: str) -> dict | None:
        return next(
            (a for a in agents if cap in a["capabilities"] and a["status"] == "IDLE"),
            None,
        )

    def policy(ctx: dict) -> None:
        tasks = ctx["payload"]["tasks"]
        agents = ctx["payload"]["agents"]

        for task in tasks:
            s, tid = task["status"], task["task_id"]

            if s == "placed":
                w = _idle(agents, "sales")
                if w:
                    board_client.update_task(
                        tid, "IN_PROGRESS", assigned_to=w["agent_id"]
                    )
                    network.request(
                        w["a2a_url"],
                        A2ARequest(
                            intent="worker.execute_task", payload={"task_id": tid}
                        ).to_dict(),
                    )

            elif s == "accepted":
                w = _idle(agents, "stock")
                if w:
                    board_client.update_task(
                        tid, "IN_PROGRESS", assigned_to=w["agent_id"]
                    )
                    network.request(
                        w["a2a_url"],
                        A2ARequest(
                            intent="worker.execute_task", payload={"task_id": tid}
                        ).to_dict(),
                    )

            elif s == "stock_ready":
                w = _idle(agents, "delivery")
                if w:
                    board_client.update_task(
                        tid, "delivering", assigned_to=w["agent_id"]
                    )
                    network.request(
                        w["a2a_url"],
                        A2ARequest(
                            intent="worker.execute_task", payload={"task_id": tid}
                        ).to_dict(),
                    )

            # awaiting_stock → do nothing; stock_handler re-queues after replenishment

    return policy


# ─── Worker execute functions ─────────────────────────────────────────────────


def salesperson_fn(ctx: dict, board) -> str:
    """Accepts every order (deterministic)."""
    task = ctx["payload"]["task"]
    board("board.update_task", {"task_id": task["task_id"], "to_status": "accepted"})
    return "accepted"


def stock_handler_fn(ctx: dict, board) -> str:
    """Checks inventory; fulfils or queues; replenishes from reserve when needed."""
    task = ctx["payload"]["task"]
    order = json.loads(task["notes"][0])
    sku, qty = order["sku"], order["quantity"]

    wh = board("board.get_data", {"key": "WH-MAIN"})["value"] or {}

    if wh.get(sku, 0) >= qty:
        wh[sku] -= qty
        board("board.put_data", {"key": "WH-MAIN", "value": wh})
        board(
            "board.update_task",
            {"task_id": task["task_id"], "to_status": "stock_ready"},
        )
        return "fulfilled"

    # Insufficient main stock — try to replenish from reserve
    reserve = board("board.get_data", {"key": "WH-RESERVE"})["value"] or {}
    if reserve.get(sku, 0) > 0:
        replenish = min(reserve[sku], 20)
        wh[sku] = wh.get(sku, 0) + replenish
        reserve[sku] -= replenish
        board("board.put_data", {"key": "WH-MAIN", "value": wh})
        board("board.put_data", {"key": "WH-RESERVE", "value": reserve})
        # Park current task then re-queue it alongside any other waiting orders
        board(
            "board.update_task",
            {"task_id": task["task_id"], "to_status": "awaiting_stock"},
        )
        full = board("board.get_full_state", {})
        for t in full.get("tasks", []):
            if t["status"] == "awaiting_stock":
                board(
                    "board.update_task",
                    {"task_id": t["task_id"], "to_status": "accepted"},
                )
        return "queued (replenished)"

    board(
        "board.update_task", {"task_id": task["task_id"], "to_status": "awaiting_stock"}
    )
    return "queued (no stock)"


def delivery_fn(ctx: dict, board) -> str:
    """Marks the order as delivered."""
    task = ctx["payload"]["task"]
    board("board.update_task", {"task_id": task["task_id"], "to_status": "delivered"})
    return "delivered"


# ─── Main ─────────────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(description="Quadro ordering system example")
    parser.add_argument(
        "--orders", type=int, default=4, help="Stop after N orders delivered"
    )
    parser.add_argument("--cycles", type=int, default=60, help="Maximum run cycles")
    parser.add_argument(
        "--lifecycle", type=str, default=None, help="Path to a .lifecycle.toml file"
    )
    args = parser.parse_args()

    # Lifecycle — from TOML file or Python builder
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

    # Warehouse initial state
    bc.put_data("WH-MAIN", {"SKU-A": 5, "SKU-B": 3, "SKU-C": 2})
    bc.put_data("WH-RESERVE", {"SKU-A": 20, "SKU-B": 15, "SKU-C": 10})

    # Workers
    salesperson = (
        WorkerAgent.builder("salesperson_1", bc)
        .name("Salesperson")
        .capability("sales")
        .at("a2a://workers/salesperson_1")
        .execute(salesperson_fn)
        .build()
    )
    stock_handler = (
        WorkerAgent.builder("stock_handler_1", bc)
        .name("StockHandler")
        .capability("stock")
        .at("a2a://workers/stock_handler_1")
        .execute(stock_handler_fn)
        .build()
    )
    delivery = (
        WorkerAgent.builder("delivery_1", bc)
        .name("Delivery")
        .capability("delivery")
        .at("a2a://workers/delivery_1")
        .execute(delivery_fn)
        .build()
    )
    for w in (salesperson, stock_handler, delivery):
        w.register()

    # Chief
    chief = ChiefAgent.builder(bc).policy(make_order_policy(bc, network)).build()

    # Order sequence posted by the customer
    order_queue = [
        ("SKU-A", 2),
        ("SKU-B", 1),
        ("SKU-C", 2),
        ("SKU-A", 3),
        ("SKU-B", 2),
        ("SKU-A", 1),
    ]
    order_num = 0

    print(f"Ordering system started. Target: {args.orders} delivered orders.\n")

    def _is_done(state: dict) -> bool:
        return (
            sum(1 for t in state["tasks"] if t["status"] == "delivered") >= args.orders
        )

    def _log_cycle(state: dict, cycle: int) -> None:
        nonlocal order_num
        if order_num < len(order_queue):
            sku, qty = order_queue[order_num]
            task = bc.post_task(
                "order",
                f"Order #{order_num + 1}: {sku} x{qty}",
                notes=[json.dumps({"sku": sku, "quantity": qty})],
            )
            bc.update_task(task["task_id"], "placed")
            order_num += 1

        tasks = state["tasks"]
        delivered = sum(1 for t in tasks if t["status"] == "delivered")
        pending = sum(1 for t in tasks if t["status"] not in ("delivered", "cancelled"))
        wh = state["data"].get("WH-MAIN", {})
        print(
            f"[cycle {cycle:3d}]  delivered={delivered}  pending={pending}  WH-MAIN={wh}"
        )

    final_state = (
        RunLoop(board, chief)
        .sponsor(AllOf(GoalSponsor(_is_done), TickBudgetSponsor(args.cycles)))
        .on_cycle(_log_cycle)
        .poll_every(0.0)
        .ombudsman_every(0.0)
        .run()
    )
    print("\n── Final task states ──────────────────────────────────────────")
    for task in final_state["tasks"]:
        print(f"  [{task['status']:>16}]  {task['label']}")

    print("\n── Final warehouse ────────────────────────────────────────────")
    print(f"  WH-MAIN:    {final_state['data'].get('WH-MAIN', {})}")
    print(f"  WH-RESERVE: {final_state['data'].get('WH-RESERVE', {})}")

    events = bc.stream_events()
    print(f"\n── Event log ({len(events)} events) ────────────────────────────────")
    for ev in events:
        print(
            f"  #{ev['sequence_id']:3d}  {ev['event_type']:16s}"
            f"  {str(ev['from_status']):>16} → {ev['to_status']}"
            f"  task={ev['task_id'][:8]}"
        )


if __name__ == "__main__":
    main()
