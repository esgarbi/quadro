"""
Quadro-native saga runtime plugin.

Implements the ``FrameworkRuntime`` protocol and dispatches stages
whose ``StageSpec.saga`` is set. Substrate-resident, framework-free:
``Pipeline.__init__`` auto-registers this runtime on every pipeline so
saga reasoners have a registration target and saga-only pipelines
drive end-to-end without any LLM framework. LLM-framework adapters
(for example ``quadro_maf.MafChiefRuntime`` and
``quadro_langchain.LangChainChiefRuntime``) plug in alongside via
``Pipeline.with_framework_runtime(...)``.

Milestone A scope: dispatches ``StepKind.DETERMINISTIC`` steps only.
Persists ``SagaState`` to the Board's data store between step
completions under the key ``_saga:{task_id}``. Re-entrance is the
load-bearing property — calling ``run_stage`` for a task that already
has saga state resumes from the persisted ``pc`` rather than starting
over.

Milestone B adds ``StepKind.REASON`` dispatch. A reason step looks up
the registered ``Reasoner`` (via ``register_reasoner``), resolves the
step's prompt (from disk if a ``Path``), builds the user message by
calling the configured lambda, and awaits the reasoner's ``reason()``
method. The reasoner is the seam between the saga runtime and any
LLM framework — concrete implementations (``MafReasoner``,
``LangChainReasoner``) live in ``quadro.integrations``.

Milestone C adds dispatch paths for ``gate``, ``guard``, ``expect``,
``evidence``, and ``stamp``, plus the first two cross-cutting step
modifiers (``.retry()`` and ``.deadline()``). The run loop gains
first-class handling for guard/expect failures and deadline timeouts,
translating each into a dedicated telemetry event type and a
``terminal_reason`` that names the offending step. The reason-step
dispatch also bumps ``_tokens:{task_id}.by_stage[stage]`` after every
reasoner call so saga-driven stages contribute to the same per-stage
token breakdown that workflow-driven stages have always populated
(closes observation #2 from milestone B's run note).

Milestone D adds the compensation-rollback walker. When a step raises
mid-saga and the saga has at least one registered compensation, the
runtime walks completed steps in reverse order and invokes each
registered ``undo`` callable. Per-compensation ``on_failure`` metadata
governs whether a compensation-that-itself-raises aborts the walk
(``"halt"``) or is logged and the walker continues (``"continue"``,
the default — Option 2 in the brief's design discussion). Every
attempt is recorded in ``state.compensations_run`` so resume after a
mid-rollback worker crash invokes only the un-compensated
compensations. Four new telemetry event types flow through the
existing envelope: ``saga.compensation_start``,
``saga.compensation_end``, ``saga.compensation_failed``, and
``saga.rollback_complete``.

Milestone G adds per-step ``Reasoner`` selection. A reason step
authored as ``.reason(..., via="<reasoner_id>")`` carries that id
through to ``_run_reason``, which looks the reasoner up on the
registry and raises a clear ``RuntimeError`` naming the missing id
(and the available ids) when the lookup fails. ``via`` absent or
``None`` preserves the milestone-B fallback of the first registered
reasoner, so existing sagas are unaffected. Every reason-step
dispatch also records the reasoner that ran it on two audit
surfaces: per-saga via ``state.reasoners_by_step`` (round-trips
through ``SagaState`` alongside ``compensations_run``) and
cross-saga via ``_record_reasoner_on_task`` writing
``_tokens:{task_id}.reasoners_by_step`` (accumulates across every
saga for the task, so an external reader can reconstruct the full
polyglot routing decision for a task from one board key). The
milestone also hardens ``_bump_stage_tokens`` into a
read-modify-write against ``_tokens:{task_id}`` so it no longer
clobbers the sibling ``reasoners_by_step`` field that now shares
the same board key.

Milestone E adds ``parallel`` dispatch: branch-local mini-sagas run
concurrently and join with ``all``, ``any``, or ``n_of_m`` semantics.
Subsequent milestones may add dispatch paths for ``fork`` / ``join`` (F).
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from ..saga.saga import BuiltSaga
from ..saga.state import SagaState
from ..saga.steps import BuiltBranch, SagaContext, Step, StepKind
from .base import FrameworkRuntime, RuntimeContext, StageRunResult
from .telemetry import build_runtime_event

logger = logging.getLogger(__name__)


# ── Sentinels reserved for later milestones ──────────────────────────────────
# Defined here so the dispatch loop's shape does not change as later step
# kinds are added. Milestone A's ``_dispatch_step`` never returns these
# values, but the loop checks for them so milestone F can wire suspension
# and gates without restructuring the runner.

@dataclass(frozen=True)
class _Suspend:
    """Sentinel returned by step handlers when the saga must suspend."""
    status: str          # WAITING_FORK / WAITING_HUMAN — milestone F
    waiting_for: str


@dataclass(frozen=True)
class _PCJump:
    """Sentinel returned by step handlers that have already advanced ``pc``."""
    pass


# ── Internal signalling exceptions (milestone C) ─────────────────────────────
# Raised from inside a step handler to unwind back to the run loop with
# enough structure to emit the right telemetry event and build the
# correct ``terminal_reason``. Kept private to the module — caller code
# never imports them.


class _GuardFailed(Exception):
    """Raised by ``_run_guard`` when a guard's check returns False."""

    def __init__(self, step_name: str) -> None:
        super().__init__(f"guard_failed:{step_name}")
        self.step_name = step_name


class _ExpectFailed(Exception):
    """Raised by ``_run_expect`` when an expect's invariant returns False."""

    def __init__(self, step_name: str) -> None:
        super().__init__(f"expect_failed:{step_name}")
        self.step_name = step_name


class _DeadlineExceeded(Exception):
    """Raised by ``_apply_modifiers`` when a step's ``.deadline()`` trips."""

    def __init__(self, step_name: str) -> None:
        super().__init__(f"deadline_exceeded:{step_name}")
        self.step_name = step_name


class _ParallelFailed(Exception):
    """Raised when a parallel step cannot satisfy its join mode."""

    def __init__(self, parallel_name: str, failures: dict[str, BaseException]) -> None:
        branch_names = ", ".join(sorted(failures)) or "<none>"
        super().__init__(f"parallel step {parallel_name!r} failed branches: {branch_names}")
        self.parallel_name = parallel_name
        self.failures = failures


def _backoff_seconds(attempt: int, backoff: str) -> float:
    """Return the sleep duration for the configured retry backoff."""
    if backoff == "exponential":
        return float(min(2 ** (max(0, attempt - 1)), 30))
    return 0.0


def _backoff_sleep(attempt: int, backoff: str) -> None:
    """Apply the configured between-retries delay.

    ``fixed`` is a no-op (retries fire back-to-back). ``exponential``
    sleeps 1s, 2s, 4s, ... capped at 30 seconds so a flaky dependency
    doesn't push step wall time unboundedly.
    """
    seconds = _backoff_seconds(attempt, backoff)
    if seconds > 0:
        time.sleep(seconds)


# ── The runtime plugin ───────────────────────────────────────────────────────


