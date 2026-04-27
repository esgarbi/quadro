from __future__ import annotations

import asyncio
from dataclasses import dataclass
from types import SimpleNamespace as NS

from quadro import LocalA2ANetwork
from quadro.board.backends.sqlite import SqliteBoardBackend
from quadro.board.board import QuadroBoard
from quadro.pipeline import Pipeline, StageSpec, ToolDescriptor
from quadro.runtime_plugins.base import RuntimeContext, StageRunResult
from quadro.runtime_plugins.maf_workflow import MafWorkflowRuntime
from quadro.runtime_plugins.stage_spec import native_runtime_entrypoint


@dataclass
class _DummyRuntime:
    runtime_id: str = "dummy"
    handled_specs: list[StageSpec] | None = None
    chief_calls: int = 0

    def can_handle(self, spec: StageSpec) -> bool:
        if self.handled_specs is not None:
            self.handled_specs.append(spec)
        return spec.workflow is not None

    def decorate_tools(self, descriptors: list[ToolDescriptor]) -> list:
        return descriptors

    async def run_chief_turn(
        self,
        board_summary: str,
        instructions: str,
        tools: list,
        *,
        chief_name_prefix: str,
    ) -> str | None:
        self.chief_calls += 1
        return "chief-ok"

    async def run_stage(self, ctx: RuntimeContext) -> StageRunResult:
        return StageRunResult(
            output={"ok": True},
            status="classified",
            notes_append="runtime-note",
            update_fields={"label": "Runtime Label"},
            token_total=7,
            telemetry=[{"event_type": "runtime.test"}],
        )


class _DummyPipeline(Pipeline):
    def _decorate_tools(self, descriptors: list[ToolDescriptor]) -> list:
        return descriptors

    async def _run_chief_llm_turn(
        self,
        board_summary: str,
        instructions: str,
        tools: list,
    ) -> str | None:
        return "fallback-chief"

    def _make_auto_execute_fn(self, spec: StageSpec):  # noqa: ANN201
        async def _execute(context: dict, board_fn):  # noqa: ANN001
            return "fallback-stage"

        return _execute


def test_pipeline_runtime_delegation_updates_task_and_observability() -> None:
    board = QuadroBoard(SqliteBoardBackend(":memory:"), network=LocalA2ANetwork())
    runtime = _DummyRuntime(handled_specs=[])
    token_calls: list[int] = []
    telemetry_events: list[dict] = []

    pipeline = (
        _DummyPipeline(board)
        .with_framework_runtime(runtime)
        .runtime_observability(
            token_reporter=token_calls.append,
            telemetry_sink=telemetry_events.append,
        )
        .stage(
            "classify",
            active_status="classifying",
            success_status="classified",
            workflow=object(),
        )
        .chief(prompt="chief prompt")
    )
    built = pipeline.build()
    worker = built.pool.agents[0]
    execute = worker.execute_fn

    board_updates: list[dict] = []

    def _board_fn(intent: str, payload: dict) -> dict:
        if intent == "board.update_task":
            board_updates.append(payload)
            return {"task": payload}
        return {}

    task = {"task_id": "task-1", "status": "classifying", "notes": ["hello"]}
    output = asyncio.run(execute({"payload": {"task": task}}, _board_fn))

    assert output == {"ok": True}
    assert runtime.handled_specs and runtime.handled_specs[0].workflow is not None
    assert board_updates and board_updates[0]["to_status"] == "classified"
    assert board_updates[0]["notes_append"] == "runtime-note"
    assert board_updates[0]["label"] == "Runtime Label"
    assert board_updates[0]["output"] == {"ok": True}
    assert token_calls == [7]
    assert telemetry_events and telemetry_events[0]["event_type"] == "runtime.test"


def test_stage_spec_preserves_native_runtime_entrypoints() -> None:
    board = QuadroBoard(SqliteBoardBackend(":memory:"), network=LocalA2ANetwork())
    pipeline = _DummyPipeline(board).stage(
        "classify",
        workflow="workflow-ref",
        graph="graph-ref",
        supervisor="supervisor-ref",
    )
    spec = pipeline._stages[0]

    assert spec.workflow == "workflow-ref"
    assert spec.graph == "graph-ref"
    assert spec.supervisor == "supervisor-ref"
    assert native_runtime_entrypoint(spec) == ("workflow", "workflow-ref")


class _FakeWorkflow:
    def __init__(self, events: list) -> None:
        self._events = events
        self.messages: list[str] = []

    async def run(self, *, message: str, stream: bool = False):  # noqa: ANN001
        self.messages.append(message)
        return self._events


def test_maf_workflow_runtime_maps_output_tokens_and_telemetry() -> None:
    workflow = _FakeWorkflow(
        [
            NS(type="trace", data=NS(tool_name="advance", tool_call_id="tc-1")),
            NS(type="usage", usage_details=NS(input_tokens=4, output_tokens=2), data=NS()),
            NS(
                type="output",
                data=NS(
                    text=(
                        '```json\n{"quadro_stage_result":'
                        '{"status":"classified","output":{"ok":true},'
                        '"notes_append":"done","update_fields":{"label":"L"},"token_total":5,'
                        '"terminal_reason":"marker"}}\n```'
                    ),
                    continuation_token="r-1",
                ),
            ),
        ]
    )
    runtime = MafWorkflowRuntime(client_factory_getter=lambda: lambda: None)

    result = asyncio.run(
        runtime.run_stage(
            RuntimeContext(
                stage=StageSpec(
                    capability="classify",
                    success_status="classified",
                    workflow=workflow,
                ),
                task={"task_id": "task-1", "status": "classifying", "notes": ["hello"]},
                context={"payload": {"task": {"task_id": "task-1"}}},
                board_fn=lambda intent, payload: {},  # noqa: ARG005
                token_reporter=lambda n: None,
                telemetry_sink=lambda event: None,
            )
        )
    )

    assert workflow.messages == ["hello"]
    assert result.output == {"ok": True}
    assert result.status == "classified"
    assert result.notes_append == "done"
    assert result.update_fields == {"label": "L"}
    assert result.resume_id == "r-1"
    assert result.terminal_reason == "marker"
    assert result.token_total == 11
    assert result.telemetry
    assert result.telemetry[0]["schema_version"] == "quadro.runtime_event.v1"
    assert result.telemetry[0]["event_type"] == "framework.stage_start"
    assert result.telemetry[-1]["event_type"] == "framework.stage_end"

