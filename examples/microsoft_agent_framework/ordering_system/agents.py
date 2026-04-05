"""
LLM agent execute_fn implementations for the ordering system pipeline.

Each function is an async coroutine that accepts the standard Quadro worker
signature:  (context: dict, board_fn: Callable[[str, dict], dict]) -> str

They are passed directly as execute_fn to WorkerAgent.  Quadro's async
execute_fn support runs them via asyncio.run() in a thread pool.
"""

from __future__ import annotations

import json
import logging
import os
import random
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

from agent_framework.openai import OpenAIChatClient

from shared import (
    clean_llm_output,
    create_llm_client,
    dispatch_batch,
    find_idle_worker,
    fire_worker,
    load_prompt,
    run_chief_workflow,
    run_single_agent,
)

logger = logging.getLogger(__name__)

PROMPTS_DIR = Path(__file__).parent / "prompts"


# ── LLM helpers ────────────────────────────────────────────────────────────────


def _client_local() -> OpenAIChatClient:
    return create_llm_client()


def _client_openai() -> OpenAIChatClient:
    return create_llm_client(
        api_key=os.environ.get("OPENAI_API_KEY", ""),
        base_url="https://api.openai.com/v1/",
        model_id=os.environ.get("OPENAI_MODEL_ID", "gpt-4.1"),
    )


def _client() -> OpenAIChatClient:
    """Pick a client: 70% chance OpenAI, 30% local sglang."""
    if random.random() > 0.3:
        return _client_openai()
    return _client_local()

    # return _client_openai()


def _prompt(name: str) -> str:
    return load_prompt(PROMPTS_DIR, name)


# ── Worker execute_fn functions ─────────────────────────────────────────────────


async def run_validation(context: dict, board_fn: Callable[[str, dict], dict]) -> str:
    """Validation worker: check order details against catalog → validated or validation_failed."""
    from data import PRODUCT_CATALOG
    from schemas import OrderValidation

    task = context["payload"]["task"]
    order_raw = task.get("notes", ["{}"])[0]

    try:
        order = json.loads(order_raw)
    except (json.JSONDecodeError, IndexError):
        order = {}

    sku = order.get("sku", "")
    catalog_entry = PRODUCT_CATALOG.get(sku)

    validation_input = json.dumps(
        {
            "customer_name": order.get("customer_name", ""),
            "sku": sku,
            "quantity": order.get("quantity", 0),
            "address": order.get("address", ""),
            "catalog_entry": catalog_entry,
        }
    )

    raw = await run_single_agent(
        instructions=_prompt("validation"),
        user_message=validation_input,
        client_factory=_client,
        default_options={"response_format": {"type": "json_object"}},
        executor_prefix="validation",
    )

    is_valid = False
    reason = ""
    try:
        result = OrderValidation.model_validate_json(raw)
        is_valid = result.valid
        reason = result.rejection_reason
    except Exception:
        is_valid = False
        reason = "Invalid JSON"

    if is_valid:
        board_fn(
            "board.update_task",
            {
                "task_id": task["task_id"],
                "to_status": "validated",
                "output": order_raw,
            },
        )
    else:
        board_fn(
            "board.update_task",
            {
                "task_id": task["task_id"],
                "to_status": "validation_failed",
                "output": order_raw,
                "notes_append": f"Validation failed: {reason}",
            },
        )

    return order_raw


async def run_inventory(context: dict, board_fn: Callable[[str, dict], dict]) -> str:
    """Inventory scout: check warehouse stock → stock_confirmed or needs_procurement."""
    from schemas import InventoryCheck

    task = context["payload"]["task"]
    validation_raw = task.get("output", "{}")

    try:
        validation = json.loads(validation_raw)
    except Exception:
        validation = {}

    sku = validation.get("sku", "")
    quantity = validation.get("quantity", 0)
    product_name = validation.get("sku", sku)

    state = board_fn("board.get_full_state", {})
    warehouse = state.get("data", {}).get("warehouse", {})
    catalog = state.get("data", {}).get("product_catalog", {})
    stock = warehouse.get(sku, 0)

    if sku in catalog:
        product_name = catalog[sku].get("name", sku)

    inventory_input = json.dumps(
        {
            "sku": sku,
            "quantity": quantity,
            "warehouse_stock": stock,
            "product_name": product_name,
        }
    )

    raw = await run_single_agent(
        instructions=_prompt("inventory"),
        user_message=inventory_input,
        client_factory=_client,
        default_options={"response_format": {"type": "json_object"}},
        executor_prefix="inventory",
    )

    try:
        result = InventoryCheck.model_validate_json(raw)
        output_json = result.model_dump_json()
    except Exception:
        output_json = raw

    try:
        parsed = json.loads(output_json)
        sufficient = parsed.get("sufficient", False)
    except Exception:
        sufficient = False

    if sufficient:
        warehouse[sku] = max(0, warehouse.get(sku, 0) - quantity)
        board_fn("board.put_data", {"key": "warehouse", "value": warehouse})

        board_fn(
            "board.update_task",
            {
                "task_id": task["task_id"],
                "to_status": "stock_confirmed",
                "output": output_json,
                "notes_append": f"Stock confirmed: {stock} available, {quantity} reserved",
            },
        )
    else:
        board_fn(
            "board.update_task",
            {
                "task_id": task["task_id"],
                "to_status": "needs_procurement",
                "output": output_json,
                "notes_append": f"Insufficient stock: {stock} available, {quantity} needed",
            },
        )

    return output_json


