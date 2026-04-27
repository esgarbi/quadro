from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

EXAMPLE_DIR = Path(__file__).resolve().parents[2] / "examples" / "langchain" / "llm_token_budget"
if str(EXAMPLE_DIR) not in sys.path:
    sys.path.insert(0, str(EXAMPLE_DIR))

import main as demo  # noqa: E402
import supervisor_runtime as supervisor_rt  # noqa: E402


class _FakePipeline:
    def __init__(self, board) -> None:  # noqa: ANN001
        self.board = board
        self._token_reporter = None
        self.stage_calls: list[dict] = []

    def llm(self, **kwargs):  # noqa: ANN003
        self._token_reporter = kwargs.get("token_reporter")
        return self

    def workers(self, n: int):  # noqa: ARG002
        return self

    def capacity(self, n: int):  # noqa: ARG002
        return self

    def wakes(self, url: str):  # noqa: ARG002
        return self

    def stage(self, capability: str, **kwargs):  # noqa: ANN003
        payload = {"capability": capability}
        payload.update(kwargs)
        self.stage_calls.append(payload)
        return self

    def chief(self, **kwargs):  # noqa: ANN003, ARG002
        return self


def test_resolve_stage_mode_defaults_to_native(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(demo.STAGE_MODE_ENV, raising=False)
    assert demo._resolve_stage_mode(None) == "native"


def test_resolve_stage_mode_reads_environment_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(demo.STAGE_MODE_ENV, "compat")
    assert demo._resolve_stage_mode(None) == "compat"


def test_resolve_stage_mode_rejects_invalid_values() -> None:
    with pytest.raises(ValueError):
        demo._resolve_stage_mode("not-a-mode")


def test_classify_stage_config_native_uses_supervisor() -> None:
    cfg = demo._classify_stage_config("native")
    assert "supervisor" in cfg
    assert callable(cfg["supervisor"])
    assert "prompt" not in cfg
    assert "output_schema" not in cfg


def test_classify_stage_config_compat_uses_prompt_schema() -> None:
    cfg = demo._classify_stage_config("compat")
    assert cfg["prompt"] == demo.HERE / "prompts" / "classify.md"
    assert cfg["output_schema"] is demo.TicketTag
    assert "supervisor" not in cfg


def test_build_pipeline_builder_native_wires_supervisor_and_metering(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(demo, "LangChainPipeline", _FakePipeline)
    reporter = lambda n: None  # noqa: E731
    runtime = SimpleNamespace(
        board=object(),
        meters=SimpleNamespace(report_llm_tokens=reporter),
    )
    builder = demo.build_pipeline_builder(runtime, stage_mode="native")
    stage = builder.stage_calls[0]

    assert stage["supervisor"] is not None
    assert "prompt" not in stage
    assert "output_schema" not in stage
    assert builder._token_reporter is reporter


def test_build_pipeline_builder_compat_wires_prompt_schema_and_metering(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(demo, "LangChainPipeline", _FakePipeline)
    reporter = lambda n: None  # noqa: E731
    runtime = SimpleNamespace(
        board=object(),
        meters=SimpleNamespace(report_llm_tokens=reporter),
    )
    builder = demo.build_pipeline_builder(runtime, stage_mode="compat")
    stage = builder.stage_calls[0]

    assert "supervisor" not in stage
    assert stage["prompt"] == demo.HERE / "prompts" / "classify.md"
    assert stage["output_schema"] is demo.TicketTag
    assert builder._token_reporter is reporter


def test_supervisor_runtime_normalizes_valid_classifier_json() -> None:
    output_json, status, notes = supervisor_rt._normalize_classifier_output(
        '{"urgency":"high","category":"outage","suggested_reply":"Thanks, we are on it."}'
    )
    assert status == "classified"
    assert notes is None
    assert "outage" in output_json


def test_supervisor_runtime_marks_invalid_payload_as_failed() -> None:
    output_json, status, notes = supervisor_rt._normalize_classifier_output("not json")
    assert status == "classify_failed"
    assert notes
    assert output_json == "not json"
