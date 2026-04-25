"""
Framework-agnostic pipeline infrastructure for Quadro.

Provides the base classes that any agent-framework adapter can subclass
to build a declarative, governed multi-agent pipeline:

  StageSpec                  Describes one pipeline stage.
  ToolDescriptor             Framework-agnostic chief tool descriptor.
  generate_tool_descriptors  Derive tool descriptors from a lifecycle graph.
  BuiltPipeline              The runnable result of ``Pipeline.build()``.
  Pipeline                   Base builder -- subclass and override three hooks.

The entire module is zero-dependency (stdlib + quadro core only).
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .dispatch import (
    acknowledge_task,
    dispatch_batch,
    get_acknowledged,
)

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

        descriptors.append(ToolDescriptor(
            name=tool_name,
            description=description,
            fn=_make_dispatch_fn(),
        ))

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

    descriptors.append(ToolDescriptor(
        name="discard_task",
        description=(
            "Acknowledge a HUMAN_REVIEW or FAILED task so it stops appearing "
            "in the board summary. Call once per failed task. Provide the "
            "full task_id."
        ),
        fn=_discard_task,
    ))

    return descriptors


# ═══════════════════════════════════════════════════════════════════════════════
#  BuiltPipeline
# ═══════════════════════════════════════════════════════════════════════════════


class BuiltPipeline:
    """The runnable result of ``Pipeline.build()``.

    Holds the assembled Quadro components and exposes a single
    ``.run()`` method that drives the ``RunLoop`` to completion.
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

    def run(
        self,
        *,
        done_when: Callable[[dict], bool],
        on_cycle: Callable[[dict, int], None] | None = None,
        poll_every: float = 3.0,
        ombudsman_every: float = 30.0,
        max_cycles: int = 1000,
    ) -> dict:
        """Run the pipeline to completion via Quadro's ``RunLoop``."""
        from .runner import RunLoop

        builder = (
            RunLoop(self.board, self.chief)
            .done_when(done_when)
            .ombudsman(self.ombudsman)
            .poll_every(poll_every)
            .ombudsman_every(ombudsman_every)
            .max_cycles(max_cycles)
        )
        if on_cycle:
            builder = builder.on_cycle(on_cycle)
        return builder.run()


# ═══════════════════════════════════════════════════════════════════════════════
#  Pipeline base class
# ═══════════════════════════════════════════════════════════════════════════════


class Pipeline:
    """Framework-agnostic pipeline builder.

    Subclass and override three hooks to integrate any LLM framework:

    - ``_decorate_tools(descriptors)`` — wrap ``ToolDescriptor`` list into
      framework-specific tool objects.
    - ``_run_chief_llm_turn(board_summary, instructions, tools)`` — execute
      one chief LLM decision turn.
    - ``_make_auto_execute_fn(spec)`` — generate an ``execute_fn`` for
      stages that don't provide one explicitly.

    Usage (subclass)::

        class MyPipeline(Pipeline):
            def _decorate_tools(self, descriptors): ...
            async def _run_chief_llm_turn(self, summary, instructions, tools): ...
            def _make_auto_execute_fn(self, spec): ...

        pipeline = (
            MyPipeline(board)
            .workers(4).capacity(8).wakes("a2a://chief")
            .stage("validation", active_status="validating", ...)
            .chief(goal_key="my_goal")
            .build()
        )
    """

    def __init__(self, board: Any) -> None:
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
        return StageSpec(capability, **{
            k: v for k, v in kwargs.items()
            if k in StageSpec.__dataclass_fields__
        })

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

    # ── Abstract hooks ────────────────────────────────────────────────────────

    def _decorate_tools(self, descriptors: list[ToolDescriptor]) -> list:
        """Convert framework-agnostic ToolDescriptors into framework-specific tools.

        Must be overridden by subclasses.
        """
        raise NotImplementedError(
            f"{type(self).__name__} must implement _decorate_tools()"
        )

    async def _run_chief_llm_turn(
        self,
        board_summary: str,
        instructions: str,
        tools: list,
    ) -> str | None:
        """Execute one chief LLM decision turn.

        Must be overridden by subclasses.
        """
        raise NotImplementedError(
            f"{type(self).__name__} must implement _run_chief_llm_turn()"
        )

    def _make_auto_execute_fn(self, spec: StageSpec) -> Callable:
        """Generate an execute_fn for a stage with no explicit one.

        Must be overridden by subclasses that support auto-generated workers.
        """
        raise NotImplementedError(
            f"{type(self).__name__} must implement _make_auto_execute_fn() "
            f"to auto-generate workers for stage {spec.capability!r}"
        )

    # ── Build ─────────────────────────────────────────────────────────────────

    def build(self) -> BuiltPipeline:
        """Assemble and return the runnable pipeline."""
        from .agents.chief import ChiefAgent
        from .agents.pool import WorkerPool

        bc = self._bc

        # ── WorkerPool ────────────────────────────────────────────────────────
        pool_builder = WorkerPool(bc).workers(self._workers_per_cap).wakes(self._chief_url)
        if self._capacity_override is not None:
            pool_builder = pool_builder.capacity(self._capacity_override)

        stage_map: dict[str, tuple[str, str | None]] = {}

        for spec in self._stages:
            fn = spec.execute_fn or self._make_auto_execute_fn(spec)
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
        chief_name = self._chief_name_prefix
        first_active = self._stages[0].active_status if self._stages else None
        first_cap = self._stages[0].capability if self._stages else None

        async def _chief_policy(chief_context: dict) -> None:
            def board_fn(intent: str, p: dict) -> dict:
                return bc.request(intent, p)

            if first_active and first_cap:
                dispatch_batch(
                    board_fn, network, pool.registry,
                    "UNASSIGNED", first_active, first_cap,
                )

            if lifecycle is not None:
                descriptors = generate_tool_descriptors(
                    lifecycle,
                    stage_map=stage_map,
                    board_fn=board_fn,
                    network=network,
                    worker_registry=pool.registry,
                )
                tools = self._decorate_tools(descriptors)
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
                output = await self._run_chief_llm_turn(
                    board_summary, instructions, tools,
                )
                if output:
                    logger.info("Chief: %s", output[:200])
            except Exception as exc:
                logger.error("Chief policy error: %s", exc)

        chief = (
            ChiefAgent.builder(bc)
            .at(self._chief_url)
            .policy(_chief_policy)
            .build()
        )

        # ── Ombudsman ─────────────────────────────────────────────────────────
        wd = pool.ombudsman()

        return BuiltPipeline(
            board=self._board,
            pool=pool,
            chief=chief,
            ombudsman=wd,
        )
