"""
Saga definitions for the MAF ordering system.

All four ordering stages (validation, inventory, procurement, logistics)
are implemented as sagas. Each saga reads top-to-bottom in declaration
order; side-effecting steps register ``.compensate(...)`` directives that
the runtime walks in reverse on failure.

Compensation responsibilities per stage:

- **validation** — no compensation. The stage is a read-only catalog
  lookup plus an LLM decision; nothing that needs undoing.
- **inventory** — ``reserve_units`` debits the warehouse. Its
  compensation (``_release_units``) credits the warehouse back.
- **procurement** — ``procure_units`` credits the warehouse with newly
  purchased stock AND records a supplier order. The compensation
  reverses the credit and records a supplier-cancellation marker.
- **logistics** — ``dispatch_shipment`` records a shipment and transitions
  the task to ``shipped``. The compensation records a recall-shipment
  intent (a real-world side effect that is non-physical in this demo).

Each compensation is idempotent — the runtime guarantees at-least-once
invocation, so a worker crash mid-rollback may re-attempt an already-
completed compensation. The idempotency guards use a board-level marker
key per task (``_comp_marker:{task_id}:{step}``) so a repeat invocation
is a no-op.

``--inject-failure <step>`` in ``main_pipeline.py`` forces the named
step's first invocation to raise. Use ``build_sagas(inject_failure=...)``
to produce the four sagas with failure injection wired in; the
module-level aliases (``validation_saga`` etc.) are built without
injection for static-import callers.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from quadro.saga import BuiltSaga, Saga, SagaContext

load_dotenv()

logger = logging.getLogger(__name__)

PROMPTS_DIR = Path(__file__).parent / "prompts"


# ──────────────────────────────────────────────────────────────────────────────
# Failure injection
# ──────────────────────────────────────────────────────────────────────────────


def _maybe_inject_failure(
    step_name: str,
    fn: Callable[[SagaContext], Any],
    inject_target: str | None,
) -> Callable[[SagaContext], Any]:
    """Wrap ``fn`` so its first invocation raises iff ``inject_target``
    matches ``step_name``. Subsequent calls fall through to ``fn``.

    The injected failure mirrors a real-world transient — the step's
    registered compensation should undo whatever partial work the
    wrapper's failure left behind. On the next dispatch (for the same
    task or a new one), ``seen`` is 2+ and the real body runs.
    """
    if inject_target != step_name:
        return fn

    seen = {"count": 0}

    def _wrapped(ctx: SagaContext) -> Any:
        seen["count"] += 1
        if seen["count"] == 1:
            raise RuntimeError(
                f"--inject-failure {step_name}: synthetic failure on first "
                f"invocation (compensation rollback will be exercised)"
            )
        return fn(ctx)

    return _wrapped


# ──────────────────────────────────────────────────────────────────────────────
# Shared helpers
# ──────────────────────────────────────────────────────────────────────────────


def _parse_json_note(raw: Any) -> dict[str, Any]:
    """Best-effort parse of a JSON-shaped note/output. Returns {} on failure."""
    if raw is None or raw == "":
        return {}
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str):
        return {}
    try:
        parsed = json.loads(raw)
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _order_from_task(task: dict[str, Any]) -> dict[str, Any]:
    """Extract the customer order payload from the task's first note.

    Matches the legacy ``run_validation`` convention: the producer
    writes the order as the first notes entry.
    """
    notes = task.get("notes") or []
    if not notes:
        return {}
    return _parse_json_note(notes[0])


# ──────────────────────────────────────────────────────────────────────────────
# Validation saga
# ──────────────────────────────────────────────────────────────────────────────
#
# Flow: parse the order note → LLM validation → gate on `valid` →
# persist as validated or validation_failed. No compensation — the
# validation stage has no external side effects that need undoing.


def _parse_order_input(ctx: SagaContext) -> dict[str, Any]:
    """Merge the raw order with the catalog entry for the requested SKU."""
    from data import PRODUCT_CATALOG

    order = _order_from_task(ctx.task)
    sku = order.get("sku", "")
    return {
        "customer_name": order.get("customer_name", ""),
        "sku": sku,
        "quantity": order.get("quantity", 0),
        "address": order.get("address", ""),
        "catalog_entry": PRODUCT_CATALOG.get(sku),
        "_order_raw": order,
    }


def _persist_validated(ctx: SagaContext) -> dict[str, Any]:
    """Transition the task to ``validated`` with the LLM's validated output."""
    from schemas import OrderValidation

    task = ctx.task
    board_fn: Callable[[str, dict], dict] = task["_board_fn"]
    result: OrderValidation = ctx.step["validate"]
    output_json = result.model_dump_json()

    board_fn(
        "board.update_task",
        {
            "task_id": task["task_id"],
            "to_status": "validated",
            "output": output_json,
        },
    )
    return {"valid": True, "output_json": output_json}


