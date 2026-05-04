"""Public estimator facade for projected token and dollar costs."""

from __future__ import annotations

import asyncio
import copy
import time
from dataclasses import dataclass
from typing import Any

from quadro.runtime_plugins.base import RuntimeContext
from quadro.saga.state import SagaState
from quadro.saga.steps import StepKind

from .calibration import Calibration, TaskCalibration
from .collecting_reasoner import CollectingReasoner, Observation
from .pricing import Pricing
from .projector import Projection, Projector
from .sampling import SamplingStrategy


@dataclass(frozen=True)
class _TaskObservation:
    task_index: int
    task_id: str
    total_input_chars: int
    observations: list[Observation]


class Estimator:
    """Project token and dollar costs for a saga-backed pipeline.

    Only :meth:`from_dry_run` and :meth:`from_history` are public
    constructors. The collaborator objects in ``quadro.estimator`` are
    experimental internals used to keep the implementation composable.
    """

    def __init__(
        self,
        *,
        calibration: Calibration,
        pricing: Pricing | None = None,
        confidence: float = 0.95,
        default_n_tasks: int | None = None,
        sample_cost_dollars: float | None = None,
        pass_one_seconds: float | None = None,
        observed_tasks: int | None = None,
        sampled_task_observations: list[_TaskObservation] | None = None,
    ) -> None:
        self.calibration = calibration
        self.pricing = pricing
        self.confidence = confidence
        self.default_n_tasks = default_n_tasks or calibration.n
        self.sample_cost_dollars = sample_cost_dollars
        self.pass_one_seconds = pass_one_seconds
        self.observed_tasks = observed_tasks
        self.sampled_task_observations = sampled_task_observations or []

    @classmethod
    def from_dry_run(
        cls,
        pipeline: Any,
        queue: list[dict],
        *,
        max_sample_cost_dollars: float = 1.0,
        max_samples: int | None = None,
        confidence: float = 0.95,
    ) -> Estimator:
        """Construct an Estimator by dry-running then sampling a queue."""
        if not queue:
            raise ValueError("queue must contain at least one task")
        saga_runtime = getattr(pipeline, "_saga_runtime", None)
        if saga_runtime is None:
            raise ValueError("Estimator.from_dry_run requires a Pipeline builder")
        original_reasoners = dict(getattr(saga_runtime, "_reasoners", {}) or {})
        if not original_reasoners:
            raise ValueError("Estimator.from_dry_run requires a registered reasoner")
        stages = [
            stage
            for stage in getattr(pipeline, "_stages", [])
            if stage.saga is not None
        ]
        if not stages:
            raise ValueError("Estimator.from_dry_run requires at least one saga stage")

        pricing = _pipeline_pricing(pipeline)
        target_samples = max_samples or 8
        collector = CollectingReasoner()
        start = time.perf_counter()
        try:
            saga_runtime._reasoners = {collector.reasoner_id: collector}  # noqa: SLF001
            task_observations = asyncio.run(
                _collect_queue_observations(
                    saga_runtime=saga_runtime,
                    stages=stages,
                    queue=queue,
                    collector=collector,
                )
            )
        finally:
            saga_runtime._reasoners = original_reasoners  # noqa: SLF001
        pass_one_seconds = time.perf_counter() - start

        sample_indices = SamplingStrategy(target_samples=target_samples).pick_indices(
            task_observations
        )
        if not sample_indices:
            raise ValueError("dry-run pass produced no reason-step observations")

        sample_indices = _cap_sample_indices(
            sample_indices,
            task_observations,
            pricing=pricing,
            max_sample_cost_dollars=max_sample_cost_dollars,
            max_samples=max_samples,
        )
        sampled = [task_observations[i] for i in sample_indices]
        calibration = asyncio.run(
            _measure_samples(
                saga_runtime=saga_runtime,
                stages=stages,
                queue=queue,
                sample_indices=sample_indices,
                client=getattr(pipeline, "_bc", None),
            )
        )
        sample_cost = _sample_cost(calibration, pricing)
        return cls(
            calibration=calibration,
            pricing=pricing,
            confidence=confidence,
            default_n_tasks=len(queue),
            sample_cost_dollars=sample_cost,
            pass_one_seconds=pass_one_seconds,
            observed_tasks=len(queue),
            sampled_task_observations=sampled,
        )

    @classmethod
    def from_history(
        cls,
        client: Any,
        *,
        pricing: Pricing | None = None,
        confidence: float = 0.95,
    ) -> Estimator:
        """Construct an Estimator from existing Board token records."""
        records = client.token_records()
        calibration = _calibration_from_records(records)
        if pricing is None:
            try:
                raw_pricing = client.get_data("_pricing")
            except Exception:  # noqa: BLE001
                raw_pricing = None
            if isinstance(raw_pricing, dict):
                pricing = Pricing.from_dict(raw_pricing)
        return cls(
            calibration=calibration,
            pricing=pricing,
            confidence=confidence,
            default_n_tasks=calibration.n,
        )

    def project(
        self,
        n_tasks: int | None = None,
        queue: list[dict] | None = None,
    ) -> Projection:
        """Project total cost for ``n_tasks`` or ``len(queue)``."""
        target = len(queue) if queue is not None else (n_tasks or self.default_n_tasks)
        return Projector(confidence=self.confidence).project(
            self.calibration,
            target,
            self.pricing,
            self.sample_cost_dollars,
        )

    def format(self, *, projection: Projection | None = None) -> str:
        """Render a human-readable projection report."""
        projection = projection or self.project()
        lines: list[str] = []
        if self.observed_tasks is not None:
            lines.append("=== Estimator dry run ===")
            lines.append(
                "Pass 1 (input collection): "
                f"{self.observed_tasks} tasks scanned in {self.pass_one_seconds or 0:.1f}s"
            )
            sample_cost = (
                f"${projection.sample_cost_dollars:.2f}"
                if projection.sample_cost_dollars is not None
                else "token-only"
            )
            lines.append(
                f"Pass 2 (sampling): {projection.samples_used} tasks executed "
                f"(cost: {sample_cost})"
            )
            if self.sampled_task_observations:
                sizes = [
                    obs.total_input_chars for obs in self.sampled_task_observations
                ]
                lines.append("")
                lines.append("Sample distribution chosen by input-size span:")
                lines.append(f"  Smallest input:  {min(sizes):,} chars")
                lines.append(f"  Largest input:   {max(sizes):,} chars")
                middle = max(0, len(sizes) - 2)
                lines.append(f"  Middle samples:  {middle} across the distribution")
            lines.append("")

        lines.append(f"=== Projection for {projection.n_tasks} tasks ===")
        lines.append(f"Total tokens:  ~{_format_tokens(projection.total_tokens)}")
        lines.append(
            f"  Range ({projection.confidence:.0%} CI):  "
            f"{_format_tokens(projection.total_tokens_low)} - "
            f"{_format_tokens(projection.total_tokens_high)}"
        )
        lines.append("  Per-stage breakdown (mean):")
        if projection.by_stage:
            for stage, tokens in sorted(
                projection.by_stage.items(), key=lambda item: item[1], reverse=True
            ):
                pct = (
                    (tokens / projection.total_tokens * 100)
                    if projection.total_tokens
                    else 0
                )
                lines.append(
                    f"    {stage:<14} {_format_tokens(tokens):>8}  ({pct:.1f}%)"
                )
        else:
            lines.append("    <unknown>        (no stage records)")

        lines.append(
            f"  Stdev/task: {_format_tokens(int(projection.stdev_tokens_per_task))} "
            f"(CoV {projection.coefficient_of_variation:.2f})"
        )
        if projection.total_dollars is not None:
            lines.append("")
            lines.append(f"Total dollars: ~${projection.total_dollars:.2f}")
            lines.append(
                f"  Range ({projection.confidence:.0%} CI):  "
                f"${projection.total_dollars_low or 0:.2f} - "
                f"${projection.total_dollars_high or 0:.2f}"
            )

        if projection.variance_warning:
            lines.append("")
            lines.append("Variance warning: HIGH")
            lines.append(
                "   Coefficient of variation: "
                f"{projection.coefficient_of_variation:.2f} (>0.30 threshold)"
            )
            lines.append(
                "   Recommendation: run additional samples for a tighter estimate."
            )

        if projection.pricing_source:
            lines.append("")
            lines.append(f"Pricing source: {projection.pricing_source}")
            if projection.pricing_verify_url:
                lines.append(f"Verify current rates at {projection.pricing_verify_url}")
            if projection.sample_cost_dollars is not None:
                lines.append(
                    f"Sample run cost: ${projection.sample_cost_dollars:.2f} "
                    "(already spent; included in your billing)"
                )
        return "\n".join(lines)


