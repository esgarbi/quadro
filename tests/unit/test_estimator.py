from __future__ import annotations

import asyncio
from collections.abc import Callable

import pytest
from pydantic import BaseModel

from quadro import Estimator, LifecycleBuilder, Pipeline, QuadroRuntime, Saga
from quadro.board.backends import SqliteBoardBackend
from quadro.estimator.collecting_reasoner import CollectingReasoner, Observation
from quadro.estimator.estimator import _format_tokens
from quadro.estimator.pricing import ModelPricing, Pricing
from quadro.estimator.projector import Projector
from quadro.estimator.sampling import SamplingStrategy
from quadro.runtime_plugins.base import RuntimeContext
from quadro.saga.reasoner import ReasonResult


class Answer(BaseModel):
    text: str = ""


class FakeReasoner:
    reasoner_id = "fake"

    def __init__(self) -> None:
        self.calls = 0
        self.step_names: list[str | None] = []

    async def reason(
        self,
        *,
        prompt: str,
        user_message: str,
        schema: type | None,
        token_reporter: Callable[[int], None] | None,
        step_name: str | None = None,
    ) -> ReasonResult:
        self.calls += 1
        self.step_names.append(step_name)
        tokens = max(2, len(prompt) + len(user_message))
        if token_reporter is not None:
            token_reporter(tokens)
        output = schema(text="ok") if schema is not None else "ok"
        return ReasonResult(output=output, tokens_used=tokens, raw_text="ok")


def _profile():
    return (
        LifecycleBuilder()
        .phase("UNASSIGNED", "working")
        .phase("working", "done")
        .build()
    )


def _saga():
    def persist(ctx):
        ctx.task["_board_fn"](
            "board.update_task",
            {"task_id": ctx.task["task_id"], "to_status": "done"},
        )
        return {"ok": True}

    return (
        Saga("estimate_test")
        .reason(
            "answer",
            prompt="Answer briefly.",
            user_message=lambda ctx: ctx.task["label"],
            schema=Answer,
        )
        .deterministic("persist", persist)
        .build()
    )


def _pipeline(reasoner: FakeReasoner | None = None):
    runtime = QuadroRuntime(SqliteBoardBackend(":memory:")).with_profiles(
        profile_resolver={"estimate": "estimate"},
        custom_profiles={"estimate": _profile()},
    )
    runtime.with_pricing({"fake": {"input": 1.0, "output": 3.0, "io_ratio": 0.25}})
    pipe = (
        Pipeline(runtime.board)
        .reasoner(reasoner or FakeReasoner())
        .stage("answer", saga=_saga(), active_status="working")
    )
    return runtime, pipe


def test_collecting_reasoner_records_inputs() -> None:
    reasoner = CollectingReasoner()
    reasoner.current_task_index = 7
    reasoner.current_task_id = "task-7"
    result = asyncio.run(
        reasoner.reason(
            prompt="system",
            user_message="hello",
            schema=Answer,
            token_reporter=None,
            step_name="answer",
        )
    )
    assert result.tokens_used == 0
    assert reasoner.observations[0].task_index == 7
    assert reasoner.observations[0].step_name == "answer"
    assert reasoner.observations[0].total_input_chars >= len("systemhello")


def test_collecting_reasoner_returns_text_placeholder() -> None:
    reasoner = CollectingReasoner()
    result = asyncio.run(
        reasoner.reason(
            prompt="p",
            user_message="u",
            schema=None,
            token_reporter=None,
            step_name=None,
        )
    )
    assert result.output == "<dry-run placeholder>"


def test_sampling_picks_smallest_and_largest_first() -> None:
    observations = [
        Observation(i, str(i), "s", 0, size, None, 0)
        for i, size in enumerate([40, 10, 80, 20, 60])
    ]
    picked = SamplingStrategy(target_samples=3).pick_indices(observations)
    assert 1 in picked
    assert 2 in picked
    assert len(picked) == 3


def test_sampling_returns_all_when_queue_smaller_than_target() -> None:
    observations = [Observation(i, str(i), "s", 0, i, None, 0) for i in range(2)]
    assert SamplingStrategy(target_samples=5).pick_indices(observations) == [0, 1]


def test_pricing_cost_for_tokens_uses_io_ratio() -> None:
    pricing = Pricing({"fake": ModelPricing(1.0, 3.0, io_ratio=0.25)})
    assert pricing.cost_for_tokens("fake", 1_000_000) == pytest.approx(1.5)


def test_pricing_unknown_model_returns_zero_when_multiple_models() -> None:
    pricing = Pricing(
        {
            "a": ModelPricing(1.0, 1.0),
            "b": ModelPricing(1.0, 1.0),
        }
    )
    assert pricing.cost_for_tokens("missing", 1000) == 0.0