async def run_procurement(context: dict, board_fn: Callable[[str, dict], dict]) -> str:
    """Procurement negotiator: evaluate suppliers, purchase stock → procured."""
    from data import SUPPLIERS
    from schemas import ProcurementResult

    task = context["payload"]["task"]
    inventory_raw = task.get("output", "{}")

    try:
        inventory = json.loads(inventory_raw)
    except Exception:
        inventory = {}

    # Get order details from the validation output stored in notes
    validation_data = {}
    for note in task.get("notes", []):
        if note.startswith("{"):
            try:
                validation_data = json.loads(note)
                if "sku" in validation_data and "quantity" in validation_data:
                    break
            except Exception:
                continue

    # Fall back to inventory check data
    sku = validation_data.get("sku", "") or inventory.get("sku", "")
    shortfall = inventory.get("shortfall", 0)
    units_needed = max(shortfall, inventory.get("requested_qty", 0))

    state = board_fn("board.get_full_state", {})
    catalog = state.get("data", {}).get("product_catalog", {})
    product = catalog.get(sku, {})
    catalog_price = product.get("price", 100.0)
    product_name = product.get("name", sku)

    supplier_offers = []
    for s in SUPPLIERS:
        unit_cost = round(catalog_price * s["price_multiplier"], 2)
        order_qty = max(units_needed, s["min_order"])
        supplier_offers.append(
            {
                "name": s["name"],
                "unit_cost": unit_cost,
                "lead_time_days": s["lead_time_days"],
                "reliability": s["reliability"],
                "min_order": s["min_order"],
                "total_cost": round(unit_cost * order_qty, 2),
            }
        )

    procurement_input = json.dumps(
        {
            "sku": sku,
            "product_name": product_name,
            "catalog_price": catalog_price,
            "units_needed": units_needed,
            "suppliers": supplier_offers,
        }
    )

    raw = await run_single_agent(
        instructions=_prompt("procurement"),
        user_message=procurement_input,
        client_factory=_client,
        default_options={"response_format": {"type": "json_object"}},
        executor_prefix="procurement",
    )

    try:
        result = ProcurementResult.model_validate_json(raw)
        output_json = result.model_dump_json()
    except Exception:
        output_json = raw

    # Update warehouse stock with procured units
    try:
        parsed = json.loads(output_json)
        units_ordered = parsed.get("units_ordered", units_needed)
    except Exception:
        units_ordered = units_needed

    warehouse = state.get("data", {}).get("warehouse", {})
    warehouse[sku] = warehouse.get(sku, 0) + units_ordered
    board_fn("board.put_data", {"key": "warehouse", "value": warehouse})

    board_fn(
        "board.update_task",
        {
            "task_id": task["task_id"],
            "to_status": "procured",
            "output": output_json,
            "notes_append": f"Procured {units_ordered} units of {sku}",
        },
    )

    return output_json


