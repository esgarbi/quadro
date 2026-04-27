"""
Runtime plugin observability envelope for Quadro.

Provides a normalized event shape emitted by framework runtimes so core
governance can remain framework-agnostic while retaining visibility into
framework execution details.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

SCHEMA_VERSION = "quadro.runtime_event.v1"


def build_runtime_event(
    *,
    runtime: str,
    event_type: str,
    source: str = "framework",
    stage: str | None = None,
    task_id: str | None = None,
    chief_cycle: int | None = None,
    lease_id: str | None = None,
    token_prompt: int | None = None,
    token_completion: int | None = None,
    token_total: int | None = None,
    step_name: str | None = None,
    span_id: str | None = None,
    parent_span_id: str | None = None,
    duration_ms: int | None = None,
    status: str | None = None,
    tool_name: str | None = None,
    tool_call_id: str | None = None,
    tool_status: str | None = None,
    latency_ms: int | None = None,
    terminal_reason: str | None = None,
    terminal_source: str | None = None,
    checkpoint_id: str | None = None,
    resume_id: str | None = None,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a normalized runtime telemetry event dict."""
    return {
        "schema_version": SCHEMA_VERSION,
        "event_id": uuid4().hex,
        "ts": datetime.now(UTC).isoformat(timespec="seconds"),
        "source": source,
        "runtime": runtime,
        "event_type": event_type,
        "stage": stage,
        "task_id": task_id,
        "chief_cycle": chief_cycle,
        "lease_id": lease_id,
        "token_prompt": token_prompt,
        "token_completion": token_completion,
        "token_total": token_total,
        "step_name": step_name,
        "span_id": span_id,
        "parent_span_id": parent_span_id,
        "duration_ms": duration_ms,
        "status": status,
        "tool_name": tool_name,
        "tool_call_id": tool_call_id,
        "tool_status": tool_status,
        "latency_ms": latency_ms,
        "terminal_reason": terminal_reason,
        "terminal_source": terminal_source,
        "checkpoint_id": checkpoint_id,
        "resume_id": resume_id,
        "payload": payload or {},
    }


def emit_runtime_event(
    sink: Any | None,
    event: dict[str, Any],
) -> None:
    """Best-effort sink dispatch for runtime telemetry events."""
    if sink is None:
        return
    try:
        sink(event)
    except Exception:  # noqa: BLE001
        # Telemetry must never fail a worker/chief turn.
        return