def _persist_validation_failed(ctx: SagaContext) -> dict[str, Any]:
    """Transition the task to ``validation_failed`` with rejection details."""
    from schemas import OrderValidation

    task = ctx.task
    board_fn: Callable[[str, dict], dict] = task["_board_fn"]
    result: OrderValidation = ctx.step["validate"]
    output_json = result.model_dump_json()

    board_fn(
        "board.update_task",
        {
            "task_id": task["task_id"],
            "to_status": "validation_failed",
            "output": output_json,
            "notes_append": f"Validation failed: {result.rejection_reason}",
        },
    )
    return {"valid": False, "reason": result.rejection_reason}


def _make_validation_saga(inject_failure: str | None = None) -> BuiltSaga:
    from schemas import OrderValidation

    return (
        Saga("validation")
        .deterministic(
            "parse_order",
            _maybe_inject_failure("parse_order", _parse_order_input, inject_failure),
        )
        .reason(
            "validate",
            prompt=PROMPTS_DIR / "validation.md",
            user_message=lambda ctx: {
                k: v for k, v in ctx.step["parse_order"].items() if not k.startswith("_")
            },
            schema=OrderValidation,
        )
        .gate(
            "validation_gate",
            when=lambda ctx: bool(getattr(ctx.step["validate"], "valid", False)),
            on_true="persist_validated",
            on_false="persist_validation_failed",
        )
        .deterministic(
            "persist_validated",
            _maybe_inject_failure("persist_validated", _persist_validated, inject_failure),
        )
        .deterministic(
            "persist_validation_failed",
            _maybe_inject_failure(
                "persist_validation_failed",
                _persist_validation_failed,
                inject_failure,
            ),
        )
        .build()
    )


# ──────────────────────────────────────────────────────────────────────────────
# Inventory saga
# ──────────────────────────────────────────────────────────────────────────────
#
# Flow: parse validated order → LLM stock check → reserve_units (debits
# warehouse) → gate on `sufficient` → persist_stock_confirmed OR
# persist_needs_procurement.
#
# Compensation: _release_units credits the warehouse back if
# reserve_units ran but a later step raised.


def _parse_validation_output(ctx: SagaContext) -> dict[str, Any]:
    """Read the validated order payload the validation saga wrote into task.output."""
    task = ctx.task
    parsed = _parse_json_note(task.get("output"))
    return {
        "sku": parsed.get("sku", ""),
        "quantity": parsed.get("quantity", 0),
        "customer_name": parsed.get("customer_name", ""),
        "delivery_address": parsed.get("delivery_address", ""),
        "unit_price": parsed.get("unit_price", 0.0),
        "total": parsed.get("total", 0.0),
    }


def _inventory_reason_input(ctx: SagaContext) -> dict[str, Any]:
    parsed = ctx.step["parse_validation"]
    board_fn: Callable[[str, dict], dict] = ctx.task["_board_fn"]
    state = board_fn("board.get_full_state", {})
    warehouse = state.get("data", {}).get("warehouse", {})
    catalog = state.get("data", {}).get("product_catalog", {})
    sku = parsed["sku"]
    product = catalog.get(sku, {})
    return {
        "sku": sku,
        "quantity": parsed["quantity"],
        "warehouse_stock": warehouse.get(sku, 0),
        "product_name": product.get("name", sku),
    }


def _reserve_units(ctx: SagaContext) -> dict[str, Any]:
    """Debit the warehouse for the reserved quantity when stock is sufficient.

    No-op (returns a marker dict) when the LLM's decision says stock is
    insufficient — the saga's gate routes to procurement instead, and
    the compensation path for this step is a no-op in that case.
    """
    from schemas import InventoryCheck

    task = ctx.task
    board_fn: Callable[[str, dict], dict] = task["_board_fn"]
    result: InventoryCheck = ctx.step["check_stock"]

    if not result.sufficient:
        return {"debited": False, "reason": "insufficient_stock"}

    parsed = ctx.step["parse_validation"]
    sku = parsed["sku"]
    qty = int(parsed["quantity"])

    state = board_fn("board.get_full_state", {})
    warehouse: dict[str, int] = dict(state.get("data", {}).get("warehouse", {}))
    previous_stock = int(warehouse.get(sku, 0))
    warehouse[sku] = max(0, previous_stock - qty)
    board_fn("board.put_data", {"key": "warehouse", "value": warehouse})

    return {
        "debited": True,
        "sku": sku,
        "qty": qty,
        "previous_stock": previous_stock,
    }


