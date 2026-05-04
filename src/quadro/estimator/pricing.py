"""Pricing configuration for optional dollar projection."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ModelPricing:
    """Pricing for a single model.

    ``input_per_mtok`` and ``output_per_mtok`` are dollars per million tokens.
    ``io_ratio`` estimates the fraction of total tokens that are output tokens
    when only total-token records are available.
    """

    input_per_mtok: float
    output_per_mtok: float
    io_ratio: float = 0.30

    def __post_init__(self) -> None:
        if self.input_per_mtok < 0:
            raise ValueError("input_per_mtok must be >= 0")
        if self.output_per_mtok < 0:
            raise ValueError("output_per_mtok must be >= 0")
        if not 0 <= self.io_ratio <= 1:
            raise ValueError("io_ratio must be between 0 and 1")


@dataclass(frozen=True)
class Pricing:
    """Project-wide pricing configuration.

    Dollar projection is approximate because Quadro currently persists total
    tokens only. The configured ``io_ratio`` splits total tokens into estimated
    input/output portions until a future token-record extension stores both.
    """

    models: dict[str, ModelPricing]
    source_label: str = "configured at runtime startup"
    last_verified: str | None = None
    verify_url: str | None = None

    def __post_init__(self) -> None:
        if not self.models:
            raise ValueError("Pricing requires at least one model")

    def cost_for_tokens(self, model: str, total_tokens: int) -> float:
        """Compute approximate dollar cost for ``total_tokens`` of ``model``."""
        if total_tokens <= 0:
            return 0.0
        pricing = self.models.get(model)
        if pricing is None and len(self.models) == 1:
            pricing = next(iter(self.models.values()))
        if pricing is None:
            return 0.0
        output_tokens = total_tokens * pricing.io_ratio
        input_tokens = total_tokens - output_tokens
        return (
            input_tokens * pricing.input_per_mtok / 1_000_000
            + output_tokens * pricing.output_per_mtok / 1_000_000
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize to the Board ``_pricing`` data key shape."""
        return {
            "models": {
                name: {
                    "input": model.input_per_mtok,
                    "output": model.output_per_mtok,
                    "io_ratio": model.io_ratio,
                }
                for name, model in self.models.items()
            },
            "source_label": self.source_label,
            "last_verified": self.last_verified,
            "verify_url": self.verify_url,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Pricing:
        """Load pricing from a JSON-compatible dict."""
        models_raw = raw.get("models") or {}
        return cls(
            models={
                str(name): ModelPricing(
                    input_per_mtok=float(value["input"]),
                    output_per_mtok=float(value["output"]),
                    io_ratio=float(value.get("io_ratio", 0.30)),
                )
                for name, value in models_raw.items()
            },
            source_label=str(
                raw.get("source_label") or "configured at runtime startup"
            ),
            last_verified=raw.get("last_verified"),
            verify_url=raw.get("verify_url"),
        )