async def _collect_queue_observations(
    *,
    saga_runtime: Any,
    stages: list[Any],
    queue: list[dict],
    collector: CollectingReasoner,
) -> list[_TaskObservation]:
    task_observations: list[_TaskObservation] = []
    for index, raw_task in enumerate(queue):
        task_id = str(raw_task.get("task_id") or f"dry-run-{index}")
        task = {**raw_task, "task_id": task_id}
        collector.current_task_index = index
        collector.current_task_id = task_id
        before = len(collector.observations)
        board = _MemoryBoard(task)
        for stage in stages:
            await _collect_stage(saga_runtime, stage, task, board)
        observations = collector.observations[before:]
        task_observations.append(
            _TaskObservation(
                task_index=index,
                task_id=task_id,
                total_input_chars=sum(obs.total_input_chars for obs in observations),
                observations=list(observations),
            )
        )
    return task_observations


async def _collect_stage(
    saga_runtime: Any,
    stage: Any,
    task: dict[str, Any],
    board: "_MemoryBoard",
) -> None:
    task["_board_fn"] = board.board_fn
    task["_board_state"] = board.full_state()
    ctx = RuntimeContext(
        stage=stage,
        task=task,
        context={},
        board_fn=board.board_fn,
        token_reporter=None,
        telemetry_sink=None,
    )
    state = SagaState(
        saga_name=stage.saga.name, pc=stage.saga.first_step(), started_at=""
    )
    await _walk_state(saga_runtime, stage.saga, state, ctx)


