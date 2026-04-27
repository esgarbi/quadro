from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

pytest.importorskip("agent_framework")

NEWSROOM_DIR = (
    Path(__file__).resolve().parents[2]
    / "examples"
    / "microsoft_agent_framework"
    / "newsroom"
)
if str(NEWSROOM_DIR) not in sys.path:
    sys.path.insert(0, str(NEWSROOM_DIR))

from workflows import _ExecuteFnWorkflow, _STAGE_RESULT_MARKER  # noqa: E402


def _make_board_fn():
    data_store: dict[str, dict] = {}

    def _board_fn(intent: str, payload: dict) -> dict:
        if intent == "board.get_data":
            return {"value": data_store.get(payload["key"])}
        if intent == "board.put_data":
            data_store[payload["key"]] = payload["value"]
            return {"ok": True}
        return {}

    return _board_fn, data_store


def test_execute_fn_workflow_captures_update_and_token_delta() -> None:
    board_fn, _ = _make_board_fn()
    task = {"task_id": "task-1", "status": "ideating", "output": None}
    key = "_tokens:task-1"

    async def _stage_fn(context: dict, proxy_board_fn):  # noqa: ANN001
        proxy_board_fn("board.put_data", {"key": key, "value": {"by_stage": {"ideation": 5}}})
        proxy_board_fn(
            "board.update_task",
            {
                "task_id": "task-1",
                "to_status": "idea_ready",
                "output": {"brief": "ok"},
                "label": "New Title",
            },
        )
        return "done"

    workflow = _ExecuteFnWorkflow(
        runtime_ctx=NS(task=task, board_fn=board_fn),
        stage_name="ideation",
        stage_fn=_stage_fn,
    )
    events = asyncio.run(workflow.run(message="ignored", stream=False))

    payload = json.loads(events[0].data.text)[_STAGE_RESULT_MARKER]
    assert payload["status"] == "idea_ready"
    assert payload["output"] == {"brief": "ok"}
    assert payload["update_fields"] == {"label": "New Title"}
    assert payload["token_total"] == 5


def test_execute_fn_workflow_defaults_when_no_update_emitted() -> None:
    board_fn, _ = _make_board_fn()
    task = {"task_id": "task-2", "status": "reviewing", "output": {"draft": "x"}}

    async def _stage_fn(context: dict, proxy_board_fn):  # noqa: ANN001
        _ = (context, proxy_board_fn)
        return "noop"

    workflow = _ExecuteFnWorkflow(
        runtime_ctx=NS(task=task, board_fn=board_fn),
        stage_name="review",
        stage_fn=_stage_fn,
    )
    events = asyncio.run(workflow.run(message="ignored", stream=False))

    payload = json.loads(events[0].data.text)[_STAGE_RESULT_MARKER]
    assert payload["status"] == "reviewing"
    assert payload["output"] == {"draft": "x"}
    assert payload["token_total"] == 0