def test_projection_reports_variance_warning_when_cov_high() -> None:
    from quadro.estimator.calibration import Calibration, TaskCalibration

    projection = Projector().project(
        Calibration(
            [
                TaskCalibration("a", 100, {"stage": 100}, by_model={"fake": 100}),
                TaskCalibration("b", 1000, {"stage": 1000}, by_model={"fake": 1000}),
            ]
        ),
        10,
        None,
        None,
    )
    assert projection.variance_warning
    assert projection.total_tokens == 5500
    assert projection.total_tokens_high > projection.total_tokens


def test_projection_widens_ci_when_extrapolating_far_beyond_sample_size() -> None:
    """Prediction interval must compound parameter uncertainty.

    Naive standard-error-of-the-sum scales as sqrt(N). The correct
    prediction interval, which accounts for the fact that we estimated
    the mean from only n samples, scales as sqrt(N + N**2/n). When N is
    much larger than n, the second term dominates and the interval grows
    proportionally with N rather than sqrt(N).

    This test guards against regressing back to the naive formula by
    asserting that the relative CI width does NOT collapse to near zero
    when projecting from a small sample to a large queue. With the data
    below the naive formula gives a relative width of ~2%; the corrected
    formula gives ~116%.
    """
    from quadro.estimator.calibration import Calibration, TaskCalibration

    # 4 calibration samples with mean 100 and Bessel-corrected stdev ~36.5.
    samples = [
        TaskCalibration(str(i), tokens, {"s": tokens}, by_model={"m": tokens})
        for i, tokens in enumerate([60, 80, 120, 140])
    ]
    projection = Projector().project(
        Calibration(samples),
        n_tasks=10_000,
        pricing=None,
        sample_cost_dollars=None,
    )
    assert projection.total_tokens == 1_000_000  # 100 * 10000
    interval_width = projection.total_tokens_high - projection.total_tokens_low
    relative_width = interval_width / projection.total_tokens
    # Naive sqrt(N) formula would give ~0.02; corrected formula gives ~1.16.
    assert relative_width > 0.5, (
        f"prediction interval too narrow ({relative_width:.2%}) — "
        "the projector may have regressed to the naive sqrt(N) formula"
    )


def test_runtime_with_pricing_writes_to_board() -> None:
    runtime = QuadroRuntime(SqliteBoardBackend(":memory:")).with_pricing(
        {"fake": {"input": 1.0, "output": 2.0}}
    )
    assert runtime.pricing is not None
    assert runtime.client.get_data("_pricing")["models"]["fake"]["input"] == 1.0


def test_dry_run_walks_all_tasks_and_restores_reasoner() -> None:
    fake = FakeReasoner()
    _runtime, pipe = _pipeline(fake)
    estimator = Estimator.from_dry_run(
        pipe,
        [
            {"task_type": "estimate", "label": "short"},
            {"task_type": "estimate", "label": "a much longer task label"},
            {"task_type": "estimate", "label": "middle"},
        ],
        max_samples=2,
    )
    assert estimator.observed_tasks == 3
    assert estimator.calibration.n == 2
    assert fake.calls == 2
    assert pipe._saga_runtime._reasoners["fake"] is fake  # noqa: SLF001
    assert fake.step_names == ["answer", "answer"]


def test_saga_runtime_passes_step_name_to_reasoner() -> None:
    fake = FakeReasoner()
    _runtime, pipe = _pipeline(fake)
    stage = pipe._stages[0]  # noqa: SLF001
    task = {"task_id": "t1", "task_type": "estimate", "label": "hello"}
    data = {}

    def board_fn(intent: str, payload: dict) -> dict:
        if intent == "board.get_full_state":
            return {"tasks": [task], "agents": [], "data": data}
        if intent == "board.get_data":
            return {"value": data.get(payload["key"])}
        if intent == "board.put_data":
            data[payload["key"]] = payload["value"]
            return {}
        if intent == "board.update_task":
            task["status"] = payload["to_status"]
            return {"task": task}
        return {}

    asyncio.run(
        pipe._saga_runtime.run_stage(  # noqa: SLF001
            RuntimeContext(stage=stage, task=task, context={}, board_fn=board_fn)
        )
    )
    assert fake.step_names == ["answer"]


def test_format_omits_dollar_lines_without_pricing() -> None:
    from quadro.estimator.calibration import Calibration, TaskCalibration

    estimator = Estimator(
        calibration=Calibration([TaskCalibration("a", 10), TaskCalibration("b", 20)])
    )
    output = estimator.format()
    assert "Total dollars" not in output
    assert _format_tokens(1500) == "1.5K"