def _release_units(ctx: SagaContext) -> None:
    """Compensation for ``reserve_units``: credit the warehouse back.

    Idempotent via a board-level marker key. If the compensation has
    already completed cleanly, re-entry is a no-op — safe even without
    the runtime's built-in log dedup.
    """
    task = ctx.task
    board_fn: Callable[[str, dict], dict] = task["_board_fn"]
    reservation = ctx.step.get("reserve_units") or {}
    if not reservation.get("debited"):
        return  # reserve_units was a no-op; nothing to undo

    marker_key = f"_comp_marker:{task['task_id']}:reserve_units"
    marker = board_fn("board.get_data", {"key": marker_key}).get("value")
    if marker == "released":
        logger.debug("compensation release_units: already applied; skipping")
        return

    sku = reservation["sku"]
    qty = int(reservation["qty"])
    state = board_fn("board.get_full_state", {})
    warehouse: dict[str, int] = dict(state.get("data", {}).get("warehouse", {}))
    warehouse[sku] = int(warehouse.get(sku, 0)) + qty
    board_fn("board.put_data", {"key": "warehouse", "value": warehouse})
    board_fn("board.put_data", {"key": marker_key, "value": "released"})


def _persist_stock_confirmed(ctx: SagaContext) -> dict[str, Any]:
    from schemas import InventoryCheck

    task = ctx.task
    board_fn: Callable[[str, dict], dict] = task["_board_fn"]
    result: InventoryCheck = ctx.step["check_stock"]
    output_json = result.model_dump_json()
    board_fn(
        "board.update_task",
        {
            "task_id": task["task_id"],
            "to_status": "stock_confirmed",
            "output": output_json,
            "notes_append": (
                f"Stock confirmed: {result.available_qty} available, "
                f"{result.requested_qty} reserved"
            ),
        },
    )
    return {"output_json": output_json}


def _persist_needs_procurement(ctx: SagaContext) -> dict[str, Any]:
    from schemas import InventoryCheck

    task = ctx.task
    board_fn: Callable[[str, dict], dict] = task["_board_fn"]
    result: InventoryCheck = ctx.step["check_stock"]
    output_json = result.model_dump_json()
    board_fn(
        "board.update_task",
        {
            "task_id": task["task_id"],
            "to_status": "needs_procurement",
            "output": output_json,
            "notes_append": (
                f"Insufficient stock: {result.available_qty} available, "
                f"{result.requested_qty} needed"
            ),
        },
    )
    return {"output_json": output_json}


def _make_inventory_saga(inject_failure: str | None = None) -> BuiltSaga:
    from schemas import InventoryCheck

    return (
        Saga("inventory")
        .guard(
            "validated_output_present",
            check=lambda ctx: ctx.task.get("output") not in (None, ""),
        )
        .deterministic(
            "parse_validation",
            _maybe_inject_failure(
                "parse_validation", _parse_validation_output, inject_failure
            ),
        )
        .reason(
            "check_stock",
            prompt=PROMPTS_DIR / "inventory.md",
            user_message=_inventory_reason_input,
            schema=InventoryCheck,
        )
        .deterministic(
            "reserve_units",
            _maybe_inject_failure("reserve_units", _reserve_units, inject_failure),
        )
        .compensate("reserve_units", undo=_release_units)
        .gate(
            "inventory_gate",
            when=lambda ctx: bool(
                getattr(ctx.step["check_stock"], "sufficient", False)
            ),
            on_true="persist_stock_confirmed",
            on_false="persist_needs_procurement",
        )
        .deterministic(
            "persist_stock_confirmed",
            _maybe_inject_failure(
                "persist_stock_confirmed", _persist_stock_confirmed, inject_failure
            ),
        )
        .deterministic(
            "persist_needs_procurement",
            _maybe_inject_failure(
                "persist_needs_procurement",
                _persist_needs_procurement,
                inject_failure,
            ),
        )
        .build()
    )


# ──────────────────────────────────────────────────────────────────────────────
# Procurement saga
# ──────────────────────────────────────────────────────────────────────────────
#
# Flow: parse inventory output → build supplier-offers input → LLM
# supplier selection → procure_units (credits warehouse + records
# supplier order) → persist_procured.
#
# Compensation: _cancel_supplier_order debits the warehouse back and
# records a supplier-cancellation marker.


