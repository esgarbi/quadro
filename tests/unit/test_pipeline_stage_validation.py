from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from quadro.pipeline import Pipeline, StageSpec


class _DummyBoard:
    def client(self):  # noqa: ANN201
        class _DummyClient:
            network = object()

        return _DummyClient()


@dataclass
class _SchemaStageSpec(StageSpec):
    output_schema: type | None = None


class _SchemaAwarePipeline(Pipeline):
    """Pipeline subclass that merely widens ``StageSpec`` with an
    ``output_schema`` field so the substrate's ``_validate_stages``
    unsafe-schema check has something to assert against. The three
    abstract hooks deleted in milestone J1 are not overridden here —
    after J1, LLM-framework integration is composed via
    ``.reasoner(...)`` and ``.with_framework_runtime(...)``, not
    subclassing.
    """

    def _make_stage_spec(self, capability: str, **kwargs: Any) -> StageSpec:
        allowed = {
            k: v
            for k, v in kwargs.items()
            if k in _SchemaStageSpec.__dataclass_fields__
        }
        return _SchemaStageSpec(capability, **allowed)


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