async def _walk_state(
    saga_runtime: Any, saga: Any, state: SagaState, ctx: RuntimeContext
) -> None:
    while state.pc is not None:
        if saga_runtime._is_gate_barrier(saga, state):  # noqa: SLF001
            return
        step = saga.find(state.pc)
        if step.kind is StepKind.GATE:
            on_true = step.payload["on_true"]
            on_false = step.payload["on_false"]
            for chosen in (on_true, on_false):
                branch_state = copy.deepcopy(state)
                branch_state.completed_steps[step.name] = {"chosen": chosen}
                branch_state.pc = chosen
                await _walk_state(saga_runtime, saga, branch_state, ctx)
            return
        try:
            outcome = await saga_runtime._dispatch_step(step, state, ctx, [])  # noqa: SLF001
        except Exception:  # noqa: BLE001
            return
        if outcome.__class__.__name__ == "_PCJump":
            continue
        state.completed_steps[step.name] = outcome
        state.pc = saga.next_after(step.name)


async def _measure_samples(
    *,
    saga_runtime: Any,
    stages: list[Any],
    queue: list[dict],
    sample_indices: list[int],
    client: Any | None = None,
) -> Calibration:
    calibrations: list[TaskCalibration] = []
    for sample_index in sample_indices:
        raw_task = queue[sample_index]
        task_id = str(raw_task.get("task_id") or f"sample-{sample_index}")
        if client is not None:
            task = _post_sample_task(client, raw_task, task_id)
            board_fn = client.request
        else:
            task = {**raw_task, "task_id": task_id}
            board = _MemoryBoard(task)
            board_fn = board.board_fn
        for stage in stages:
            if client is not None and stage.active_status:
                try:
                    task = client.update_task(task["task_id"], stage.active_status)
                except Exception:  # noqa: BLE001
                    pass
            ctx = RuntimeContext(
                stage=stage,
                task=task,
                context={},
                board_fn=board_fn,
                token_reporter=None,
                telemetry_sink=None,
            )
            await saga_runtime.run_stage(ctx)
            if client is not None:
                try:
                    task = client.get_task(task["task_id"])
                except Exception:  # noqa: BLE001
                    pass
        records = (
            client.token_records(task_id=task["task_id"])
            if client is not None
            else board.token_records(task_id)
        )
        calibrations.append(_task_calibration_from_records(task_id, records))
    return Calibration(calibrations)


def _post_sample_task(
    client: Any, raw_task: dict[str, Any], task_id: str
) -> dict[str, Any]:
    payload = dict(raw_task)
    task_type = str(payload.pop("task_type", payload.pop("type", "estimate_sample")))
    label = str(payload.pop("label", f"Estimator sample {task_id}"))
    payload.setdefault("task_id", task_id)
    return client.post_task(task_type, label, **payload)