def _parse_inventory_output(ctx: SagaContext) -> dict[str, Any]:
    parsed = _parse_json_note(ctx.task.get("output"))
    # The validation output still lives in task.notes[0]; pull sku + qty from there.
    order = _order_from_task(ctx.task)
    sku = order.get("sku", "") or parsed.get("sku", "")
    shortfall = int(parsed.get("shortfall", 0) or 0)
    requested = int(parsed.get("requested_qty", 0) or order.get("quantity", 0) or 0)
    return {
        "sku": sku,
        "shortfall": shortfall,
        "units_needed": max(shortfall, requested),
    }


def _procurement_reason_input(ctx: SagaContext) -> dict[str, Any]:
    from data import SUPPLIERS

    parsed = ctx.step["parse_inventory"]
    board_fn: Callable[[str, dict], dict] = ctx.task["_board_fn"]
    state = board_fn("board.get_full_state", {})
    catalog = state.get("data", {}).get("product_catalog", {})
    product = catalog.get(parsed["sku"], {})
    catalog_price = float(product.get("price", 100.0))
    units_needed = int(parsed["units_needed"])

    supplier_offers: list[dict[str, Any]] = []
    for s in SUPPLIERS:
        unit_cost = round(catalog_price * s["price_multiplier"], 2)
        order_qty = max(units_needed, s["min_order"])
        supplier_offers.append({
            "name": s["name"],
            "unit_cost": unit_cost,
            "lead_time_days": s["lead_time_days"],
            "reliability": s["reliability"],
            "min_order": s["min_order"],
            "total_cost": round(unit_cost * order_qty, 2),
        })
    return {
        "sku": parsed["sku"],
        "product_name": product.get("name", parsed["sku"]),
        "catalog_price": catalog_price,
        "units_needed": units_needed,
        "suppliers": supplier_offers,
    }


def _procure_units(ctx: SagaContext) -> dict[str, Any]:
    """Credit the warehouse with the procured units and record a
    supplier-order marker on the board (used by the compensation)."""
    from schemas import ProcurementResult

    task = ctx.task
    board_fn: Callable[[str, dict], dict] = task["_board_fn"]
    result: ProcurementResult = ctx.step["pick_supplier"]
    parsed = ctx.step["parse_inventory"]
    sku = parsed["sku"]
    units_ordered = int(result.units_ordered or parsed["units_needed"])

    state = board_fn("board.get_full_state", {})
    warehouse: dict[str, int] = dict(state.get("data", {}).get("warehouse", {}))
    warehouse[sku] = int(warehouse.get(sku, 0)) + units_ordered
    board_fn("board.put_data", {"key": "warehouse", "value": warehouse})

    # Record the supplier-order marker so the compensation can read it.
    order_marker_key = f"_supplier_order:{task['task_id']}"
    order_marker = {
        "sku": sku,
        "units": units_ordered,
        "supplier": result.supplier_name,
        "unit_cost": result.unit_cost,
        "placed_at": datetime.now(timezone.utc).isoformat(),
    }
    board_fn("board.put_data", {"key": order_marker_key, "value": order_marker})

    return {"units_ordered": units_ordered, "sku": sku, "supplier": result.supplier_name}


def _cancel_supplier_order(ctx: SagaContext) -> None:
    """Compensation for ``procure_units``: debit the warehouse and mark
    the supplier order cancelled. Idempotent."""
    task = ctx.task
    board_fn: Callable[[str, dict], dict] = task["_board_fn"]
    procurement = ctx.step.get("procure_units") or {}
    if not procurement:
        return

    marker_key = f"_comp_marker:{task['task_id']}:procure_units"
    marker = board_fn("board.get_data", {"key": marker_key}).get("value")
    if marker == "cancelled":
        logger.debug("compensation cancel_supplier_order: already applied; skipping")
        return

    sku = procurement.get("sku", "")
    units = int(procurement.get("units_ordered", 0))
    if not sku or units <= 0:
        return

    state = board_fn("board.get_full_state", {})
    warehouse: dict[str, int] = dict(state.get("data", {}).get("warehouse", {}))
    warehouse[sku] = max(0, int(warehouse.get(sku, 0)) - units)
    board_fn("board.put_data", {"key": "warehouse", "value": warehouse})

    order_marker_key = f"_supplier_order:{task['task_id']}"
    existing = board_fn("board.get_data", {"key": order_marker_key}).get("value") or {}
    existing["cancelled_at"] = datetime.now(timezone.utc).isoformat()
    existing["cancelled_reason"] = "compensation rollback"
    board_fn("board.put_data", {"key": order_marker_key, "value": existing})
    board_fn("board.put_data", {"key": marker_key, "value": "cancelled"})


