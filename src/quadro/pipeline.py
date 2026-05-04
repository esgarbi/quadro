"""
Framework-agnostic pipeline infrastructure for Quadro.

Provides the substrate classes every Quadro pipeline builds on:

  StageSpec                  Describes one pipeline stage.
  ToolDescriptor             Framework-agnostic chief tool descriptor.
  generate_tool_descriptors  Derive tool descriptors from a lifecycle graph.
  BuiltPipeline              The runnable result of ``Pipeline.build()``.
  Pipeline                   Substrate builder. Compose LLM-framework
                             integrations via ``.reasoner(...)`` and
                             ``.with_framework_runtime(...)``; no
                             framework-specific subclass is required.

Every ``Pipeline`` instance auto-registers a ``QuadroSagaRuntime`` so
saga reasoners have a registration target and saga-only pipelines work
without an LLM-backed chief. LLM-framework adapters (for example
``quadro_maf.MafChiefRuntime`` and ``quadro_langchain.LangChainChiefRuntime``)
plug in through the same ``FrameworkRuntime`` protocol.

The entire module is zero-dependency (stdlib + quadro core only).
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .dispatch import (
    acknowledge_task,
    dispatch_batch,
    get_acknowledged,
)

if TYPE_CHECKING:
    from .runtime_plugins.base import FrameworkRuntime

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════════
#  Public data types
# ═══════════════════════════════════════════════════════════════════════════════


@dataclass
class StageSpec:
    """Describes one pipeline stage.

    Framework-specific adapters may extend this with additional fields
    (e.g. ``prompt``, ``output_schema``) via composition or subclassing.
    """

    capability: str
    execute_fn: Callable | None = None
    active_status: str | None = None
    success_status: str | None = None
    failure_status: str | None = None
    max_working_time: float | None = None
    tool_name: str | None = None
    workflow: Any | None = None
    graph: Any | None = None
    supervisor: Any | None = None
    saga: Any | None = None

    def __post_init__(self) -> None:
        if self.active_status is None:
            self.active_status = self.capability.replace(" ", "_")


@dataclass(frozen=True)
class ToolDescriptor:
    """Framework-agnostic description of a chief tool.

    Each adapter converts these into framework-specific tool objects
    (e.g. MAF ``@tool``, LangChain ``Tool``, OpenAI function tools).
    """

    name: str
    description: str
    fn: Callable[..., str]


# ═══════════════════════════════════════════════════════════════════════════════
#  Lifecycle graph helpers
# ═══════════════════════════════════════════════════════════════════════════════


def _extract_raw_transitions(lifecycle: Any) -> set[tuple[str, str]]:
    """Strip the auto-expanded HUMAN_REVIEW/ON_HOLD edges from a Lifecycle."""
    return {
        (f, t)
        for f, t in lifecycle.transitions
        if t not in {"HUMAN_REVIEW", "ON_HOLD", "FAILED"}
        and f not in {"HUMAN_REVIEW", "ON_HOLD"}
    }


def _predecessors_for(
    transitions: set[tuple[str, str]],
    target: str,
) -> set[str]:
    """Return all states that have a direct transition INTO *target*."""
    return {f for f, t in transitions if t == target}


# ═══════════════════════════════════════════════════════════════════════════════
#  generate_tool_descriptors()
# ═══════════════════════════════════════════════════════════════════════════════


def generate_tool_descriptors(
    lifecycle: Any,
    *,
    stage_map: Mapping[str, str | tuple[str, str | None]],
    board_fn: Callable[[str, dict], dict],
    network: Any,
    worker_registry: dict[str, list[tuple[str, str]]],
) -> list[ToolDescriptor]:
    """Derive chief tool descriptors from a lifecycle transition graph.

    For each ``active_status`` in *stage_map*, generates a
    ``ToolDescriptor`` whose callable dispatches all tasks from
    predecessor states to that active status.  Always appends a
    ``discard_task`` descriptor.

    Parameters
    ----------
    lifecycle:
        A ``Lifecycle`` object.
    stage_map:
        Maps ``active_status -> capability`` or
        ``active_status -> (capability, tool_name)``. When *tool_name* is not
        provided, the generated tool name defaults to ``advance_to_{active_status}``.
    board_fn:
        Callable that sends an intent to the board.
    network:
        The A2A network instance.
    worker_registry:
        ``{capability: [(agent_id, url), ...]}``

    Returns
    -------
    A list of ``ToolDescriptor`` instances (undecorated).
    """
    raw = _extract_raw_transitions(lifecycle)
    descriptors: list[ToolDescriptor] = []

    for active_status, stage_info in stage_map.items():
        if isinstance(stage_info, tuple):
            capability, configured_tool_name = stage_info
        else:
            capability = stage_info
            configured_tool_name = None

        preds = _predecessors_for(raw, active_status)
        if not preds:
            logger.warning(
                "generate_tool_descriptors: no predecessors for %r — skipping",
                active_status,
            )
            continue

        pred_label = ", ".join(sorted(preds))
        tool_name = configured_tool_name or f"advance_to_{active_status}"
        description = (
            f"Dispatch ALL tasks in [{pred_label}] to {active_status}. "
            f"Pass any task_id — the tool handles all eligible tasks. "
            f"Returns immediately — workers run in the background."
        )

        def _make_dispatch_fn(
            _preds: set[str] = preds,
            _target: str = active_status,
            _cap: str = capability,
        ) -> Callable:
            def dispatch_fn(task_id: str) -> str:
                dispatched, skipped = dispatch_batch(
                    board_fn,
                    network,
                    worker_registry,
                    _preds,
                    _target,
                    _cap,
                )
                if not dispatched and not skipped:
                    return f"No tasks ready for {_target}."
                msg = f"Dispatched {len(dispatched)} to {_target}: {', '.join(dispatched)}"
                if skipped:
                    msg += (
                        f" | {len(skipped)} skipped (no idle {_cap} worker): "
                        f"{', '.join(skipped)}"
                    )
                return msg

            return dispatch_fn

        descriptors.append(
            ToolDescriptor(
                name=tool_name,
                description=description,
                fn=_make_dispatch_fn(),
            )
        )

    def _discard_task(task_id: str) -> str:
        state = board_fn("board.get_full_state", {})
        task = next(
            (t for t in state.get("tasks", []) if t["task_id"] == task_id), None
        )
        if not task:
            return f"Task {task_id[:8]} not found."
        acked = get_acknowledged(board_fn)
        if task_id in acked:
            return f"Task {task_id[:8]} already acknowledged."
        acknowledge_task(board_fn, task_id)
        return f"Task {task_id[:8]} acknowledged."

    descriptors.append(
        ToolDescriptor(
            name="discard_task",
            description=(
                "Acknowledge a HUMAN_REVIEW or FAILED task so it stops appearing "
                "in the board summary. Call once per failed task. Provide the "
                "full task_id."
            ),
            fn=_discard_task,
        )
    )

    return descriptors


# ═══════════════════════════════════════════════════════════════════════════════
#  BuiltPipeline
# ═══════════════════════════════════════════════════════════════════════════════


class BuiltPipeline:
    """The runnable result of ``Pipeline.build()``.

    Holds the assembled Quadro components (board, worker pool, chief,
    ombudsman). Run the pipeline through a :class:`~quadro.QuadroRuntime`
    — the previous ``BuiltPipeline.run(done_when=..., max_cycles=...)``
    shortcut was removed when the Sponsor/Lease layer replaced those
    knobs. See ``docs/design/sponsor.md``.
    """

    def __init__(
        self,
        board: Any,
        pool: Any,
        chief: Any,
        ombudsman: Any,
    ) -> None:
        self.board = board
        self.pool = pool
        self.chief = chief
        self.ombudsman = ombudsman


# ═══════════════════════════════════════════════════════════════════════════════
#  Pipeline base class
# ═══════════════════════════════════════════════════════════════════════════════


class Pipeline:
    """Substrate pipeline builder.

    ``Pipeline`` assembles the Quadro components (board, worker pool,
    chief, ombudsman) around a lifecycle graph and a set of stages. LLM
    integration is explicit composition rather than inheritance: register
    a reasoner on the saga runtime with :meth:`reasoner` and a
    chief-tooling runtime with :meth:`with_framework_runtime`.

    Every ``Pipeline`` auto-registers a ``QuadroSagaRuntime`` in
    ``__init__``:

    * Saga steps find their registered reasoner there.
    * Saga-only pipelines (no LLM-backed chief) use the saga runtime's
      deterministic chief mode — the same "walk the lifecycle graph and
      dispatch eligible tasks forward" pattern the ``core`` examples
      shipped in milestone D.

    Usage with an LLM-framework adapter (see ``quadro_maf`` /
    ``quadro_langchain`` docs for the framework client setup)::

        from quadro import Pipeline
        from quadro_maf import MafReasoner, MafChiefRuntime

        pipeline = (
            Pipeline(board)
            .reasoner(MafReasoner(client_factory=client_factory))
            .with_framework_runtime(
                MafChiefRuntime(client_factory=client_factory)
            )
            .workers(4).capacity(8).wakes("a2a://chief")
            .stage("validation", active_status="validating", ...)
            .chief(goal_key="my_goal")
            .build()
        )

    Usage without any LLM framework (saga-only deterministic chief)::

        pipeline = (
            Pipeline(board)
            .stage("compose", saga=my_saga, active_status="composing")
            .build()
        )
    """

    def __init__(self, board: Any) -> None:
        from .runtime_plugins.saga import QuadroSagaRuntime

        self._board = board
        self._bc = board.client()
        self._workers_per_cap: int = 1
        self._capacity_override: int | None = None
        self._chief_url: str = "a2a://chief"
        self._stages: list[StageSpec] = []
        self._chief_prompt: str | Path | None = None
        self._chief_goal_key: str | None = None
        self._chief_extra_tools: list | None = None
        self._chief_name_prefix: str = "chief"
        self._framework_runtimes: list[FrameworkRuntime] = []
        self._runtime_token_reporter: Callable[[int], None] | None = None
        self._runtime_telemetry_sink: Callable[[dict[str, Any]], None] | None = None

        # Auto-register the saga runtime so .reasoner(...) always has a
        # target and saga-only pipelines can drive their deterministic
        # chief without any additional wiring. Kept first in the
        # registration list so that ``_primary_runtime`` can identify
        # it by object identity and fall through to LLM-backed runtimes
        # registered afterwards.
        self._saga_runtime = QuadroSagaRuntime()
        self._framework_runtimes.append(self._saga_runtime)

    def workers(self, n: int) -> Pipeline:
        """Set number of worker agents per capability."""
        self._workers_per_cap = n
        return self

    def capacity(self, n: int) -> Pipeline:
        """Set total pipeline capacity (max active tasks on board)."""
        self._capacity_override = n
        return self

    def wakes(self, chief_url: str) -> Pipeline:
        """Set the chief's A2A URL for worker completion wakeups."""
        self._chief_url = chief_url
        return self

    def stage(
        self,
        capability: str,
        *,
        execute_fn: Callable | None = None,
        active_status: str | None = None,
        success_status: str | None = None,
        failure_status: str | None = None,
        max_working_time: float | None = None,
        tool_name: str | None = None,
        **kwargs: Any,
    ) -> Pipeline:
        """Add a pipeline stage.

        Subclasses may accept additional keyword arguments (e.g.
        ``prompt``, ``output_schema``) via ``**kwargs``.
        """
        self._stages.append(
            self._make_stage_spec(
                capability,
                execute_fn=execute_fn,
                active_status=active_status,
                success_status=success_status,
                failure_status=failure_status,
                max_working_time=max_working_time,
                tool_name=tool_name,
                **kwargs,
            )
        )
        return self

    def _make_stage_spec(self, capability: str, **kwargs: Any) -> StageSpec:
        """Create a StageSpec. Override in subclasses to use extended specs."""
        return StageSpec(
            capability,
            **{k: v for k, v in kwargs.items() if k in StageSpec.__dataclass_fields__},
        )

    def chief(
        self,
        *,
        prompt: str | Path | None = None,
        goal_key: str | None = None,
        extra_tools: list | None = None,
        name_prefix: str = "chief",
    ) -> Pipeline:
        """Configure the chief agent."""
        self._chief_prompt = prompt
        self._chief_goal_key = goal_key
        self._chief_extra_tools = extra_tools
        self._chief_name_prefix = name_prefix
        return self

    def with_framework_runtime(self, runtime: "FrameworkRuntime") -> Pipeline:
        """Register a framework runtime plugin for native delegation paths."""
        self._framework_runtimes.append(runtime)
        return self

    def reasoner(self, reasoner: Any) -> Pipeline:
        """Register a reasoner on this pipeline's saga runtime.

        A ``QuadroSagaRuntime`` is auto-registered in :meth:`__init__`;
        this method adds a reasoner to it so saga ``reason`` steps have
        something to dispatch through. Multiple reasoners can be
        registered (the first becomes the fallback for steps without
        ``via=``; subsequent reasoners are reachable via
        ``.reason(via="<reasoner_id>")``).

        The reasoner must implement the structural protocol declared in
        ``quadro.saga.reasoner.Reasoner`` — a ``reasoner_id`` class
        attribute and an async ``reason()`` method.
        """
        self._saga_runtime.register_reasoner(reasoner)
        return self

    def runtime_observability(
        self,
        *,
        token_reporter: Callable[[int], None] | None = None,
        telemetry_sink: Callable[[dict[str, Any]], None] | None = None,
    ) -> Pipeline:
        """Configure telemetry/token sinks used by runtime plugins."""
        self._runtime_token_reporter = token_reporter
        self._runtime_telemetry_sink = telemetry_sink
        return self

    def _validate_stages(self) -> None:
        """Validate stage configuration before wiring worker/chief agents."""
        for spec in self._stages:
            output_schema = getattr(spec, "output_schema", None)
            if output_schema is not None and not spec.failure_status:
                raise ValueError(
                    f"Stage {spec.capability!r} config is unsafe: output_schema "
                    "requires failure_status so schema-invalid outputs cannot be "
                    "marked successful."
                )

    def _runtime_for_stage(self, spec: StageSpec) -> "FrameworkRuntime | None":
        """Return the first runtime plugin that can execute *spec*."""
        for runtime in self._framework_runtimes:
            try:
                if runtime.can_handle(spec):
                    return runtime
            except Exception:  # noqa: BLE001
                continue
        return None

    def _primary_runtime(self) -> "FrameworkRuntime | None":
        """Return the first non-saga registered framework runtime, if any.

        The saga runtime is auto-registered for every pipeline (so
        reasoners have a registration target), but it is not a primary
        runtime for chief tooling — its ``run_chief_turn`` is the
        deterministic chief fallback, used only when no LLM-driven
        runtime is registered. When no LLM-driven runtime is present,
        this falls through to the saga runtime so saga-only pipelines
        still get a chief.
        """
        for runtime in self._framework_runtimes:
            if runtime is not self._saga_runtime:
                return runtime
        return self._saga_runtime

    def _make_runtime_execute_fn(
        self,
        spec: StageSpec,
        runtime: "FrameworkRuntime",
    ) -> Callable:
        """Generate execute_fn that delegates one stage turn to a runtime plugin."""

        async def _execute(context: dict, board_fn: Callable[[str, dict], dict]) -> Any:
            from .runtime_plugins.base import RuntimeContext
            from .runtime_plugins.telemetry import emit_runtime_event

            task = context["payload"]["task"]
            result = await runtime.run_stage(
                RuntimeContext(
                    stage=spec,
                    task=task,
                    context=context,
                    board_fn=board_fn,
                    token_reporter=self._runtime_token_reporter,
                    telemetry_sink=self._runtime_telemetry_sink,
                )
            )

            if result.token_total > 0 and self._runtime_token_reporter is not None:
                self._runtime_token_reporter(result.token_total)

            if result.telemetry and self._runtime_telemetry_sink is not None:
                for event in result.telemetry:
                    emit_runtime_event(self._runtime_telemetry_sink, event)

            # Only transition the task here if an explicit target was
            # requested — either by the runtime (``result.status``) or by
            # the stage declaration (``spec.success_status``). When both
            # are absent (the saga pattern, where the saga's last step
            # performs its own ``board.update_task`` with the right
            # destination status), the task has already been moved off
            # the ``active_status`` and the lifecycle will correctly
            # reject a self-transition back. In that case trust the
            # runtime's in-step persistence and skip the post-stage
            # update entirely.
            target = result.status or spec.success_status
            if target is None:
                return result.output

            payload = {
                "task_id": task["task_id"],
                "to_status": target,
            }
            if result.output is not None:
                payload["output"] = result.output
            if result.notes_append:
                payload["notes_append"] = result.notes_append
            if result.update_fields:
                payload.update(result.update_fields)
            board_fn("board.update_task", payload)
            return result.output

        return _execute

    # ── Build ─────────────────────────────────────────────────────────────────

    def build(self) -> BuiltPipeline:
        """Assemble and return the runnable pipeline."""
        self._validate_stages()

        from .agents.chief import ChiefAgent
        from .agents.pool import WorkerPool

        bc = self._bc

        # ── WorkerPool ────────────────────────────────────────────────────────
        pool_builder = (
            WorkerPool(bc).workers(self._workers_per_cap).wakes(self._chief_url)
        )
        if self._capacity_override is not None:
            pool_builder = pool_builder.capacity(self._capacity_override)

        stage_map: dict[str, tuple[str, str | None]] = {}

        for spec in self._stages:
            runtime = self._runtime_for_stage(spec) if spec.execute_fn is None else None
            if spec.execute_fn is not None:
                fn = spec.execute_fn
            elif runtime is not None:
                fn = self._make_runtime_execute_fn(spec, runtime)
            else:
                raise ValueError(
                    f"Pipeline stage {spec.capability!r} has no execute_fn "
                    f"and no registered FrameworkRuntime claims it. Either "
                    f"pass ``execute_fn=...`` to .stage(), register a "
                    f"runtime that handles one of the native entrypoints "
                    f"(workflow=, graph=, supervisor=, saga=) via "
                    f".with_framework_runtime(...), or use a helper such "
                    f"as ``quadro_maf.make_auto_execute_fn`` to build "
                    f"the execute_fn explicitly."
                )
            pool_builder = pool_builder.add(
                spec.capability,
                fn,
                active_status=spec.active_status,
                max_working_time=spec.max_working_time,
            )
            stage_map[spec.active_status] = (spec.capability, spec.tool_name)

        pool = pool_builder.build()

        # ── Chief policy ──────────────────────────────────────────────────────
        network = bc.network

        chief_instructions: str | None = None
        if isinstance(self._chief_prompt, Path):
            chief_instructions = self._chief_prompt.read_text()
        elif isinstance(self._chief_prompt, str):
            chief_instructions = self._chief_prompt

        lifecycle = None
        profiles = getattr(self._board, "_custom_profiles", None)
        if profiles:
            for _name, lc in profiles.items():
                lifecycle = lc
                break

        goal_key = self._chief_goal_key
        extra_tools_raw = self._chief_extra_tools
        first_active = self._stages[0].active_status if self._stages else None
        first_cap = self._stages[0].capability if self._stages else None

        # ── Register deterministic-chief context on opt-in runtimes ───────────
        # Any framework runtime that declares ``register_chief_context``
        # (today only ``QuadroSagaRuntime``) gets the lifecycle, stage
        # map, network, worker registry, and a board-fn shim handed to
        # it once at build time. The saga runtime uses these to
        # dispatch tasks forward deterministically in ``run_chief_turn``
        # when no LLM-backed chief is available — making a saga-only
        # pipeline (base ``Pipeline`` + ``QuadroSagaRuntime``) driveable
        # without an LLM. LLM-backed pipelines (MAF, LangChain) register
        # their own primary runtime; ``_primary_runtime()`` returns it
        # first and the saga runtime's deterministic mode is not
        # reached. The ``hasattr`` check keeps this opt-in per runtime.
        def _chief_context_board_fn(intent: str, p: dict) -> dict:
            return bc.request(intent, p)

        for runtime in self._framework_runtimes:
            register_ctx = getattr(runtime, "register_chief_context", None)
            if callable(register_ctx):
                register_ctx(
                    lifecycle=lifecycle,
                    stage_map=stage_map,
                    network=network,
                    worker_registry=pool.registry,
                    board_fn=_chief_context_board_fn,
                )

        async def _chief_policy(chief_context: dict) -> None:
            def board_fn(intent: str, p: dict) -> dict:
                return bc.request(intent, p)

            if first_active and first_cap:
                dispatch_batch(
                    board_fn,
                    network,
                    pool.registry,
                    "UNASSIGNED",
                    first_active,
                    first_cap,
                )

            # ``_primary_runtime`` always returns a runtime — either an
            # LLM-backed one (MAF, LangChain, custom) registered
            # afterward, or the auto-registered saga runtime as the
            # deterministic-chief fallback. So tool decoration and
            # chief-turn execution always have a concrete target.
            runtime = self._primary_runtime()
            assert runtime is not None  # invariant: saga runtime is always registered

            if lifecycle is not None:
                descriptors = generate_tool_descriptors(
                    lifecycle,
                    stage_map=stage_map,
                    board_fn=board_fn,
                    network=network,
                    worker_registry=pool.registry,
                )
                tools = runtime.decorate_tools(descriptors)
            else:
                tools = []

            if extra_tools_raw:
                tools.extend(extra_tools_raw)

            board_summary = bc.snapshot(tools, goal_key=goal_key)
            if board_summary is None:
                logger.info("Chief: nothing actionable — sleeping")
                return

            instructions = chief_instructions or (
                "You are the chief coordinator. Review the board state and "
                "use your tools to advance tasks through the pipeline."
            )

            try:
                output = await runtime.run_chief_turn(
                    board_summary,
                    instructions,
                    tools,
                    chief_name_prefix=self._chief_name_prefix,
                )
                if output:
                    logger.info("Chief: %s", output[:200])
                else:
                    logger.warning("Chief produced no output for current board snapshot")
            except Exception as exc:
                logger.error("Chief policy error: %s", exc)

        chief = ChiefAgent.builder(bc).at(self._chief_url).policy(_chief_policy).build()

        # ── Ombudsman ─────────────────────────────────────────────────────────
        wd = pool.ombudsman()

        return BuiltPipeline(
            board=self._board,
            pool=pool,
            chief=chief,
            ombudsman=wd,
        )
