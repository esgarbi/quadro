from __future__ import annotations

import asyncio
from dataclasses import dataclass
from types import SimpleNamespace as NS

import pytest

from quadro.pipeline import StageSpec
from quadro.runtime_plugins.base import RuntimeContext
from quadro_langchain import LangChainChiefRuntime


@dataclass
class _FakeRunnable:
    result: object
    payloads: list[object]

    async def ainvoke(self, payload):  # noqa: ANN001
        self.payloads.append(payload)
        return self.result


@dataclass
class _TypeErrorThenSuccessRunnable:
    payloads: list[object]
    success_payload: object

    async def ainvoke(self, payload):  # noqa: ANN001
        self.payloads.append(payload)
        if len(self.payloads) == 1:
            raise TypeError("expected string payload")
        return self.success_payload


@dataclass
class _ShutdownFailureRunnable:
    payloads: list[object]

    async def ainvoke(self, payload):  # noqa: ANN001
        self.payloads.append(payload)
        raise RuntimeError("cannot schedule new futures after shutdown")


def test_langchain_runtime_can_handle_graph_or_supervisor() -> None:
    runtime = LangChainChiefRuntime(client_factory=lambda: None)

    assert runtime.can_handle(StageSpec(capability="x", graph=object()))
    assert runtime.can_handle(StageSpec(capability="x", supervisor=object()))
    assert not runtime.can_handle(StageSpec(capability="x"))


def test_invoke_runnable_retries_only_on_type_error() -> None:
    runtime = LangChainChiefRuntime(client_factory=lambda: None)
    runnable = _TypeErrorThenSuccessRunnable(payloads=[], success_payload={"ok": True})

    result, index = asyncio.run(
        runtime._invoke_runnable(
            runnable, attempts=[{"input": "x"}, "x", {"messages": []}]
        )
    )

    assert result == {"ok": True}
    assert index == 1
    assert len(runnable.payloads) == 2


def test_invoke_runnable_surfaces_shutdown_failure_without_retry() -> None:
    runtime = LangChainChiefRuntime(client_factory=lambda: None)
    runnable = _ShutdownFailureRunnable(payloads=[])

    with pytest.raises(
        RuntimeError, match="cannot schedule new futures after shutdown"
    ):
        asyncio.run(runtime._invoke_runnable(runnable, attempts=[{"input": "x"}, "x"]))

    assert len(runnable.payloads) == 1


def test_langchain_runtime_maps_stage_result_and_telemetry() -> None:
    message = NS(
        content=(
            '```json\n{"quadro_stage_result":{"status":"classified","output":{"ok":true},'
            '"notes_append":"done","update_fields":{"label":"L"},"token_total":5,'
            '"terminal_reason":"marker"}}\n```'
        ),
        usage_metadata={"input_tokens": 4, "output_tokens": 2},
        tool_calls=[{"name": "lookup_policy", "id": "tc-1"}],
    )
    runnable = _FakeRunnable(
        result={"messages": [message], "checkpoint_id": "cp-1", "resume_id": "rs-1"},
        payloads=[],
    )
    runtime = LangChainChiefRuntime(client_factory=lambda: None)

    result = asyncio.run(
        runtime.run_stage(
            RuntimeContext(
                stage=StageSpec(
                    capability="classify",
                    success_status="classified",
                    supervisor=runnable,
                ),
                task={"task_id": "task-1", "status": "classifying", "notes": ["hello"]},
                context={"payload": {"task": {"task_id": "task-1"}}},
                board_fn=lambda intent, payload: {},  # noqa: ARG005
                token_reporter=lambda n: None,
                telemetry_sink=lambda event: None,
            )
        )
    )

    assert runnable.payloads
    assert isinstance(runnable.payloads[0], dict)
    assert runnable.payloads[0]["input"] == "hello"
    assert result.output == {"ok": True}
    assert result.status == "classified"
    assert result.notes_append == "done"
    assert result.update_fields == {"label": "L"}
    assert result.checkpoint_id == "cp-1"
    assert result.resume_id == "rs-1"
    assert result.terminal_reason == "marker"
    assert result.token_total == 11
    assert result.telemetry
    assert result.telemetry[0]["event_type"] == "framework.stage_start"
    assert any(e["event_type"] == "framework.tool_call" for e in result.telemetry)
    assert result.telemetry[-1]["event_type"] == "framework.stage_end"


def test_langchain_runtime_uses_safe_fallback_for_empty_task_input() -> None:
    runnable = _FakeRunnable(result={"output": {"ok": True}}, payloads=[])
    runtime = LangChainChiefRuntime(client_factory=lambda: None)

    result = asyncio.run(
        runtime.run_stage(
            RuntimeContext(
                stage=StageSpec(capability="classify", graph=runnable),
                task={
                    "task_id": "task-2",
                    "status": "classifying",
                    "output": None,
                    "notes": [],
                },
                context={"payload": {"task": {"task_id": "task-2"}}},
                board_fn=lambda intent, payload: {},  # noqa: ARG005
                token_reporter=lambda n: None,
                telemetry_sink=lambda event: None,
            )
        )
    )

    assert runnable.payloads
    assert isinstance(runnable.payloads[0], dict)
    assert runnable.payloads[0]["input"] == "{}"
    assert result.output == {"ok": True}