async def run_logistics(context: dict, board_fn: Callable[[str, dict], dict]) -> str:
    """Logistics coordinator: select carrier, generate tracking → shipped."""
    from data import CARRIERS
    from schemas import ShippingLabel

    task = context["payload"]["task"]

    # Reconstruct order details from validation output in notes
    validation_data = {}
    for note in task.get("notes", []):
        if note.startswith("{"):
            try:
                candidate = json.loads(note)
                if "customer_name" in candidate or "customer" in candidate:
                    validation_data = candidate
                    break
            except Exception:
                continue

    # Also try the original order data
    order_data = {}
    if task.get("notes"):
        try:
            order_data = json.loads(task["notes"][0])
        except Exception:
            pass

    customer_name = (
        validation_data.get("customer_name", "")
        or order_data.get("customer", "")
        or "Customer"
    )
    delivery_address = (
        validation_data.get("delivery_address", "")
        or order_data.get("address", "")
        or "Address on file"
    )
    sku = validation_data.get("sku", "") or order_data.get("sku", "")
    quantity = validation_data.get("quantity", 0) or order_data.get("quantity", 1)
    unit_price = validation_data.get("unit_price", 0)
    order_total = validation_data.get("total", unit_price * quantity)

    state = board_fn("board.get_full_state", {})
    catalog = state.get("data", {}).get("product_catalog", {})
    product = catalog.get(sku, {})
    product_name = product.get("name", sku)

    if not order_total and product:
        order_total = product.get("price", 0) * quantity

    now = datetime.now(timezone.utc)
    carrier_options = []
    for c in CARRIERS:
        est_cost = round(c["base_cost"] + (quantity * 1.50), 2)
        est_delivery = (now + timedelta(days=c["speed_days"])).strftime("%Y-%m-%d")
        carrier_options.append(
            {
                "name": c["name"],
                "base_cost": c["base_cost"],
                "speed_days": c["speed_days"],
                "estimated_cost": est_cost,
                "estimated_delivery": est_delivery,
            }
        )

    logistics_input = json.dumps(
        {
            "sku": sku,
            "product_name": product_name,
            "quantity": quantity,
            "customer_name": customer_name,
            "delivery_address": delivery_address,
            "order_total": order_total,
            "carriers": carrier_options,
        }
    )

    raw = await run_single_agent(
        instructions=_prompt("logistics"),
        user_message=logistics_input,
        client_factory=_client,
        default_options={"response_format": {"type": "json_object"}},
        executor_prefix="logistics",
    )

    try:
        result = ShippingLabel.model_validate_json(raw)
        output_json = result.model_dump_json()
    except Exception:
        output_json = raw

    try:
        carrier_name = json.loads(output_json).get("carrier", "carrier")
    except Exception:
        carrier_name = "carrier"

    board_fn(
        "board.update_task",
        {
            "task_id": task["task_id"],
            "to_status": "shipped",
            "output": output_json,
            "notes_append": f"Shipped via {carrier_name}",
        },
    )

    return output_json


# ── Chief policy ────────────────────────────────────────────────────────────────


def build_chief_policy(
    board_client: "BoardClient",
    worker_registry: dict[str, list[tuple[str, str]]],
) -> Callable:
    """
    Build the async chief policy function that drives the ordering system.

    Args:
        board_client:     BoardClient wrapping the board's A2A endpoint.
        worker_registry:  {capability: [(agent_id, url), ...]}

    Returns an async callable with signature (chief_context: dict) -> None
    passed to ChiefAgent as the policy parameter.
    """
    from tools import create_chief_tools

    network = board_client.network
    board_url = board_client.board_url

    async def chief_policy(chief_context: dict) -> None:
        def board_fn(intent: str, p: dict) -> dict:
            return board_client.request(intent, p)

        state = board_client.full_state()
        data = state.get("data", {})

        for order in data.get("orders_in_queue", []):
            result = board_client.post_task(
                "order",
                f"Order: For customer {order.get('customer_name', 'BLAH')}",
                notes=[json.dumps(order)],
            )
            task_id = result["task_id"]
            w = find_idle_worker(board_fn, worker_registry, "validation")
            if w:
                agent_id, url = w
                board_client.update_task(task_id, "validating", assigned_to=agent_id)
                fire_worker(network, url, task_id)

        dispatch_batch(
            board_fn,
            network,
            worker_registry,
            "UNASSIGNED",
            "validating",
            "validation",
        )

        tools = create_chief_tools(board_fn, network, board_url, worker_registry)

        board_summary = board_client.snapshot(tools, goal_key="order_goal")
        if board_summary is None:
            logger.info("Chief: nothing actionable — sleeping")
            return

        try:
            output = await run_chief_workflow(
                board_summary=board_summary,
                instructions=_prompt("chief"),
                tools=tools,
                client_factory=_client,
                agent_name_prefix="order_ops_manager",
            )
            if output:
                logger.info("Chief: %s", output[:200])
        except Exception as exc:
            logger.error("Chief policy error: %s", exc)

    return chief_policy
