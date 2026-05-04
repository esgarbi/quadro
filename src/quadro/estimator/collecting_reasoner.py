"""Dry-run reasoner used by the estimator's first pass.

Experimental internal API. The public surface is :class:`quadro.Estimator`;
this module exists so future contributors can improve collection behavior
without changing the estimator facade.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from quadro.saga.reasoner import ReasonResult


@dataclass(frozen=True)
class Observation:
    """One reason step's input shape, recorded during pass 1."""

    task_index: int
    task_id: str
    step_name: str
    prompt_chars: int
    user_message_chars: int
    schema_name: str | None
    schema_chars: int

    @property
    def total_input_chars(self) -> int:
        return self.prompt_chars + self.user_message_chars + self.schema_chars


class CollectingReasoner:
    """Reasoner that records inputs without making LLM calls.

    Experimental internal API. It returns schema-shaped placeholders so the
    saga walk can continue through deterministic, guard, expect, evidence,
    stamp, gate, and parallel steps without spending model tokens.
    """

    reasoner_id: str = "_collecting"

    def __init__(self) -> None:
        self.observations: list[Observation] = []
        self.current_task_index: int = 0
        self.current_task_id: str = "<unknown>"

    async def reason(
        self,
        *,
        prompt: str,
        user_message: str,
        schema: type | None,
        token_reporter: Callable[[int], None] | None,
        step_name: str | None = None,
    ) -> ReasonResult:
        del token_reporter
        self.observations.append(
            Observation(
                task_index=self.current_task_index,
                task_id=self.current_task_id,
                step_name=step_name or "<unknown>",
                prompt_chars=len(prompt),
                user_message_chars=len(str(user_message)),
                schema_name=getattr(schema, "__name__", None) if schema else None,
                schema_chars=_serialized_schema_size(schema),
            )
        )
        return ReasonResult(
            output=_placeholder_for_schema(schema),
            tokens_used=0,
            raw_text="<dry-run placeholder>",
        )


def _placeholder_for_schema(schema: type | None) -> Any:
    """Return a value satisfying the schema for pass-1 placeholder use."""
    if schema is None:
        return "<dry-run placeholder>"
    if hasattr(schema, "model_construct"):
        return schema.model_construct()
    if hasattr(schema, "construct"):
        return schema.construct()
    try:
        return schema()
    except Exception:  # noqa: BLE001
        return None


def _serialized_schema_size(schema: type | None) -> int:
    """Approximate character size of the schema's JSON representation."""
    if schema is None:
        return 0
    try:
        if hasattr(schema, "model_json_schema"):
            return len(json.dumps(schema.model_json_schema()))
    except Exception:  # noqa: BLE001
        return 0
    return 0
