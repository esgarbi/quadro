"""
Native workflow builders for Newsroom pipeline stages.

These builders expose ``stage(workflow=...)`` entrypoints while reusing the
existing stage execute_fn logic from ``agents.py``. The wrapper captures the
stage's intended board transition payload and returns it as normalized workflow
output, so Quadro's runtime wrapper remains the single commit point.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from types import SimpleNamespace as NS
from typing import Any

from agents import run_ideation, run_research, run_review, run_writing

_TOKENS_KEY_PREFIX = "_tokens:"
_STAGE_RESULT_MARKER = "quadro_stage_result"


def _read_stage_tokens(board_fn, task_id: str, stage: str) -> int:  # noqa: ANN001
    """Best-effort read of cumulative tokens for one stage on one task."""
    try:
        raw = board_fn("board.get_data", {"key": f"{_TOKENS_KEY_PREFIX}{task_id}"})
    except Exception:  # noqa: BLE001
        return 0
    value = (raw or {}).get("value") or {}
    by_stage = value.get("by_stage") or {}
    try:
        return int(by_stage.get(stage, 0))
    except Exception:  # noqa: BLE001
        return 0


@dataclass
class _ExecuteFnWorkflow:
    """Adapter that presents an execute_fn as a workflow-like object."""

    runtime_ctx: Any
    stage_name: str
    stage_fn: Any

    async def run(self, *, message: str, stream: bool = False):  # noqa: ANN001
        _ = (message, stream)  # keep signature parity with workflow.run(...)

        task = self.runtime_ctx.task
        board_fn = self.runtime_ctx.board_fn
        task_id = task["task_id"]
        before = _read_stage_tokens(board_fn, task_id, self.stage_name)
        captured_update: dict[str, Any] | None = None

        def _proxy_board_fn(intent: str, payload: dict) -> dict:
            nonlocal captured_update
            if intent == "board.update_task" and payload.get("task_id") == task_id:
                captured_update = dict(payload)
                return {"task": payload}
            return board_fn(intent, payload)

        worker_context = {"payload": {"task": task}}
        stage_return = await self.stage_fn(worker_context, _proxy_board_fn)
        after = _read_stage_tokens(board_fn, task_id, self.stage_name)
        token_delta = max(0, after - before)

        update = captured_update or {}
        output_value = update.get("output", task.get("output", stage_return))
        status = update.get("to_status", task.get("status"))
        notes_append = update.get("notes_append")
        update_fields = {
            key: value
            for key, value in update.items()
            if key not in {"task_id", "to_status", "output", "notes_append"}
        }

        stage_result = {
            "status": status,
            "output": output_value,
            "notes_append": notes_append,
            "update_fields": update_fields or None,
            "token_total": token_delta,
            "terminal_reason": "workflow_completed",
        }

        return [
            NS(
                type="output",
                data=NS(text=json.dumps({_STAGE_RESULT_MARKER: stage_result})),
            )
        ]


def build_ideation_workflow(runtime_ctx):
    return _ExecuteFnWorkflow(runtime_ctx, "ideation", run_ideation)


def build_research_workflow(runtime_ctx):
    return _ExecuteFnWorkflow(runtime_ctx, "research", run_research)


def build_writing_workflow(runtime_ctx):
    return _ExecuteFnWorkflow(runtime_ctx, "writing", run_writing)


def build_review_workflow(runtime_ctx):
    return _ExecuteFnWorkflow(runtime_ctx, "review", run_review)

