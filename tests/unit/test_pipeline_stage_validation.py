from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from quadro.pipeline import Pipeline, StageSpec, ToolDescriptor


class _DummyBoard:
    def client(self):  # noqa: ANN201
        class _DummyClient:
            network = object()

        return _DummyClient()


@dataclass
class _SchemaStageSpec(StageSpec):
    output_schema: type | None = None


class _SchemaAwarePipeline(Pipeline):
    def _make_stage_spec(self, capability: str, **kwargs: Any) -> StageSpec:
        allowed = {
            k: v
            for k, v in kwargs.items()
            if k in _SchemaStageSpec.__dataclass_fields__
        }
        return _SchemaStageSpec(capability, **allowed)

    def _decorate_tools(self, descriptors: list[ToolDescriptor]) -> list:
        return descriptors

    async def _run_chief_llm_turn(
        self,
        board_summary: str,
        instructions: str,
        tools: list,
    ) -> str | None:
        return None

    def _make_auto_execute_fn(self, spec: StageSpec):  # noqa: ANN201
        return lambda context, board_fn: "{}"  # noqa: ARG005


class _Schema:
    @classmethod
    def model_json_schema(cls) -> dict:  # noqa: ANN102
        return {"type": "object"}


def test_build_rejects_output_schema_without_failure_status() -> None:
    pipeline = _SchemaAwarePipeline(_DummyBoard()).stage(
        "classify",
        output_schema=_Schema,
        active_status="classifying",
        success_status="classified",
    )

    with pytest.raises(ValueError, match="output_schema requires failure_status"):
        pipeline.build()


def test_stage_validation_allows_schema_when_failure_status_present() -> None:
    pipeline = _SchemaAwarePipeline(_DummyBoard()).stage(
        "classify",
        output_schema=_Schema,
        active_status="classifying",
        success_status="classified",
        failure_status="classify_failed",
    )

    pipeline._validate_stages()


def test_stage_validation_ignores_stages_without_schema() -> None:
    pipeline = _SchemaAwarePipeline(_DummyBoard()).stage(
        "classify",
        active_status="classifying",
        success_status="classified",
    )

    pipeline._validate_stages()
