from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

EXAMPLE_DIR = Path(__file__).resolve().parents[2] / "examples" / "token_budget"
if str(EXAMPLE_DIR) not in sys.path:
    sys.path.insert(0, str(EXAMPLE_DIR))

import main as demo  # noqa: E402
import supervisor_runtime as supervisor_rt  # noqa: E402


class _FakePipeline:
    """Substrate-style fake that captures the composition calls.

    Post-milestone-J1, ``build_pipeline_builder`` constructs a plain
    :class:`quadro.Pipeline` and composes the LangChain adapter via
    ``.reasoner(...)`` and ``.with_framework_runtime(...)``. This fake
    stands in for ``quadro.Pipeline`` so the test can observe every
    builder call without instantiating the real sqlite-backed board.
    """

    def __init__(self, board) -> None:  # noqa: ANN001
        self.board = board
        self.reasoners: list = []
        self.runtimes: list = []
        self._token_reporter = None
        self.stage_calls: list[dict] = []

    def reasoner(self, reasoner):  # noqa: ANN001
        self.reasoners.append(reasoner)
        return self

    def with_framework_runtime(self, runtime):  # noqa: ANN001
        self.runtimes.append(runtime)
        return self

    def runtime_observability(self, **kwargs):  # noqa: ANN003
        if "token_reporter" in kwargs:
            self._token_reporter = kwargs["token_reporter"]
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
    # No execute_fn on the native path — the registered chief runtime
    # handles the supervisor entrypoint directly.
    assert "execute_fn" not in cfg


def test_classify_stage_config_compat_returns_execute_fn() -> None:
    cfg = demo._classify_stage_config("compat")
    # Substrate-composition shape: the compat path builds an explicit
    # execute_fn via quadro_langchain.make_auto_execute_fn rather than
    # passing ``prompt=``/``output_schema=`` kwargs to ``.stage()``.
    assert "execute_fn" in cfg
    assert callable(cfg["execute_fn"])
    assert "supervisor" not in cfg
    # prompt/output_schema are captured inside the built execute_fn's
    # closure, not surfaced as stage kwargs.
    assert "prompt" not in cfg
    assert "output_schema" not in cfg


def test_build_pipeline_builder_native_wires_supervisor_and_metering(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(demo, "Pipeline", _FakePipeline)
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
    assert "execute_fn" not in stage
    # Token reporter is wired at three surfaces: (1) onto the
    # registered reasoner, (2) onto the registered chief runtime,
    # (3) onto the pipeline's runtime_observability. The test asserts
    # (3) because it's the shared sink the runtime's
    # ``_make_runtime_execute_fn`` reads from.
    assert builder._token_reporter is reporter
    # And the reasoner + runtime got the same reporter.
    assert builder.reasoners and builder.reasoners[0]._token_reporter is reporter
    assert builder.runtimes and builder.runtimes[0].token_reporter is reporter


def test_build_pipeline_builder_compat_wires_execute_fn_and_metering(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(demo, "Pipeline", _FakePipeline)
    reporter = lambda n: None  # noqa: E731
    runtime = SimpleNamespace(
        board=object(),
        meters=SimpleNamespace(report_llm_tokens=reporter),
    )
    builder = demo.build_pipeline_builder(runtime, stage_mode="compat")
    stage = builder.stage_calls[0]

    assert "supervisor" not in stage
    assert "execute_fn" in stage
    assert callable(stage["execute_fn"])
    assert builder._token_reporter is reporter
    assert builder.reasoners and builder.reasoners[0]._token_reporter is reporter
    assert builder.runtimes and builder.runtimes[0].token_reporter is reporter


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