def _persist_procured(ctx: SagaContext) -> dict[str, Any]:
    from schemas import ProcurementResult

    task = ctx.task
    board_fn: Callable[[str, dict], dict] = task["_board_fn"]
    result: ProcurementResult = ctx.step["pick_supplier"]
    output_json = result.model_dump_json()
    parsed = ctx.step["parse_inventory"]
    board_fn(
        "board.update_task",
        {
            "task_id": task["task_id"],
            "to_status": "procured",
            "output": output_json,
            "notes_append": f"Procured {result.units_ordered} units of {parsed['sku']}",
        },
    )
    return {"output_json": output_json}


def _make_procurement_saga(inject_failure: str | None = None) -> BuiltSaga:
    from schemas import ProcurementResult

    return (
        Saga("procurement")
        .guard(
            "inventory_output_present",
            check=lambda ctx: ctx.task.get("output") not in (None, ""),
        )
        .deterministic(
            "parse_inventory",
            _maybe_inject_failure(
                "parse_inventory", _parse_inventory_output, inject_failure
            ),
        )
        .reason(
            "pick_supplier",
            prompt=PROMPTS_DIR / "procurement.md",
            user_message=_procurement_reason_input,
            schema=ProcurementResult,
        )
        .deterministic(
            "procure_units",
            _maybe_inject_failure("procure_units", _procure_units, inject_failure),
        )
        .compensate("procure_units", undo=_cancel_supplier_order)
        .deterministic(
            "persist_procured",
            _maybe_inject_failure(
                "persist_procured", _persist_procured, inject_failure
            ),
        )
        .build()
    )


# ──────────────────────────────────────────────────────────────────────────────
# Logistics saga
# ──────────────────────────────────────────────────────────────────────────────
#
# Flow: reconstruct order details → build carrier-offers input → LLM
# carrier selection → dispatch_shipment (records shipment + transitions
# to shipped).
#
# Compensation: _recall_shipment records a recall intent. In a real
# system you can't literally unship a package — the compensation is the
# operator's recall/return-to-warehouse instruction.


def _reconstruct_order(ctx: SagaContext) -> dict[str, Any]:
    """Pull the order details from the original note + downstream outputs."""
    task = ctx.task
    order = _order_from_task(task)
    latest_output = _parse_json_note(task.get("output"))
    # Walk task notes for the validation-shaped note (may have customer_name etc.).
    enriched: dict[str, Any] = {}
    for note in task.get("notes") or []:
        candidate = _parse_json_note(note)
        if candidate.get("customer_name"):
            enriched = candidate
            break

    sku = enriched.get("sku", "") or order.get("sku", "")
    quantity = int(enriched.get("quantity", 0) or order.get("quantity", 1) or 1)
    board_fn: Callable[[str, dict], dict] = task["_board_fn"]
    state = board_fn("board.get_full_state", {})
    catalog = state.get("data", {}).get("product_catalog", {})
    product = catalog.get(sku, {})
    product_name = product.get("name", sku)
    unit_price = float(
        enriched.get("unit_price", 0)
        or latest_output.get("unit_price", 0)
        or product.get("price", 0.0)
    )
    order_total = float(
        enriched.get("total", 0) or latest_output.get("total", 0) or unit_price * quantity
    )

    return {
        "sku": sku,
        "product_name": product_name,
        "quantity": quantity,
        "customer_name": enriched.get("customer_name", "") or order.get("customer_name", ""),
        "delivery_address": (
            enriched.get("delivery_address", "")
            or order.get("address", "")
            or "Address on file"
        ),
        "unit_price": unit_price,
        "order_total": order_total,
    }


def _logistics_reason_input(ctx: SagaContext) -> dict[str, Any]:
    from data import CARRIERS

    parsed = ctx.step["reconstruct_order"]
    now = datetime.now(timezone.utc)
    quantity = int(parsed["quantity"])
    carrier_options: list[dict[str, Any]] = []
    for c in CARRIERS:
        est_cost = round(c["base_cost"] + (quantity * 1.50), 2)
        est_delivery = (now + timedelta(days=c["speed_days"])).strftime("%Y-%m-%d")
        carrier_options.append({
            "name": c["name"],
            "base_cost": c["base_cost"],
            "speed_days": c["speed_days"],
            "estimated_cost": est_cost,
            "estimated_delivery": est_delivery,
        })
    return {
        "sku": parsed["sku"],
        "product_name": parsed["product_name"],
        "quantity": quantity,
        "customer_name": parsed["customer_name"],
        "delivery_address": parsed["delivery_address"],
        "order_total": parsed["order_total"],
        "carriers": carrier_options,
    }


