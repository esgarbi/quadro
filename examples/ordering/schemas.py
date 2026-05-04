"""
Pydantic models for the ordering system pipeline.

Each model represents one agent's structured output:
  OrderValidation   — validation agent output
  InventoryCheck    — inventory scout output
  ProcurementResult — procurement negotiator output
  ShippingLabel     — logistics coordinator output
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class OrderValidation(BaseModel):
    valid: bool = Field(description="Whether the order passes validation")
    sku: str = Field(description="Product SKU from the catalog")
    quantity: int = Field(description="Requested quantity")
    unit_price: float = Field(description="Unit price from catalog")
    total: float = Field(description="quantity * unit_price")
    customer_name: str = Field(description="Customer full name")
    delivery_address: str = Field(default="", description="Shipping address")
    rejection_reason: str = Field(default="", description="Reason if order is invalid")


class InventoryCheck(BaseModel):
    sufficient: bool = Field(description="Whether warehouse stock covers the order")
    available_qty: int = Field(description="Current stock level for the SKU")
    requested_qty: int = Field(description="Quantity the order requires")
    shortfall: int = Field(default=0, description="Units short (0 if sufficient)")
    recommendation: str = Field(
        default="",
        description="Action recommendation: 'fulfill' or 'procure N units'",
    )


class ProcurementResult(BaseModel):
    supplier_name: str = Field(description="Chosen supplier name")
    units_ordered: int = Field(description="Number of units procured")
    unit_cost: float = Field(description="Per-unit wholesale cost")
    total_cost: float = Field(description="Total procurement cost")
    lead_time_days: int = Field(description="Expected delivery in days")
    negotiation_notes: str = Field(
        default="",
        description="Brief note on why this supplier was chosen",
    )


class ShippingLabel(BaseModel):
    carrier: str = Field(description="Selected shipping carrier")
    tracking_number: str = Field(description="Generated tracking number")
    estimated_delivery: str = Field(description="Estimated delivery date string")
    shipping_cost: float = Field(description="Shipping cost in USD")
    delivery_address: str = Field(description="Customer delivery address")
    order_summary: str = Field(
        default="",
        description="Brief one-line order summary for the label",
    )