@dataclass
class QuadroSagaRuntime(FrameworkRuntime):
    """Framework runtime plugin that executes ``stage(saga=...)``.

    The constructor takes no arguments — saga execution does not
    depend on an LLM client factory in milestone A. (Milestone B adds
    a constructor parameter for a registered ``Reasoner`` once
    ``reason`` steps land.)

    The saga runtime also ships a **deterministic chief mode** used by
    :class:`quadro.Pipeline` whenever no LLM-backed chief runtime is
    registered. ``Pipeline.build()`` calls
    :meth:`register_chief_context` once at build time, passing the
    lifecycle graph, the stage map, the A2A network, a worker
    registry, and a board-fn shim. On each chief turn,
    :meth:`run_chief_turn` walks every ``(active_status, capability)``
    entry in the stage map and dispatches all tasks in predecessor
    states via ``dispatch_batch``. No LLM, no tool wiring — the
    lifecycle graph already encodes the "what to do next" decision
    that an LLM chief would otherwise call tools for. Mirrors the
    ``dispatch_batch(UNASSIGNED → first_active)`` pattern at the top
    of the pipeline's chief policy, but across the full lifecycle.

    This makes a saga-only ``Pipeline(board)`` driveable end-to-end
    without an LLM, which is what the core ordering example in
    milestone D needs. LLM-backed pipelines register their own primary
    runtime (for example ``quadro_maf.MafChiefRuntime`` or
    ``quadro_langchain.LangChainChiefRuntime``) that handles the chief
    turn; ``_primary_runtime()`` returns the LLM-backed runtime in
    preference to this saga runtime, so the deterministic path is not
    reached when an LLM chief is in play.
    """

    runtime_id: str = "quadro_saga"
    _reasoners: dict[str, Any] = field(default_factory=dict)
    # Populated by register_chief_context() once per pipeline.build().
    # None when no chief context has been wired (e.g. tests that drive
    # run_stage directly bypassing the pipeline).
    _chief_context: dict[str, Any] | None = None

    # ── FrameworkRuntime protocol ────────────────────────────────────────────

    def can_handle(self, spec: Any) -> bool:
        """Match stages whose ``saga`` field is set."""
        return getattr(spec, "saga", None) is not None

    def decorate_tools(self, descriptors: list[Any]) -> list:
        """Saga runtime does not own chief tooling. The pipeline falls
        back to whichever other runtime is registered — typically the
        MAF or LangChain plugin that was added first."""
        return descriptors

    def register_chief_context(
        self,
        *,
        lifecycle: Any,
        stage_map: dict[str, Any],
        network: Any,
        worker_registry: dict[str, list[tuple[str, str]]],
        board_fn: Callable[[str, dict], dict],
    ) -> None:
        """Store the context needed for deterministic chief mode.

        Called once per pipeline by ``Pipeline.build()``. Kept as a
        separate method rather than widening the
        ``FrameworkRuntime.run_chief_turn`` signature so other
        runtimes (MAF, LangChain) can continue ignoring the chief
        context — they handle chief turns via an LLM call and don't
        need the lifecycle graph. ``Pipeline.build()`` uses
        ``hasattr`` to call this opt-in, so a runtime without a
        deterministic chief mode (the common case) is a no-op.
        """
        self._chief_context = {
            "lifecycle": lifecycle,
            "stage_map": stage_map,
            "network": network,
            "worker_registry": worker_registry,
            "board_fn": board_fn,
        }

    async def run_chief_turn(
        self,
        board_summary: str,
        instructions: str,
        tools: list,
        *,
        chief_name_prefix: str,
    ) -> str | None:
        """Deterministic chief mode: walk the lifecycle graph and
        dispatch every eligible task forward.

        The lifecycle + stage_map passed to ``register_chief_context``
        encode the same "what to do next" decision an LLM chief would
        make by looking at the board and picking a tool. For every
        ``(active_status, capability)`` in the stage map we find the
        predecessor states (the states a task must be in to be
        eligible for that active_status) and call ``dispatch_batch``
        to move them. This is the exact shape
        ``generate_tool_descriptors`` produces per-tool — the
        deterministic chief is the same dispatches, just done
        unconditionally per cycle rather than gated on an LLM
        tool call.

        Returns a one-line summary so the existing
        ``logger.info("Chief: %s", output[:200])`` line in the
        pipeline's chief policy has something to print — including
        on "quiet" cycles where no dispatches were needed. The
        pipeline interprets a ``None`` return as "the chief had
        nothing to say" and emits a WARNING ("Chief produced no
        output for current board snapshot"); post-milestone-D that
        warning would be misleading for saga-only pipelines because
        "no dispatches" is a routine outcome, not a misconfiguration.
        So we return the explicit "no dispatches needed" string for
        the quiet-cycle case and reserve ``None`` for the
        genuinely-unconfigured case (``register_chief_context`` was
        never called — preserving pre-milestone-D behaviour for
        direct callers that bypass ``Pipeline.build()``).

        ``board_summary``, ``instructions``, ``tools``, and
        ``chief_name_prefix`` are unused — they're the
        LLM-chief-oriented parameters of the ``FrameworkRuntime``
        protocol. The deterministic mode doesn't need them; the
        signature stays frozen because milestone D's brief
        explicitly forbids changing the protocol.
        """
        del board_summary, instructions, tools, chief_name_prefix  # unused here

        ctx = self._chief_context
        if ctx is None:
            return None

        lifecycle = ctx["lifecycle"]
        stage_map = ctx["stage_map"]
        network = ctx["network"]
        worker_registry = ctx["worker_registry"]
        board_fn = ctx["board_fn"]

        # Late import to avoid a circular dependency between the runtime
        # plugin and the pipeline module (pipeline imports FrameworkRuntime
        # under TYPE_CHECKING; this module imports from pipeline at
        # runtime only when deterministic chief is invoked).
        from ..dispatch import dispatch_batch
        from ..pipeline import _extract_raw_transitions, _predecessors_for

        if lifecycle is None or not stage_map:
            # Misconfiguration — still a chief-context-present case,
            # but there's nothing meaningful for the deterministic
            # chief to do. Return the quiet-cycle message rather than
            # None so the pipeline doesn't warn about it.
            return "deterministic chief: no dispatches needed this cycle"

        raw_transitions = _extract_raw_transitions(lifecycle)
        total_dispatched = 0
        stages_advanced = 0

        for active_status, stage_info in stage_map.items():
            if isinstance(stage_info, tuple):
                capability = stage_info[0]
            else:
                capability = stage_info
            preds = _predecessors_for(raw_transitions, active_status)
            if not preds:
                continue
            try:
                dispatched, _skipped = dispatch_batch(
                    board_fn,
                    network,
                    worker_registry,
                    preds,
                    active_status,
                    capability,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Deterministic chief: dispatch to %s (capability %s) failed: %s",
                    active_status, capability, exc,
                )
                continue
            if dispatched:
                total_dispatched += len(dispatched)
                stages_advanced += 1

        if total_dispatched == 0:
            return "deterministic chief: no dispatches needed this cycle"
        return (
            f"deterministic chief: dispatched {total_dispatched} task(s) "
            f"across {stages_advanced} stage(s)"
        )

    async def run_stage(self, ctx: RuntimeContext) -> StageRunResult:
        """Run a saga to completion (or to a suspension point) for one task.

        Loads ``SagaState`` from the Board if it exists; initializes a
        fresh state otherwise. Walks steps from ``state.pc`` until either
        the saga completes (``pc`` becomes ``None``) or a step returns a
        ``_Suspend`` sentinel (milestone F+; never happens in milestone A).
        Persists state after every successful step so a subsequent
        ``run_stage`` invocation for the same task resumes correctly.
        """
        saga: BuiltSaga = ctx.stage.saga
        task = ctx.task
        task_id = task["task_id"]
        telemetry: list[dict[str, Any]] = []

        # ── 0. Validate saga prerequisites ───────────────────────────────
        # Misconfiguration (e.g. a saga with reason steps but no
        # reasoner registered) is a developer error — it surfaces as a
        # loud exception rather than a StageRunResult with
        # ``terminal_reason="step_failed:..."``. The run loop's generic
        # Exception handler (added in milestone C) otherwise swallows
        # every error into structured telemetry, which is appropriate
        # for step-level failures but hides misconfiguration.
        self._validate_saga_prerequisites(saga)

        # ── 1. Load or initialize state ──────────────────────────────────
        state = self._load_or_init_state(saga, task_id, ctx)

        # Inject board access into task dict for steps that need it.
        # Underscore-prefixed keys never collide with real TaskRecord fields.
        task["_board_fn"] = ctx.board_fn
        try:
            full_state = ctx.board_fn("board.get_full_state", {})
            task["_board_state"] = full_state
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Failed to fetch board state for saga %s on task %s: %s",
                saga.name, task_id, exc,
            )
            task["_board_state"] = {"tasks": []}

        telemetry.append(build_runtime_event(
            runtime=self.runtime_id,
            event_type="saga.resume" if state.completed_steps else "saga.start",
            stage=ctx.stage.capability,
            task_id=task_id,
            payload={"saga": saga.name, "pc": state.pc},
        ))

        # ── 2. Walk steps until completion or suspension ─────────────────
        while state.pc is not None:
            # Gate-barrier check: a prior gate in this run recorded its
            # chosen branch. If ``pc`` has advanced (via linear
            # ``next_after``) into the OTHER branch's target step, the
            # saga's logical arm has ended — terminate early so we
            # don't execute steps that belong to a branch we didn't
            # take. Without this check, a saga like
            # ``gate → approve_path → reject_path`` would run both
            # branches, because linear advancement treats declaration
            # order as the only successor relation.
            if self._is_gate_barrier(saga, state):
                break

            step = saga.find(state.pc)

            step_start = datetime.now(UTC)
            telemetry.append(build_runtime_event(
                runtime=self.runtime_id,
                event_type="saga.step_start",
                stage=ctx.stage.capability,
                task_id=task_id,
                step_name=step.name,
                payload={"kind": step.kind.value},
            ))

            try:
                outcome = await self._dispatch_step(step, state, ctx, telemetry)
            except _GuardFailed as exc:
                return await self._handle_step_failure(
                    saga=saga,
                    telemetry=telemetry,
                    state=state,
                    ctx=ctx,
                    step=step,
                    step_start=step_start,
                    event_type="saga.guard_failed",
                    failure_reason=f"guard_failed:{exc.step_name}",
                )
            except _ExpectFailed as exc:
                return await self._handle_step_failure(
                    saga=saga,
                    telemetry=telemetry,
                    state=state,
                    ctx=ctx,
                    step=step,
                    step_start=step_start,
                    event_type="saga.expect_failed",
                    failure_reason=f"expect_failed:{exc.step_name}",
                )
            except _DeadlineExceeded as exc:
                return await self._handle_step_failure(
                    saga=saga,
                    telemetry=telemetry,
                    state=state,
                    ctx=ctx,
                    step=step,
                    step_start=step_start,
                    event_type="saga.deadline_exceeded",
                    failure_reason=f"deadline_exceeded:{exc.step_name}",
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Saga %r step %r raised %s: %s",
                    saga.name, step.name, type(exc).__name__, exc,
                )
                return await self._handle_step_failure(
                    saga=saga,
                    telemetry=telemetry,
                    state=state,
                    ctx=ctx,
                    step=step,
                    step_start=step_start,
                    event_type="saga.step_failed",
                    failure_reason=f"step_failed:{step.name}",
                    extra_payload={"error_type": type(exc).__name__},
                )

            # Suspension path — reserved for milestone F. Milestone A's
            # _dispatch_step never returns _Suspend, but the check is
            # here so the loop's shape is final.
            if isinstance(outcome, _Suspend):
                state.waiting_for = outcome.waiting_for
                self._persist(state, ctx)
                return StageRunResult(
                    output=None,
                    status=outcome.status,
                    telemetry=telemetry,
                    terminal_reason="saga_suspended",
                )

            # Normal completion: record output, advance pc, persist.
            # ``_PCJump`` means the handler has already set
            # ``state.pc`` itself (gates are the current canonical case)
            # and also recorded its own ``completed_steps`` entry.
            if not isinstance(outcome, _PCJump):
                state.completed_steps[step.name] = outcome
                state.pc = saga.next_after(step.name)

            duration_ms = max(
                0,
                int((datetime.now(UTC) - step_start).total_seconds() * 1000),
            )
            telemetry.append(build_runtime_event(
                runtime=self.runtime_id,
                event_type="saga.step_end",
                stage=ctx.stage.capability,
                task_id=task_id,
                step_name=step.name,
                duration_ms=duration_ms,
                payload={"kind": step.kind.value},
            ))

            self._persist(state, ctx)

        # ── 3. Saga complete ─────────────────────────────────────────────
        # Final output is the output of the LAST step that actually
        # ran, not the last step in declaration order — gate routing
        # may have terminated the saga before reaching the declared
        # tail. ``completed_steps`` preserves insertion order (Python
        # dict semantics since 3.7), so ``reversed`` on the keys yields
        # the last completed step.
        final_output: Any = None
        if state.completed_steps:
            last_step_name = next(reversed(state.completed_steps))
            final_output = state.completed_steps[last_step_name]
        telemetry.append(build_runtime_event(
            runtime=self.runtime_id,
            event_type="saga.complete",
            stage=ctx.stage.capability,
            task_id=task_id,
            payload={"saga": saga.name},
        ))

        return StageRunResult(
            output=final_output,
            status=ctx.stage.success_status,
            telemetry=telemetry,
            terminal_reason="saga_completed",
        )

    # ── Step dispatch ────────────────────────────────────────────────────────

    async def _dispatch_step(
        self,
        step: Step,
        state: SagaState,
        ctx: RuntimeContext,
        telemetry: list[dict[str, Any]] | None = None,
    ) -> Any:
        """Dispatch a single step by kind.

        Deterministic and reason steps go through ``_apply_modifiers``
        so ``.retry()`` / ``.deadline()`` can wrap them uniformly.
        Gate / guard / expect / evidence / stamp are dispatched
        directly — their semantics don't meaningfully compose with retry
        or deadline, and the short-circuit control flow (gates jump
        ``pc``; guards / expects raise signal exceptions) would be
        confusing under a retry wrapper.
        """
        if step.kind == StepKind.DETERMINISTIC:
            return await self._apply_modifiers(
                step, state, ctx, self._run_deterministic, telemetry
            )
        if step.kind == StepKind.REASON:
            return await self._apply_modifiers(
                step, state, ctx, self._run_reason, telemetry
            )
        if step.kind == StepKind.GATE:
            return await self._run_gate(step, state, ctx)
        if step.kind == StepKind.GUARD:
            return await self._run_guard(step, state, ctx)
        if step.kind == StepKind.EXPECT:
            return await self._run_expect(step, state, ctx)
        if step.kind == StepKind.EVIDENCE:
            return await self._run_evidence(step, state, ctx)
        if step.kind == StepKind.STAMP:
            return await self._run_stamp(step, state, ctx)
        if step.kind == StepKind.PARALLEL:
            return await self._run_parallel(step, state, ctx, telemetry)

        raise NotImplementedError(
            f"Saga runtime does not yet dispatch {step.kind.value!r} steps "
            f"— landed in a later milestone"
        )

    # ── Modifier wrapper ────────────────────────────────────────────────────

    async def _apply_modifiers(
        self,
        step: Step,
        state: SagaState,
        ctx: RuntimeContext,
        inner: Callable[[Step, SagaState, RuntimeContext], Awaitable[Any]],
        telemetry: list[dict[str, Any]] | None = None,
    ) -> Any:
        """Wrap ``inner`` dispatch with retry + deadline modifiers, if present.

        Order of composition: deadline is applied per-attempt, retry is
        the outer loop. A step declared with
        ``.retry(attempts=3).deadline(within=30s)`` gets up to 3
        attempts, each bounded at 30 seconds. Exceptions whose type is
        not in ``retry.on`` propagate on their first occurrence — the
        retry loop does not swallow typed errors it was not asked to.
        """
        retry_cfg = step.modifiers.get("retry")
        deadline_cfg = step.modifiers.get("deadline")
        attempts = retry_cfg["attempts"] if retry_cfg else 1
        catch: tuple[type[BaseException], ...] = (
            retry_cfg["on"] if retry_cfg else ()
        )
        backoff = retry_cfg["backoff"] if retry_cfg else "fixed"
        timeout = deadline_cfg["seconds"] if deadline_cfg else None

        for attempt in range(1, attempts + 1):
            try:
                if timeout is not None:
                    return await asyncio.wait_for(
                        inner(step, state, ctx), timeout=timeout
                    )
                return await inner(step, state, ctx)
            except asyncio.TimeoutError:
                # Per-attempt deadline trip. Only retried if the caller
                # explicitly listed TimeoutError in ``on=(...)`` —
                # otherwise the deadline is treated as a terminal
                # failure of this step.
                if asyncio.TimeoutError in catch and attempt < attempts:
                    sleep_seconds = _backoff_seconds(attempt, backoff)
                    self._emit_retry_attempt(
                        telemetry,
                        step=step,
                        ctx=ctx,
                        attempt_number=attempt,
                        error_type="TimeoutError",
                        sleep_seconds_before_next=sleep_seconds,
                    )
                    _backoff_sleep(attempt, backoff)
                    continue
                raise _DeadlineExceeded(step.name) from None
            except catch as exc:
                if attempt < attempts:
                    logger.debug(
                        "Saga %r step %r attempt %d/%d failed (%s); retrying",
                        state.saga_name, step.name, attempt, attempts, exc,
                    )
                    sleep_seconds = _backoff_seconds(attempt, backoff)
                    self._emit_retry_attempt(
                        telemetry,
                        step=step,
                        ctx=ctx,
                        attempt_number=attempt,
                        error_type=type(exc).__name__,
                        sleep_seconds_before_next=sleep_seconds,
                    )
                    _backoff_sleep(attempt, backoff)
                    continue
                raise

        # Unreachable: the loop either returns, re-raises, or continues
        # exactly ``attempts`` times. Present for static analysis only.
        raise RuntimeError(  # pragma: no cover
            f"Saga {state.saga_name!r} step {step.name!r}: retry loop fell "
            f"through without returning a result — unreachable"
        )

    # ── Step-kind handlers ──────────────────────────────────────────────────

    async def _run_deterministic(
        self,
        step: Step,
        state: SagaState,
        ctx: RuntimeContext,
    ) -> Any:
        """Invoke a deterministic step's callable and return its result.

        The callable is invoked as ``fn(saga_ctx)``. Async callables are
        awaited; sync callables are called directly.
        """
        fn = step.payload["fn"]
        saga_ctx = self._saga_context(state, ctx)
        if asyncio.iscoroutinefunction(fn):
            return await fn(saga_ctx)
        return fn(saga_ctx)

    async def _run_reason(
        self,
        step: Step,
        state: SagaState,
        ctx: RuntimeContext,
    ) -> Any:
        """Invoke the registered reasoner for a reason step.

        Also bumps ``_tokens:{task_id}.by_stage[stage_capability]`` by
        the reasoner's reported ``tokens_used`` — the saga-side
        counterpart to ``_bump_stage_tokens`` in
        ``examples/.../agents.py``. This is what closes observation #2
        from milestone B's run note: whether a stage runs as a saga or
        as the legacy workflow adapter, the published flight-plan JSON
        carries the same ``tokens.by_stage`` shape.
        """
        import json
        from pathlib import Path

        # Saga-level prerequisite validation (run_stage front door)
        # already guarantees at least one reasoner is registered by the
        # time we reach here.
        #
        # Milestone G — per-step dispatch via ``step.payload["via"]``.
        # When ``via`` is set (and not None), look up the reasoner
        # whose ``reasoner_id`` matches; raise a clear RuntimeError
        # naming the missing id and the available ids if the lookup
        # fails. When ``via`` is absent or None, fall back to the
        # first registered reasoner — the milestone-B behaviour,
        # preserved for backward compatibility.
        via = step.payload.get("via")
        if via is not None:
            reasoner = self._reasoners.get(via)
            if reasoner is None:
                available = sorted(self._reasoners.keys())
                raise RuntimeError(
                    f"Saga {state.saga_name!r} reason step {step.name!r}: "
                    f"via={via!r} references an unregistered reasoner. "
                    f"Available reasoner_ids: {available}"
                )
        else:
            reasoner = next(iter(self._reasoners.values()))

        raw_prompt = step.payload["prompt"]
        if isinstance(raw_prompt, Path):
            prompt = raw_prompt.read_text()
        else:
            prompt = str(raw_prompt)

        user_message_fn = step.payload["user_message"]
        saga_ctx = self._saga_context(state, ctx)
        msg = user_message_fn(saga_ctx)
        if isinstance(msg, dict):
            user_message = json.dumps(msg)
        else:
            user_message = str(msg)

        schema = step.payload["schema"]

        token_reporter = getattr(ctx, "token_reporter", None)

        try:
            result = await reasoner.reason(
                prompt=prompt,
                user_message=user_message,
                schema=schema,
                token_reporter=token_reporter,
                step_name=step.name,
            )
        except TypeError as exc:
            if "step_name" not in str(exc) or "unexpected keyword" not in str(exc):
                raise
            result = await reasoner.reason(
                prompt=prompt,
                user_message=user_message,
                schema=schema,
                token_reporter=token_reporter,
            )

        # Milestone G: record which reasoner ran this step. Two audit
        # surfaces, one shape each:
        #   (a) per-saga — ``state.reasoners_by_step`` persists through
        #       the same SagaState round-trip as ``compensations_run``.
        #   (b) cross-saga — ``_tokens:{task_id}.reasoners_by_step``
        #       accumulates across every saga for the task; read by
        #       the flight-plan JSON's ``reasoners.by_step`` section.
        # Best-effort: a reasoner that exposes no ``reasoner_id``
        # simply doesn't contribute to either surface — telemetry
        # never fails a step.
        reasoner_id = getattr(reasoner, "reasoner_id", None)
        if reasoner_id:
            state.reasoners_by_step[step.name] = str(reasoner_id)
            self._record_reasoner_on_task(
                ctx.board_fn,
                ctx.task["task_id"],
                step.name,
                str(reasoner_id),
            )

        tokens_used = int(getattr(result, "tokens_used", 0) or 0)
        if tokens_used > 0:
            self._bump_stage_tokens(
                ctx.board_fn,
                ctx.task["task_id"],
                ctx.stage.capability,
                tokens_used,
            )
            if reasoner_id:
                self._write_token_record(
                    ctx.board_fn,
                    task_id=ctx.task["task_id"],
                    stage=ctx.stage.capability,
                    step_name=step.name,
                    reasoner_id=str(reasoner_id),
                    tokens_total=tokens_used,
                )

        return result.output

    async def _run_gate(
        self,
        step: Step,
        state: SagaState,
        ctx: RuntimeContext,
    ) -> _PCJump:
        """Evaluate the gate's predicate and jump ``pc`` to the chosen branch.

        Records ``{"chosen": <branch_name>}`` under
        ``state.completed_steps[step.name]`` so the routing decision is
        visible to downstream steps (and to audit queries) without
        re-evaluating the predicate. Returning ``_PCJump()`` tells the
        run loop that ``pc`` has already been advanced.
        """
        saga_ctx = self._saga_context(state, ctx)
        when = step.payload["when"]
        chosen = step.payload["on_true"] if when(saga_ctx) else step.payload["on_false"]
        state.completed_steps[step.name] = {"chosen": chosen}
        state.pc = chosen
        return _PCJump()

    async def _run_guard(
        self,
        step: Step,
        state: SagaState,
        ctx: RuntimeContext,
    ) -> Any:
        """Pre-condition: halts the saga via ``_GuardFailed`` if check
        returns False; otherwise returns None so the loop advances."""
        saga_ctx = self._saga_context(state, ctx)
        if step.payload["check"](saga_ctx):
            return None
        raise _GuardFailed(step.name)

    async def _run_expect(
        self,
        step: Step,
        state: SagaState,
        ctx: RuntimeContext,
    ) -> Any:
        """Post-condition: same shape as ``_run_guard`` but raises the
        distinct ``_ExpectFailed`` so the run loop can emit
        ``saga.expect_failed`` rather than ``saga.guard_failed``."""
        saga_ctx = self._saga_context(state, ctx)
        if step.payload["invariant"](saga_ctx):
            return None
        raise _ExpectFailed(step.name)

    async def _run_evidence(
        self,
        step: Step,
        state: SagaState,
        ctx: RuntimeContext,
    ) -> Any:
        """Best-effort audit capture. Records into ``state.evidence``.

        A raising ``capture`` callable is logged at WARNING and the
        saga continues to the next step — evidence is governance
        signal, not load-bearing control flow, so it never fails a
        saga.
        """
        saga_ctx = self._saga_context(state, ctx)
        try:
            record = step.payload["capture"](saga_ctx)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Saga %r evidence step %r capture raised; skipping: %s",
                state.saga_name, step.name, exc,
            )
            return None
        state.evidence[step.name] = record
        return record

    async def _run_stamp(
        self,
        step: Step,
        state: SagaState,
        ctx: RuntimeContext,
    ) -> Any:
        """Append a timestamped record to ``state.stamps``.

        Unlike evidence, stamp capture failures DO propagate — a stamp
        that can't be produced is a bug rather than a best-effort
        audit, because stamps are ordered markers that later milestones
        rely on for consistency (e.g. milestone F's fork-child dedup).
        """
        saga_ctx = self._saga_context(state, ctx)
        value = step.payload["capture"](saga_ctx)
        state.stamps.append({
            "key": step.name,
            "value": value,
            "timestamp": datetime.now(UTC).isoformat(),
        })
        return value

    async def _run_parallel(
        self,
        step: Step,
        state: SagaState,
        ctx: RuntimeContext,
        telemetry: list[dict[str, Any]] | None = None,
    ) -> Any:
        """Run all branches concurrently and join according to the mode."""
        branches: tuple[BuiltBranch, ...] = tuple(step.payload["branches"])
        join = step.payload["join"]
        coroutines = [
            self._run_branch(branch, state, ctx, step.name, telemetry)
            for branch in branches
        ]

        if join == "all":
            results = await asyncio.gather(*coroutines, return_exceptions=True)
            outputs: dict[str, Any] = {}
            failures: dict[str, BaseException] = {}
            for branch, result in zip(branches, results):
                if isinstance(result, BaseException):
                    failures[branch.name] = result
                else:
                    outputs[branch.name] = result
            if failures:
                if outputs:
                    state.completed_steps[step.name] = outputs
                raise _ParallelFailed(step.name, failures)
            return outputs

        if join == "any":
            return await self._run_any(branches, coroutines, step.name)

        _, n = join
        return await self._run_n_of_m(branches, coroutines, n, step.name, state)

    async def _run_any(
        self,
        branches: tuple[BuiltBranch, ...],
        coroutines: list[Awaitable[Any]],
        parallel_name: str,
    ) -> dict[str, Any]:
        """Wait for the first successful branch; cancel the rest."""
        pending: dict[asyncio.Task, BuiltBranch] = {
            asyncio.create_task(c, name=branch.name): branch
            for branch, c in zip(branches, coroutines)
        }
        failures: dict[str, BaseException] = {}

        while pending:
            done, _ = await asyncio.wait(
                pending.keys(),
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in done:
                branch = pending.pop(task)
                try:
                    result = task.result()
                except Exception as exc:  # noqa: BLE001
                    failures[branch.name] = exc
                    continue
                await self._cancel_pending_branches(pending)
                return {branch.name: result}

        raise _ParallelFailed(parallel_name, failures)

    async def _run_n_of_m(
        self,
        branches: tuple[BuiltBranch, ...],
        coroutines: list[Awaitable[Any]],
        n: int,
        parallel_name: str,
        state: SagaState,
    ) -> dict[str, Any]:
        """Wait for at least N successful branches; cancel the rest."""
        pending: dict[asyncio.Task, BuiltBranch] = {
            asyncio.create_task(c, name=branch.name): branch
            for branch, c in zip(branches, coroutines)
        }
        successes: dict[str, Any] = {}
        failures: dict[str, BaseException] = {}

        while pending and len(successes) < n:
            done, _ = await asyncio.wait(
                pending.keys(),
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in done:
                branch = pending.pop(task)
                try:
                    successes[branch.name] = task.result()
                except Exception as exc:  # noqa: BLE001
                    failures[branch.name] = exc
                    continue
            if len(successes) >= n:
                break

        await self._cancel_pending_branches(pending)
        if len(successes) < n:
            if successes:
                state.completed_steps[parallel_name] = successes
            raise _ParallelFailed(parallel_name, failures)
        return successes

    async def _cancel_pending_branches(
        self,
        pending: dict[asyncio.Task, BuiltBranch],
    ) -> None:
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending.keys(), return_exceptions=True)

    async def _run_branch(
        self,
        branch: BuiltBranch,
        parent_state: SagaState,
        parent_ctx: RuntimeContext,
        parallel_name: str,
        telemetry: list[dict[str, Any]] | None = None,
    ) -> Any:
        """Run one branch with branch-local state and parent-step visibility."""
        branch_start = datetime.now(UTC)
        self._emit_parallel_branch_event(
            telemetry,
            event_type="saga.parallel_branch_started",
            ctx=parent_ctx,
            parallel_name=parallel_name,
            branch_name=branch.name,
        )
        branch_states = parent_state.branch_states.setdefault(parallel_name, {})
        branch_state = branch_states.get(branch.name)
        if branch_state is None:
            branch_state = SagaState(
                saga_name=f"{parent_state.saga_name}.{parallel_name}.{branch.name}",
                pc=branch.steps[0].name if branch.steps else None,
                started_at=datetime.now(UTC).isoformat(),
            )
            branch_states[branch.name] = branch_state

        branch_ctx = RuntimeContext(
            stage=parent_ctx.stage,
            task=parent_ctx.task,
            context={
                **parent_ctx.context,
                "_saga_parent_steps": dict(parent_state.completed_steps),
            },
            board_fn=parent_ctx.board_fn,
            token_reporter=parent_ctx.token_reporter,
            telemetry_sink=parent_ctx.telemetry_sink,
        )

        try:
            while branch_state.pc is not None:
                step = self._find_branch_step(branch, branch_state.pc)
                if step.name in branch_state.completed_steps:
                    branch_state.pc = self._branch_next_after(branch, step.name)
                    continue
                outcome = await self._dispatch_step(
                    step, branch_state, branch_ctx, telemetry
                )
                if isinstance(outcome, _Suspend):
                    raise RuntimeError(
                        f"Parallel branch {branch.name!r} cannot suspend in milestone E"
                    )
                if isinstance(outcome, _PCJump):
                    continue
                branch_state.completed_steps[step.name] = outcome
                branch_state.pc = self._branch_next_after(branch, step.name)
                self._persist(parent_state, parent_ctx)

            if not branch.steps:
                output = None
            else:
                output = branch_state.completed_steps[branch.steps[-1].name]
            duration_ms = max(
                0,
                int((datetime.now(UTC) - branch_start).total_seconds() * 1000),
            )
            self._emit_parallel_branch_event(
                telemetry,
                event_type="saga.parallel_branch_completed",
                ctx=parent_ctx,
                parallel_name=parallel_name,
                branch_name=branch.name,
                duration_ms=duration_ms,
            )
            return output
        except asyncio.CancelledError:
            duration_ms = max(
                0,
                int((datetime.now(UTC) - branch_start).total_seconds() * 1000),
            )
            self._emit_parallel_branch_event(
                telemetry,
                event_type="saga.parallel_branch_cancelled",
                ctx=parent_ctx,
                parallel_name=parallel_name,
                branch_name=branch.name,
                duration_ms=duration_ms,
            )
            raise
        except Exception as exc:
            duration_ms = max(
                0,
                int((datetime.now(UTC) - branch_start).total_seconds() * 1000),
            )
            self._emit_parallel_branch_event(
                telemetry,
                event_type="saga.parallel_branch_failed",
                ctx=parent_ctx,
                parallel_name=parallel_name,
                branch_name=branch.name,
                duration_ms=duration_ms,
                extra_payload={"error_type": type(exc).__name__},
            )
            raise

    def _emit_parallel_branch_event(
        self,
        telemetry: list[dict[str, Any]] | None,
        *,
        event_type: str,
        ctx: RuntimeContext,
        parallel_name: str,
        branch_name: str,
        duration_ms: int | None = None,
        extra_payload: dict[str, Any] | None = None,
    ) -> None:
        if telemetry is None:
            return
        payload = {
            "parallel_step_name": parallel_name,
            "branch_name": branch_name,
        }
        if extra_payload:
            payload.update(extra_payload)
        telemetry.append(build_runtime_event(
            runtime=self.runtime_id,
            event_type=event_type,
            stage=ctx.stage.capability,
            task_id=ctx.task["task_id"],
            step_name=parallel_name,
            duration_ms=duration_ms,
            payload=payload,
        ))

    def _emit_retry_attempt(
        self,
        telemetry: list[dict[str, Any]] | None,
        *,
        step: Step,
        ctx: RuntimeContext,
        attempt_number: int,
        error_type: str,
        sleep_seconds_before_next: float,
    ) -> None:
        if telemetry is None:
            return
        telemetry.append(build_runtime_event(
            runtime=self.runtime_id,
            event_type="saga.retry_attempt",
            stage=ctx.stage.capability,
            task_id=ctx.task["task_id"],
            step_name=step.name,
            payload={
                "attempt_number": attempt_number,
                "last_error_type": error_type,
                "sleep_seconds_before_next": sleep_seconds_before_next,
            },
        ))

    @staticmethod
    def _find_branch_step(branch: BuiltBranch, step_name: str) -> Step:
        for step in branch.steps:
            if step.name == step_name:
                return step
        raise KeyError(f"branch {branch.name!r} has no step named {step_name!r}")

    @staticmethod
    def _branch_next_after(branch: BuiltBranch, step_name: str) -> str | None:
        for i, step in enumerate(branch.steps):
            if step.name == step_name:
                return branch.steps[i + 1].name if i + 1 < len(branch.steps) else None
        raise KeyError(f"branch {branch.name!r} has no step named {step_name!r}")

    # ── SagaContext factory ─────────────────────────────────────────────────

    def _saga_context(self, state: SagaState, ctx: RuntimeContext) -> SagaContext:
        """Build a SagaContext from the current runtime state.

        Centralised so every dispatch handler sees the same view:
        ``ctx.step`` reflects everything completed so far (including
        gate ``{"chosen": ...}`` entries), ``ctx.evidence`` reflects
        everything captured so far, ``ctx.now`` is a fresh UTC snapshot
        at dispatch time.
        """
        return SagaContext(
            task=ctx.task,
            step={
                **dict(ctx.context.get("_saga_parent_steps") or {}),
                **dict(state.completed_steps),
            },
            evidence=dict(state.evidence),
            now=datetime.now(UTC),
        )

    def _is_gate_barrier(self, saga: BuiltSaga, state: SagaState) -> bool:
        """Return True if ``state.pc`` points at the non-chosen branch
        target of any gate that has already routed in this run.

        The saga DSL does not require gates to have an explicit
        convergence point — each gate arm is just "linear execution
        from the chosen target until you would cross into the other
        arm, or end of saga." This helper encodes that "or end of
        saga" boundary by treating the unchosen branch target (and any
        step after it in declaration order) as a terminator.

        Cost is O(number of gates previously dispatched), which is
        negligible for realistic sagas (<10 gates).
        """
        pc = state.pc
        if pc is None:
            return False
        for step_name, outcome in state.completed_steps.items():
            if not isinstance(outcome, dict) or "chosen" not in outcome:
                continue
            try:
                gate_step = saga.find(step_name)
            except KeyError:
                continue
            if gate_step.kind is not StepKind.GATE:
                continue
            chosen = outcome["chosen"]
            on_true = gate_step.payload.get("on_true")
            on_false = gate_step.payload.get("on_false")
            barrier = on_false if chosen == on_true else on_true
            if barrier == pc:
                return True
        return False

    # ── Failure helpers ─────────────────────────────────────────────────────

    async def _handle_step_failure(
        self,
        *,
        saga: BuiltSaga,
        telemetry: list[dict[str, Any]],
        state: SagaState,
        ctx: RuntimeContext,
        step: Step,
        step_start: datetime,
        event_type: str,
        failure_reason: str,
        extra_payload: dict[str, Any] | None = None,
    ) -> StageRunResult:
        """Emit the step-failure telemetry, optionally run the
        compensation walk, and build the terminal StageRunResult.

        When the saga has at least one registered compensation, the
        walker is invoked against the steps that have completed so
        far. The resulting ``terminal_reason`` reflects the outcome
        of the walk rather than the original step failure:

        - ``"compensated:<failed_step>"`` — every compensation
          succeeded (or none needed to run).
        - ``"compensation_partial:<failed_step>"`` — at least one
          compensation failed and ``on_failure="continue"`` allowed
          the walk to proceed through the rest.
        - ``"compensation_failed:<step>"`` — a compensation with
          ``on_failure="halt"`` raised; the walk aborted at that
          step.

        When the saga has no compensations at all, the original
        ``failure_reason`` is the terminal_reason and no rollback
        telemetry is emitted. This keeps the existing milestone-C
        shape for sagas that never registered a ``.compensate(...)``
        directive.

        Persists the saga state before returning so audit queries
        can see the ``pc`` that failed AND the compensation log the
        walker produced.
        """
        duration_ms = max(
            0,
            int((datetime.now(UTC) - step_start).total_seconds() * 1000),
        )
        payload: dict[str, Any] = {"kind": step.kind.value, "step_name": step.name}
        if extra_payload:
            payload.update(extra_payload)
        telemetry.append(build_runtime_event(
            runtime=self.runtime_id,
            event_type=event_type,
            stage=ctx.stage.capability,
            task_id=ctx.task["task_id"],
            step_name=step.name,
            duration_ms=duration_ms,
            payload=payload,
        ))

        if self._has_compensations_to_apply(saga, state):
            terminal_reason = await self._apply_compensations(
                saga=saga,
                state=state,
                ctx=ctx,
                telemetry=telemetry,
                failed_step=step.name,
            )
        else:
            terminal_reason = failure_reason

        self._persist(state, ctx)
        return StageRunResult(
            output=None,
            status=ctx.stage.failure_status,
            telemetry=telemetry,
            terminal_reason=terminal_reason,
        )

    def _has_compensations_to_apply(self, saga: BuiltSaga, state: SagaState) -> bool:
        if saga.compensations:
            return True
        for step_name, output in state.completed_steps.items():
            try:
                step = saga.find(step_name)
            except KeyError:
                continue
            if step.kind is not StepKind.PARALLEL or not isinstance(output, dict):
                continue
            for branch_name in output:
                try:
                    branch = self._find_branch_by_name(step, branch_name)
                except KeyError:
                    continue
                if branch.compensations:
                    return True
        return False

    @staticmethod
    def _find_branch_by_name(step: Step, branch_name: str) -> BuiltBranch:
        for branch in step.payload.get("branches", ()):
            if branch.name == branch_name:
                return branch
        raise KeyError(
            f"parallel step {step.name!r} has no branch named {branch_name!r}"
        )

    async def _apply_compensations(
        self,
        *,
        saga: BuiltSaga,
        state: SagaState,
        ctx: RuntimeContext,
        telemetry: list[dict[str, Any]],
        failed_step: str,
    ) -> str:
        """Walk registered compensations in reverse completion order.

        Returns the ``terminal_reason`` that summarises the walk. Also
        appends attempt records to ``state.compensations_run`` and
        emits compensation telemetry through the existing envelope.
        Milestone-D event names (``saga.compensation_start`` /
        ``saga.compensation_end``) are preserved for existing
        consumers; milestone H adds the clearer
        ``saga.compensation_started`` / ``saga.compensation_completed``
        aliases with the same payload shape.

        Resume semantics: any step whose name already appears in
        ``state.compensations_run`` with an ``outcome`` key (either
        ``"ok"`` or ``"failed"``) is skipped. A ``"failed"`` record is
        skipped because the failure was already captured; re-attempting
        risks a different failure mode and is outside the milestone-D
        resume contract. A step in flight when the worker crashed
        (no record at all) is re-attempted — idempotency is the
        compensation author's responsibility.
        """
        already_logged = {
            r.get("step")
            for r in state.compensations_run
            if isinstance(r, dict) and r.get("outcome") in ("ok", "failed")
        }

        # Reverse completion order — gate records ({"chosen": ...}) are
        # legitimate entries in completed_steps but have no registered
        # compensation, so they fall out naturally when we check
        # compensation membership below.
        ordered_steps = list(state.completed_steps.keys())
        invoked = 0
        failed = 0
        halted_step: str | None = None

        for step_name in reversed(ordered_steps):
            try:
                step = saga.find(step_name)
            except KeyError:
                step = None

            if step is not None and step.kind is StepKind.PARALLEL:
                branch_outputs = state.completed_steps.get(step_name)
                if not isinstance(branch_outputs, dict):
                    continue
                for branch_name in reversed(list(branch_outputs.keys())):
                    try:
                        branch = self._find_branch_by_name(step, branch_name)
                    except KeyError:
                        continue
                    branch_state = (
                        state.branch_states
                        .get(step_name, {})
                        .get(branch_name)
                    )
                    if branch_state is None:
                        continue
                    for branch_step_name in reversed(list(branch_state.completed_steps.keys())):
                        if branch_step_name not in branch.compensations:
                            continue
                        log_key = f"{step_name}.{branch_name}.{branch_step_name}"
                        if log_key in already_logged:
                            continue
                        outcome = await self._invoke_compensation(
                            log_key=log_key,
                            display_step=branch_step_name,
                            comp_cfg=branch.compensations[branch_step_name],
                            state_for_ctx=branch_state,
                            state_for_log=state,
                            ctx=ctx,
                            telemetry=telemetry,
                        )
                        invoked += 1
                        if outcome == "failed":
                            failed += 1
                            if branch.compensations[branch_step_name].get("on_failure") == "halt":
                                halted_step = log_key
                                break
                    if halted_step is not None:
                        break
                if halted_step is not None:
                    break
                continue

            if step_name not in saga.compensations:
                continue
            if step_name in already_logged:
                continue

            outcome = await self._invoke_compensation(
                log_key=step_name,
                display_step=step_name,
                comp_cfg=saga.compensations[step_name],
                state_for_ctx=state,
                state_for_log=state,
                ctx=ctx,
                telemetry=telemetry,
            )
            invoked += 1
            if outcome == "failed":
                failed += 1
                if saga.compensations[step_name].get("on_failure") == "halt":
                    halted_step = step_name
                    break

        if halted_step is not None:
            terminal_reason = f"compensation_failed:{halted_step}"
        elif failed > 0:
            terminal_reason = f"compensation_partial:{failed_step}"
        else:
            terminal_reason = f"compensated:{failed_step}"

        telemetry.append(build_runtime_event(
            runtime=self.runtime_id,
            event_type="saga.rollback_complete",
            stage=ctx.stage.capability,
            task_id=ctx.task["task_id"],
            payload={
                "compensations_invoked": invoked,
                "compensations_failed": failed,
                "terminal_reason": terminal_reason,
            },
        ))
        return terminal_reason

    async def _invoke_compensation(
        self,
        *,
        log_key: str,
        display_step: str,
        comp_cfg: dict[str, Any],
        state_for_ctx: SagaState,
        state_for_log: SagaState,
        ctx: RuntimeContext,
        telemetry: list[dict[str, Any]],
    ) -> str:
        undo = comp_cfg["undo"]
        on_failure = comp_cfg.get("on_failure", "continue")

        telemetry.append(build_runtime_event(
            runtime=self.runtime_id,
            event_type="saga.compensation_start",
            stage=ctx.stage.capability,
            task_id=ctx.task["task_id"],
            step_name=log_key,
            payload={"step": log_key, "attempt_number": 1},
        ))
        telemetry.append(build_runtime_event(
            runtime=self.runtime_id,
            event_type="saga.compensation_started",
            stage=ctx.stage.capability,
            task_id=ctx.task["task_id"],
            step_name=log_key,
            payload={"step": log_key, "attempt_number": 1},
        ))

        comp_start = datetime.now(UTC)
        saga_ctx = self._saga_context(state_for_ctx, ctx)
        try:
            if asyncio.iscoroutinefunction(undo):
                await undo(saga_ctx)
            else:
                undo(saga_ctx)
        except Exception as exc:  # noqa: BLE001
            duration_ms = max(
                0,
                int((datetime.now(UTC) - comp_start).total_seconds() * 1000),
            )
            state_for_log.compensations_run.append({
                "step": log_key,
                "outcome": "failed",
                "duration_ms": duration_ms,
                "timestamp": datetime.now(UTC).isoformat(),
                "error_type": type(exc).__name__,
                "error_message": str(exc)[:500],
            })
            telemetry.append(build_runtime_event(
                runtime=self.runtime_id,
                event_type="saga.compensation_failed",
                stage=ctx.stage.capability,
                task_id=ctx.task["task_id"],
                step_name=log_key,
                duration_ms=duration_ms,
                payload={
                    "step": log_key,
                    "error_type": type(exc).__name__,
                    "error_message": str(exc)[:500],
                    "duration_ms": duration_ms,
                    "on_failure": on_failure,
                },
            ))
            return "failed"

        duration_ms = max(
            0,
            int((datetime.now(UTC) - comp_start).total_seconds() * 1000),
        )
        state_for_log.compensations_run.append({
            "step": log_key,
            "outcome": "ok",
            "duration_ms": duration_ms,
            "timestamp": datetime.now(UTC).isoformat(),
        })
        telemetry.append(build_runtime_event(
            runtime=self.runtime_id,
            event_type="saga.compensation_end",
            stage=ctx.stage.capability,
            task_id=ctx.task["task_id"],
            step_name=log_key,
            duration_ms=duration_ms,
            payload={"step": log_key, "duration_ms": duration_ms},
        ))
        telemetry.append(build_runtime_event(
            runtime=self.runtime_id,
            event_type="saga.compensation_completed",
            stage=ctx.stage.capability,
            task_id=ctx.task["task_id"],
            step_name=log_key,
            duration_ms=duration_ms,
            payload={"step": log_key, "duration_ms": duration_ms},
        ))
        del display_step  # retained in signature for readable call sites
        return "ok"

    # ── Token attribution (observation #2 closeout) ─────────────────────────

    def _bump_stage_tokens(
        self,
        board_fn: Callable[[str, dict], dict],
        task_id: str,
        stage: str,
        delta: int,
    ) -> None:
        """Merge-add ``delta`` tokens onto ``_tokens:{task_id}.by_stage[stage]``.

        Mirrors the workflow-path helper of the same name in the
        newsroom ``agents.py``. Living on the saga runtime means
        saga-driven stages get the same per-stage attribution that
        workflow-driven stages get, so the published flight-plan JSON's
        ``tokens.by_stage`` section is identical regardless of which
        path the stage took — the closeout for observation #2 in
        milestone B's run note.

        Failures in the read/write are logged at DEBUG and swallowed
        — token attribution is telemetry, and telemetry must never
        fail a step.
        """
        if delta <= 0:
            return
        key = f"_tokens:{task_id}"
        try:
            existing = (board_fn("board.get_data", {"key": key}) or {}).get("value") or {}
        except Exception as exc:  # noqa: BLE001
            logger.debug("tokens: failed to read %s: %s", key, exc)
            existing = {}
        # Read-modify-write preserves sibling keys (e.g. milestone-G's
        # ``reasoners_by_step`` lives under the same task-scoped key).
        value: dict[str, Any] = dict(existing)
        by_stage = dict(existing.get("by_stage") or {})
        by_stage[stage] = int(by_stage.get(stage, 0)) + int(delta)
        value["by_stage"] = by_stage
        try:
            board_fn(
                "board.put_data",
                {"key": key, "value": value},
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("tokens: failed to write %s: %s", key, exc)

    def _record_reasoner_on_task(
        self,
        board_fn: Callable[[str, dict], dict],
        task_id: str,
        step_name: str,
        reasoner_id: str,
    ) -> None:
        """Record the reasoner_id used for ``step_name`` under the
        task-scoped ``_tokens:{task_id}`` data-store key.

        Milestone G uses the same board-level key as
        ``_bump_stage_tokens`` to colocate per-task reason telemetry:
        ``by_stage`` holds per-stage token totals, ``reasoners_by_step``
        holds the reasoner used for each reason step. Using one key
        keeps the "Board contract is unchanged" property from the
        milestone-G brief — no new data-store key, just an additional
        field under an existing one.

        Cross-saga accumulation is the point: each stage's saga
        overwrites its own ``_saga:{task_id}`` state, so the
        per-saga ``state.reasoners_by_step`` is only visible while
        that saga runs. This board-level record accumulates across
        every saga for the task and is what the newsroom flight-plan
        JSON reads when it builds its ``reasoners.by_step`` section.

        Failures in the read/write are logged at DEBUG and swallowed
        — telemetry never fails a step.
        """
        if not reasoner_id:
            return
        key = f"_tokens:{task_id}"
        try:
            existing = (board_fn("board.get_data", {"key": key}) or {}).get("value") or {}
        except Exception as exc:  # noqa: BLE001
            logger.debug("reasoners: failed to read %s: %s", key, exc)
            existing = {}
        value: dict[str, Any] = dict(existing)
        by_step = dict(existing.get("reasoners_by_step") or {})
        by_step[step_name] = str(reasoner_id)
        value["reasoners_by_step"] = by_step
        try:
            board_fn(
                "board.put_data",
                {"key": key, "value": value},
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("reasoners: failed to write %s: %s", key, exc)

    def _write_token_record(
        self,
        board_fn: Callable[[str, dict], dict],
        *,
        task_id: str,
        stage: str,
        step_name: str,
        reasoner_id: str,
        tokens_total: int,
        tokens_prompt: int | None = None,
        tokens_completion: int | None = None,
    ) -> None:
        """Write a structured token record to the Board's data store.

        Best-effort: failures are logged at debug level and swallowed.
        Telemetry must never fail a worker turn.
        """
        record = {
            "task_id": task_id,
            "stage": stage,
            "step_name": step_name,
            "reasoner_id": reasoner_id,
            "token_prompt": tokens_prompt,
            "token_completion": tokens_completion,
            "token_total": tokens_total,
            "timestamp": datetime.now(UTC).isoformat(),
        }
        key = f"_token_record:{task_id}:{step_name}"
        try:
            board_fn("board.put_data", {"key": key, "value": record})
        except Exception as exc:  # noqa: BLE001
            logger.debug("token_record: failed to write %s: %s", key, exc)

    # ── Prerequisite validation ─────────────────────────────────────────────

    def _validate_saga_prerequisites(self, saga: BuiltSaga) -> None:
        """Raise if the saga's shape is incompatible with the runtime's
        current registrations.

        Today's only check: a saga with any REASON step requires at
        least one registered reasoner.

        Per-step ``via=`` validation happens at dispatch time in
        ``_run_reason`` rather than here, so an unregistered ``via=``
        only fails when the offending step actually runs. This is
        intentional — saga prerequisites are about saga-shape
        compatibility with the runtime, while per-step validation is
        about runtime state at dispatch.
        """
        has_reason = any(
            s.kind is StepKind.REASON
            or (
                s.kind is StepKind.PARALLEL
                and any(
                    branch_step.kind is StepKind.REASON
                    for branch in s.payload.get("branches", ())
                    for branch_step in branch.steps
                )
            )
            for s in saga.steps
        )
        if has_reason and not self._reasoners:
            raise RuntimeError(
                f"Saga {saga.name!r}: saga has reason steps but no reasoner "
                f"is registered. Call QuadroSagaRuntime.register_reasoner(...) "
                f"before dispatching the saga."
            )

    # ── Reasoner registration ────────────────────────────────────────────────

    def register_reasoner(self, reasoner: Any) -> None:
        """Register a Reasoner implementation under its ``reasoner_id``.

        Multiple reasoners may be registered. Milestone B always uses the
        first registered reasoner for every reason step; milestone G adds
        per-step selection via the ``.reason(via=...)`` parameter.
        """
        rid = getattr(reasoner, "reasoner_id", None)
        if not rid:
            raise ValueError(
                "Reasoner must have a non-empty `reasoner_id` attribute"
            )
        self._reasoners[rid] = reasoner

    # ── Persistence helpers ──────────────────────────────────────────────────

    def _load_or_init_state(
        self,
        saga: BuiltSaga,
        task_id: str,
        ctx: RuntimeContext,
    ) -> SagaState:
        """Load persisted state or build a fresh one anchored at the
        saga's first step."""
        existing = ctx.board_fn(
            "board.get_data",
            {"key": self._state_key(task_id)},
        )
        loaded = SagaState.from_board_data((existing or {}).get("value"))
        if loaded is not None:
            if loaded.saga_name == saga.name:
                self._rehydrate_reason_outputs(saga, loaded)
                return loaded
            # Cross-stage re-initialization: the saga state key is
            # ``_saga:{task_id}`` (no stage component), so a task that
            # progresses from one saga-backed stage to the next will
            # naturally find the previous stage's state under this
            # key. Re-initializing is the correct behaviour — each
            # saga is a stage-local unit of work, and the prior
            # stage's commits live on the task record, not on the
            # saga state. Logged at DEBUG so routine stage transitions
            # don't look like errors in production logs.
            logger.debug(
                "Saga state under %s belongs to %r, not %r — re-initializing",
                self._state_key(task_id),
                loaded.saga_name,
                saga.name,
            )
        return SagaState(
            saga_name=saga.name,
            pc=saga.first_step(),
            started_at=datetime.now(UTC).isoformat(),
        )

    def _rehydrate_reason_outputs(
        self,
        saga: BuiltSaga,
        state: SagaState,
    ) -> None:
        """Re-materialize pydantic outputs after loading from the Board.

        ``to_board_data`` stores a reason step's validated pydantic output
        as its ``model_dump()`` dict form (the board's sqlite backend
        ``json.dumps`` values, which cannot serialize ``BaseModel``). On
        resume, downstream steps still expect the pydantic instance so
        they can use attribute access (e.g. ``ctx.step["x"].field``).
        Reconstruct using the schema declared on each step.

        This is a best-effort rehydration: if the schema is missing, not
        a pydantic class, or validation fails, leave the value as the
        raw dict. That matches the contract for deterministic steps
        (which store JSON-compatible values verbatim) and gracefully
        handles schema evolution across resumes.
        """
        self._rehydrate_reason_outputs_for_steps(saga.name, saga.steps, state)
        for step in saga.steps:
            if step.kind is not StepKind.PARALLEL:
                continue
            branch_states = state.branch_states.get(step.name, {})
            for branch in step.payload.get("branches", ()):
                branch_state = branch_states.get(branch.name)
                if branch_state is not None:
                    self._rehydrate_reason_outputs_for_steps(
                        saga.name,
                        branch.steps,
                        branch_state,
                    )

    def _rehydrate_reason_outputs_for_steps(
        self,
        saga_name: str,
        steps: tuple[Step, ...],
        state: SagaState,
    ) -> None:
        for step_name, raw in list(state.completed_steps.items()):
            if not isinstance(raw, dict):
                continue
            try:
                step = next(s for s in steps if s.name == step_name)
            except StopIteration:
                continue
            if step.kind is not StepKind.REASON:
                continue
            schema = step.payload.get("schema")
            if schema is None:
                continue
            model_validate = getattr(schema, "model_validate", None)
            if not callable(model_validate):
                continue
            try:
                state.completed_steps[step_name] = model_validate(raw)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Saga %r: could not re-validate output of step %r "
                    "against %s on resume (%s); leaving as raw dict.",
                    saga_name,
                    step_name,
                    getattr(schema, "__name__", schema),
                    exc,
                )

    def _persist(self, state: SagaState, ctx: RuntimeContext) -> None:
        """Write state back to the Board. Failures are logged but not
        raised — telemetry must never fail a step. (A persistence
        failure will, however, mean the next worker invocation re-runs
        from the previously-persisted ``pc``, which is the conservative
        choice when retries are idempotent.)"""
        try:
            ctx.board_fn(
                "board.put_data",
                {
                    "key": self._state_key(ctx.task["task_id"]),
                    "value": state.to_board_data(),
                },
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Failed to persist saga state for task %s: %s",
                ctx.task["task_id"],
                exc,
            )

    @staticmethod
    def _state_key(task_id: str) -> str:
        """Convention: saga state lives under ``_saga:{task_id}``."""
        return f"_saga:{task_id}"