def _dispatch_shipment(ctx: SagaContext) -> dict[str, Any]:
    """Record a shipment marker and transition the task to ``shipped``."""
    from schemas import ShippingLabel

    task = ctx.task
    board_fn: Callable[[str, dict], dict] = task["_board_fn"]
    result: ShippingLabel = ctx.step["pick_carrier"]
    output_json = result.model_dump_json()

    marker_key = f"_shipment:{task['task_id']}"
    marker = {
        "tracking_number": result.tracking_number,
        "carrier": result.carrier,
        "dispatched_at": datetime.now(timezone.utc).isoformat(),
    }
    board_fn("board.put_data", {"key": marker_key, "value": marker})

    board_fn(
        "board.update_task",
        {
            "task_id": task["task_id"],
            "to_status": "shipped",
            "output": output_json,
            "notes_append": f"Shipped via {result.carrier}",
        },
    )
    return {
        "output_json": output_json,
        "tracking_number": result.tracking_number,
        "carrier": result.carrier,
    }


def _recall_shipment(ctx: SagaContext) -> None:
    """Compensation for ``dispatch_shipment``: record a recall intent
    on the shipment marker. Idempotent."""
    task = ctx.task
    board_fn: Callable[[str, dict], dict] = task["_board_fn"]
    ship = ctx.step.get("dispatch_shipment") or {}
    if not ship:
        return

    marker_key = f"_comp_marker:{task['task_id']}:dispatch_shipment"
    marker = board_fn("board.get_data", {"key": marker_key}).get("value")
    if marker == "recalled":
        logger.debug("compensation recall_shipment: already applied; skipping")
        return

    shipment_key = f"_shipment:{task['task_id']}"
    existing = board_fn("board.get_data", {"key": shipment_key}).get("value") or {}
    existing["recalled_at"] = datetime.now(timezone.utc).isoformat()
    existing["recall_reason"] = "compensation rollback"
    board_fn("board.put_data", {"key": shipment_key, "value": existing})
    board_fn("board.put_data", {"key": marker_key, "value": "recalled"})


def _make_logistics_saga(inject_failure: str | None = None) -> BuiltSaga:
    from schemas import ShippingLabel

    return (
        Saga("logistics")
        .guard(
            "ready_to_ship",
            check=lambda ctx: ctx.task.get("output") not in (None, ""),
        )
        .deterministic(
            "reconstruct_order",
            _maybe_inject_failure(
                "reconstruct_order", _reconstruct_order, inject_failure
            ),
        )
        .reason(
            "pick_carrier",
            prompt=PROMPTS_DIR / "logistics.md",
            user_message=_logistics_reason_input,
            schema=ShippingLabel,
        )
        .deterministic(
            "dispatch_shipment",
            _maybe_inject_failure(
                "dispatch_shipment", _dispatch_shipment, inject_failure
            ),
        )
        .compensate("dispatch_shipment", undo=_recall_shipment)
        .build()
    )


# ──────────────────────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────────────────────


def build_sagas(inject_failure: str | None = None) -> dict[str, BuiltSaga]:
    """Build the four sagas with optional failure injection.

    ``inject_failure`` is the name of a single saga step whose first
    invocation raises synthetically, exercising the compensation walker.
    Legal values are any step name declared across the four sagas
    (``reserve_units``, ``procure_units``, ``dispatch_shipment`` are the
    canonical demo targets because each has a registered compensation).
    """
    return {
        "validation": _make_validation_saga(inject_failure),
        "inventory": _make_inventory_saga(inject_failure),
        "procurement": _make_procurement_saga(inject_failure),
        "logistics": _make_logistics_saga(inject_failure),
    }


# Default-built sagas for static-import callers (the integration test
# and REPL smoke checks). Production callers use ``build_sagas(...)`` so
# the ``--inject-failure`` CLI flag threads through correctly.
validation_saga = _make_validation_saga()
inventory_saga = _make_inventory_saga()
procurement_saga = _make_procurement_saga()
logistics_saga = _make_logistics_saga()