class _MemoryBoard:
    def __init__(self, task: dict[str, Any]) -> None:
        self.task = task
        self.data: dict[str, Any] = {}

    def full_state(self) -> dict[str, Any]:
        return {"tasks": [self.task], "agents": [], "data": self.data}

    def token_records(self, task_id: str) -> list[dict[str, Any]]:
        prefix = f"_token_record:{task_id}:"
        return [
            value
            for key, value in self.data.items()
            if key.startswith(prefix) and isinstance(value, dict)
        ]

    def board_fn(self, intent: str, payload: dict) -> dict:
        if intent == "board.get_full_state":
            return self.full_state()
        if intent == "board.get_data":
            return {"value": self.data.get(payload["key"])}
        if intent == "board.put_data":
            self.data[payload["key"]] = payload.get("value")
            return {}
        if intent == "board.update_task":
            self.task.update(
                {
                    key: value
                    for key, value in payload.items()
                    if key not in {"task_id", "to_status", "notes_append"}
                }
            )
            if "to_status" in payload:
                self.task["status"] = payload["to_status"]
            if "notes_append" in payload:
                self.task.setdefault("notes", []).append(payload["notes_append"])
            return {"task": self.task}
        return {}


def _calibration_from_records(records: list[dict[str, Any]]) -> Calibration:
    by_task: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        task_id = str(record.get("task_id") or "<unknown>")
        by_task.setdefault(task_id, []).append(record)
    return Calibration(
        [
            _task_calibration_from_records(task_id, task_records)
            for task_id, task_records in sorted(by_task.items())
        ]
    )


def _task_calibration_from_records(
    task_id: str,
    records: list[dict[str, Any]],
) -> TaskCalibration:
    by_stage: dict[str, int] = {}
    by_step: dict[str, int] = {}
    by_model: dict[str, int] = {}
    total = 0
    for record in records:
        tokens = int(record.get("token_total") or 0)
        total += tokens
        stage = record.get("stage")
        step = record.get("step_name")
        model = (
            record.get("model") or record.get("model_id") or record.get("reasoner_id")
        )
        if stage:
            by_stage[str(stage)] = by_stage.get(str(stage), 0) + tokens
        if step:
            by_step[str(step)] = by_step.get(str(step), 0) + tokens
        if model:
            by_model[str(model)] = by_model.get(str(model), 0) + tokens
    return TaskCalibration(
        task_id=task_id,
        total_tokens=total,
        by_stage=by_stage,
        by_step=by_step,
        by_model=by_model,
    )


def _cap_sample_indices(
    sample_indices: list[int],
    task_observations: list[_TaskObservation],
    *,
    pricing: Pricing | None,
    max_sample_cost_dollars: float,
    max_samples: int | None,
) -> list[int]:
    capped = sample_indices[: max_samples or len(sample_indices)]
    if pricing is None:
        return capped
    # Use a conservative char-to-token approximation for the pre-sample cost cap.
    model = next(iter(pricing.models.keys()))
    selected: list[int] = []
    projected_cost = 0.0
    for idx in capped:
        estimated_tokens = max(1, int(task_observations[idx].total_input_chars / 4))
        cost = pricing.cost_for_tokens(model, estimated_tokens)
        if selected and projected_cost + cost > max_sample_cost_dollars:
            break
        selected.append(idx)
        projected_cost += cost
    return selected or capped[:1]


def _sample_cost(calibration: Calibration, pricing: Pricing | None) -> float | None:
    if pricing is None:
        return None
    total = 0.0
    for task in calibration.tasks:
        model = (
            max(task.by_model.items(), key=lambda item: item[1])[0]
            if task.by_model
            else "default"
        )
        total += pricing.cost_for_tokens(model, task.total_tokens)
    return total


def _pipeline_pricing(pipeline: Any) -> Pricing | None:
    board = getattr(pipeline, "_board", None)
    runtime_pricing = getattr(board, "_pricing", None)
    if isinstance(runtime_pricing, Pricing):
        return runtime_pricing
    return None


def _format_tokens(n: int) -> str:
    if n < 1000:
        return f"{n:,}"
    if n < 10_000:
        return f"{n / 1000:.1f}K"
    if n < 1_000_000:
        return f"{round(n / 1000)}K"
    return f"{n / 1_000_000:.1f}M"
