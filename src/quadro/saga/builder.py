"""
Fluent builder for sagas.

The builder is mutable while you compose; ``build()`` returns a frozen
``BuiltSaga``. Modifier methods (``.retry()``, ``.deadline()``, ...)
attach to the most recently added step by remembering a
``_current_step`` pointer — same pattern as
``LifecycleBuilder.phase()`` / ``.revision()`` / ``.branch()`` already
use elsewhere in the codebase.

Milestone A exposes:

  - ``.deterministic(name, fn)`` — pure-Python step dispatched by the runner
  - ``.compensate(step_name, undo)`` — registers a compensation (stored,
    not yet wired; milestone D activates it)
  - ``.idempotent(by=...)`` — saga-wide idempotency key template (stored,
    not yet wired; milestone F uses it for fork-child dedup)
  - ``.build()`` — produces a frozen ``BuiltSaga``

Milestone B adds:

  - ``.reason(name, prompt=..., user_message=..., schema=...)`` — a single
    LLM reasoning episode dispatched through the registered ``Reasoner``

Milestone C adds the bulk of the step-kind vocabulary:

  - ``.gate(name, when=..., on_true=..., on_false=...)`` — predicate-driven
    branching; stores the chosen branch name for audit
  - ``.guard(name, check=...)`` — pre-condition that halts the saga on
    failure with a distinct telemetry event
  - ``.expect(name, invariant=...)`` — post-condition with the same
    halt-on-failure shape but a separate event type
  - ``.evidence(name, capture=...)`` — best-effort audit-record capture
    merged into ``state.evidence``; never fails the saga
  - ``.stamp(name, capture=...)`` — appends a timestamped record to
    ``state.stamps``

Milestone C also adds the first two cross-cutting modifiers, attached
via ``_current_step`` after the step they decorate:

  - ``.retry(attempts=..., on=..., backoff=...)``
  - ``.deadline(within=...)``

Milestone E adds ``.parallel(...)`` as the in-saga concurrency primitive.
Future step kinds, if any, follow the same shape: append a Step, optionally
update ``_current_step``, return ``self``.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from typing import Any

from .saga import BuiltSaga
from .steps import BuiltBranch, Step, StepKind


class SagaBuilder:
    """Fluent builder for assembling a saga.

    Usage::

        saga = (
            SagaBuilder("ideation")
            .deterministic("collect_avoid_list", gather_titles)
            .deterministic("persist_brief", write_brief_to_task)
            .compensate("persist_brief", undo=clear_brief)
            .build()
        )

    User-facing code typically imports the ``Saga`` alias from
    ``quadro.saga`` and writes ``Saga("ideation").deterministic(...)``;
    the alias points at this class. The frozen result of ``build()`` is
    a ``BuiltSaga`` instance.
    """

    def __init__(self, name: str, *, _branch_mode: bool = False) -> None:
        if not name:
            raise ValueError("Saga name must be a non-empty string")
        self._name = name
        self._branch_mode = _branch_mode
        self._steps: list[Step] = []
        self._current_step: str | None = None
        self._saga_modifiers: dict[str, Any] = {}
        self._compensations: dict[str, dict[str, Any]] = {}

    # ── Step additions ───────────────────────────────────────────────────────

    def deterministic(
        self,
        name: str,
        fn: Callable[..., Any],
    ) -> SagaBuilder:
        """Add a deterministic step — pure Python, no LLM, no external dependency.

        ``fn`` is called as ``fn(ctx)`` where ``ctx`` is a ``SagaContext``.
        The return value is stored under ``state.completed_steps[name]``
        and must be JSON-compatible (dict, list, str, int, float, bool,
        None).
        """
        self._reject_duplicate_name(name)
        self._steps.append(Step(name=name, kind=StepKind.DETERMINISTIC, payload={"fn": fn}))
        self._current_step = name
        return self

    def reason(
        self,
        name: str,
        *,
        prompt: str | Path,
        user_message: Callable[[Any], Any],
        schema: type | None = None,
        via: str | None = None,
    ) -> SagaBuilder:
        """Add a reason step — a single LLM reasoning episode.

        The runtime resolves ``prompt`` to a string at dispatch time
        (reading the file if a Path is provided). It then calls
        ``user_message(ctx)`` with the current SagaContext to build the
        user-role message body; if the callable returns a dict, the
        runtime JSON-serializes it. The resulting prompt and message are
        handed to the registered ``Reasoner``, whose ``reason()`` method
        actually invokes the model.

        When ``schema`` is provided, the reasoner validates the model's
        output against the pydantic schema and the validated instance is
        stored in ``state.completed_steps[name]``. When ``schema`` is
        None, the cleaned raw text is stored instead.

        Parameters
        ----------
        name:
            Step name. Must be unique within the saga.
        prompt:
            Either a ``Path`` to a prompt file (read at dispatch time) or
            the prompt text directly as a string.
        user_message:
            Callable invoked with the current ``SagaContext``. Must return
            a dict (which the runtime JSON-serializes) or a string (used
            as-is).
        schema:
            Optional pydantic ``BaseModel`` subclass. When provided, the
            reasoner validates the model output and stores the validated
            instance.
        via:
            Optional ``reasoner_id`` naming which registered reasoner
            should execute this step. When ``None`` (the default), the
            runtime dispatches to the first registered reasoner —
            preserving every existing call site unchanged. When set,
            the runtime looks up a reasoner with this exact
            ``reasoner_id`` (e.g. ``"maf"``, ``"langchain"``) and
            dispatches to it; if no such reasoner is registered, the
            saga fails with
            ``terminal_reason="step_failed:<step_name>"`` and a clear
            diagnostic.

            Use ``via=`` for sagas that need polyglot reasoning — one
            step through MAF, another through LangChain, etc. —
            within the same pipeline. Milestone G introduced the
            mechanism.
        """
        self._reject_duplicate_name(name)

        if not isinstance(prompt, (str, Path)):
            raise TypeError(
                f"Saga {self._name!r} step {name!r}: prompt must be a str "
                f"or pathlib.Path, got {type(prompt).__name__}"
            )
        if not callable(user_message):
            raise TypeError(
                f"Saga {self._name!r} step {name!r}: user_message must be "
                f"callable, got {type(user_message).__name__}"
            )
        if schema is not None and not isinstance(schema, type):
            raise TypeError(
                f"Saga {self._name!r} step {name!r}: schema must be a "
                f"class (typically a pydantic BaseModel subclass), got "
                f"{type(schema).__name__}"
            )
        if via is not None and not isinstance(via, str):
            raise TypeError(
                f"Saga {self._name!r} step {name!r}: via must be a str "
                f"or None, got {type(via).__name__}"
            )

        payload: dict[str, Any] = {
            "prompt": prompt,
            "user_message": user_message,
            "schema": schema,
        }
        # Only attach ``via`` when set — keeps the default payload shape
        # minimal (``via not in payload`` is meaningful to callers that
        # inspect the built saga).
        if via is not None:
            payload["via"] = via

        self._steps.append(
            Step(
                name=name,
                kind=StepKind.REASON,
                payload=payload,
            )
        )
        self._current_step = name
        return self

    def gate(
        self,
        name: str,
        *,
        when: Callable[[Any], bool],
        on_true: str,
        on_false: str,
    ) -> SagaBuilder:
        """Add a gate step — branches saga execution based on a predicate.

        The runtime calls ``when(ctx)``. If True, ``pc`` jumps to
        ``on_true``; if False, ``pc`` jumps to ``on_false``. Both
        targets must be the name of a step declared elsewhere in the
        same saga (validated at ``build()`` time because forward
        references are allowed).

        The gate's stored output is ``{"chosen": "<branch_name>"}``,
        recorded in ``state.completed_steps[name]``, so the routing
        decision is visible to telemetry and audit without re-evaluating
        the predicate on resume.
        """
        self._reject_duplicate_name(name)
        if not callable(when):
            raise TypeError(
                f"Saga {self._name!r} step {name!r}: when must be callable, "
                f"got {type(when).__name__}"
            )
        if not isinstance(on_true, str) or not isinstance(on_false, str):
            raise TypeError(
                f"Saga {self._name!r} step {name!r}: on_true and on_false "
                f"must both be step-name strings"
            )
        self._steps.append(
            Step(
                name=name,
                kind=StepKind.GATE,
                payload={
                    "when": when,
                    "on_true": on_true,
                    "on_false": on_false,
                },
            )
        )
        self._current_step = name
        return self

    def guard(
        self,
        name: str,
        *,
        check: Callable[[Any], bool],
    ) -> SagaBuilder:
        """Add a guard step — pre-condition that halts the saga on failure.

        The runtime calls ``check(ctx)``. If True, the saga continues
        to the next step. If False, the saga halts with
        ``terminal_reason="guard_failed:<name>"`` and the
        StageSpec's ``failure_status`` is used as the resulting task
        status. Emits a ``saga.guard_failed`` telemetry event.
        """
        self._reject_duplicate_name(name)
        if not callable(check):
            raise TypeError(
                f"Saga {self._name!r} step {name!r}: check must be callable, "
                f"got {type(check).__name__}"
            )
        self._steps.append(
            Step(
                name=name,
                kind=StepKind.GUARD,
                payload={"check": check},
            )
        )
        self._current_step = name
        return self

    def expect(
        self,
        name: str,
        *,
        invariant: Callable[[Any], bool],
    ) -> SagaBuilder:
        """Add an expect step — post-condition that halts the saga on failure.

        Identical machinery to ``.guard()``, but emits a distinct
        telemetry event type (``saga.expect_failed`` vs
        ``saga.guard_failed``) so audit queries can distinguish
        pre-conditions from post-conditions.
        """
        self._reject_duplicate_name(name)
        if not callable(invariant):
            raise TypeError(
                f"Saga {self._name!r} step {name!r}: invariant must be "
                f"callable, got {type(invariant).__name__}"
            )
        self._steps.append(
            Step(
                name=name,
                kind=StepKind.EXPECT,
                payload={"invariant": invariant},
            )
        )
        self._current_step = name
        return self

    def evidence(
        self,
        name: str,
        *,
        capture: Callable[[Any], Any],
    ) -> SagaBuilder:
        """Add an evidence step — best-effort audit-record capture.

        The runtime calls ``capture(ctx)``; the returned value is merged
        into ``state.evidence[name]`` and persisted to the board.
        Failures in the capture callable are logged and ignored —
        evidence never fails a saga.
        """
        self._reject_duplicate_name(name)
        if not callable(capture):
            raise TypeError(
                f"Saga {self._name!r} step {name!r}: capture must be "
                f"callable, got {type(capture).__name__}"
            )
        self._steps.append(
            Step(
                name=name,
                kind=StepKind.EVIDENCE,
                payload={"capture": capture},
            )
        )
        self._current_step = name
        return self

    def stamp(
        self,
        name: str,
        *,
        capture: Callable[[Any], Any],
    ) -> SagaBuilder:
        """Add a stamp step — append a signed, timestamped record to
        ``state.stamps``.

        Stamps are ordered audit markers (version numbers, release tags,
        revision counts). The runtime captures
        ``{"key": name, "value": capture(ctx), "timestamp": <utc-iso>}``
        and appends it to ``state.stamps`` in declaration order.
        """
        self._reject_duplicate_name(name)
        if not callable(capture):
            raise TypeError(
                f"Saga {self._name!r} step {name!r}: capture must be "
                f"callable, got {type(capture).__name__}"
            )
        self._steps.append(
            Step(
                name=name,
                kind=StepKind.STAMP,
                payload={"capture": capture},
            )
        )
        self._current_step = name
        return self

    def parallel(
        self,
        name: str,
        *,
        branches: list[Callable[[SagaBuilder], SagaBuilder]],
        join: str | tuple[str, int] = "all",
    ) -> SagaBuilder:
        """Add a parallel step to the saga.

        Each branch is declared by calling its factory with a fresh
        branch-local ``SagaBuilder``. Milestone E supports existing step
        kinds inside branches and rejects nested parallel steps at build
        time.
        """
        self._reject_duplicate_name(name)
        if not branches:
            raise ValueError(
                f"Saga {self._name!r} parallel step {name!r}: branches "
                f"list must be non-empty"
            )
        if not all(callable(b) for b in branches):
            raise TypeError(
                f"Saga {self._name!r} parallel step {name!r}: every branch "
                f"must be a callable taking a SagaBuilder"
            )

        if isinstance(join, str):
            if join not in {"all", "any"}:
                raise ValueError(
                    f"Saga {self._name!r} parallel step {name!r}: join "
                    f"must be 'all', 'any', or ('n_of_m', n=N); got {join!r}"
                )
            join_normalized: str | tuple[str, int] = join
        elif isinstance(join, tuple) and len(join) == 2 and join[0] == "n_of_m":
            n = join[1]
            if not isinstance(n, int) or n < 1:
                raise ValueError(
                    f"Saga {self._name!r} parallel step {name!r}: n_of_m "
                    f"threshold must be a positive int; got n={n!r}"
                )
            if n > len(branches):
                raise ValueError(
                    f"Saga {self._name!r} parallel step {name!r}: n_of_m "
                    f"threshold {n} exceeds branch count {len(branches)}"
                )
            join_normalized = ("n_of_m", n)
        else:
            raise ValueError(
                f"Saga {self._name!r} parallel step {name!r}: invalid join "
                f"specification; expected 'all', 'any', or "
                f"('n_of_m', n=N); got {join!r}"
            )

        built_branches: list[BuiltBranch] = []
        seen_branch_names: set[str] = set()
        for i, factory in enumerate(branches):
            sub_builder = SagaBuilder._for_branch(saga_name=f"{self._name}.{name}")
            configured = factory(sub_builder)
            if configured is None:
                raise TypeError(
                    f"Saga {self._name!r} parallel step {name!r} branch {i}: "
                    f"factory must return the configured SagaBuilder"
                )
            if not isinstance(configured, SagaBuilder):
                raise TypeError(
                    f"Saga {self._name!r} parallel step {name!r} branch {i}: "
                    f"factory must return a SagaBuilder"
                )
            branch = configured.build_branch()
            if branch.name in seen_branch_names:
                raise ValueError(
                    f"Saga {self._name!r} parallel step {name!r}: duplicate "
                    f"branch name {branch.name!r}"
                )
            seen_branch_names.add(branch.name)
            built_branches.append(branch)

        self._steps.append(
            Step(
                name=name,
                kind=StepKind.PARALLEL,
                payload={
                    "branches": tuple(built_branches),
                    "join": join_normalized,
                },
            )
        )
        self._current_step = name
        return self

    # ── Step modifiers ───────────────────────────────────────────────────────

    def retry(
        self,
        *,
        attempts: int,
        on: tuple[type[BaseException], ...] = (Exception,),
        backoff: str = "fixed",
    ) -> SagaBuilder:
        """Attach a retry policy to the most recently added step.

        Wraps that step's dispatch in a retry loop: up to ``attempts``
        invocations, intercepting exceptions whose type appears in
        ``on``. Other exceptions propagate immediately.

        ``backoff="fixed"`` inserts no delay between retries;
        ``backoff="exponential"`` inserts 1s, 2s, 4s, ... capped at
        30 seconds. Future backoff strategies can be added without
        widening the public surface — unrecognised values are rejected
        at build time so a typo fails loudly.
        """
        if self._current_step is None:
            raise ValueError(
                f"Saga {self._name!r}: .retry() called before any step was added"
            )
        if attempts < 1:
            raise ValueError(
                f"Saga {self._name!r}: .retry() requires attempts >= 1, "
                f"got {attempts!r}"
            )
        if backoff not in {"fixed", "exponential"}:
            raise ValueError(
                f"Saga {self._name!r}: unknown backoff {backoff!r}; "
                f"must be 'fixed' or 'exponential'"
            )
        last = self._steps[-1]
        new_modifiers = dict(last.modifiers)
        new_modifiers["retry"] = {
            "attempts": attempts,
            "on": tuple(on),
            "backoff": backoff,
        }
        self._steps[-1] = Step(
            name=last.name,
            kind=last.kind,
            payload=last.payload,
            modifiers=new_modifiers,
        )
        return self

    def deadline(
        self,
        *,
        within: timedelta,
    ) -> SagaBuilder:
        """Attach a wall-clock deadline to the most recently added step.

        Wraps that step's dispatch in
        ``asyncio.wait_for(..., within.total_seconds())``. On timeout,
        the saga halts with ``terminal_reason="deadline_exceeded:<step>"``.

        When combined with ``.retry()``, each retry attempt gets its own
        deadline window — the deadline is per-attempt, not cumulative.
        """
        if self._current_step is None:
            raise ValueError(
                f"Saga {self._name!r}: .deadline() called before any step was added"
            )
        if within.total_seconds() <= 0:
            raise ValueError(
                f"Saga {self._name!r}: .deadline(within=...) must be positive, "
                f"got {within!r}"
            )
        last = self._steps[-1]
        new_modifiers = dict(last.modifiers)
        new_modifiers["deadline"] = {"seconds": within.total_seconds()}
        self._steps[-1] = Step(
            name=last.name,
            kind=last.kind,
            payload=last.payload,
            modifiers=new_modifiers,
        )
        return self

    # ── Saga-level modifiers ─────────────────────────────────────────────────

    def idempotent(self, *, by: str) -> SagaBuilder:
        """Declare a saga-wide idempotency key template.

        ``by`` is the name of a field on the task (e.g. ``"order_id"``)
        whose value is interpolated into the saga's idempotency key at
        run time. Stored in milestone A; first used by the fork-child
        dedup logic in milestone F.
        """
        self._saga_modifiers["idempotent_by"] = by
        return self

    # ── Compensation registration ────────────────────────────────────────────

    def compensate(
        self,
        step_name: str,
        *,
        undo: Callable[..., Any],
        on_failure: str = "continue",
    ) -> SagaBuilder:
        """Register a compensation handler for a step.

        The runtime invokes ``undo(ctx)`` during rollback when
        ``step_name`` has completed but a later step raised. ``ctx`` is
        a ``SagaContext`` populated with the step's output in
        ``ctx.step[step_name]`` so the compensation can read what was
        done and undo it precisely.

        Parameters
        ----------
        step_name:
            The step whose side effect this compensation undoes. Must
            reference a step declared elsewhere in the same saga
            (validated at ``build()`` time).
        undo:
            The compensation function. Receives a ``SagaContext``, returns
            None (return value is ignored). Should be idempotent — the
            runtime makes no guarantee it won't be invoked twice if a
            worker crashes mid-rollback and resumes.
        on_failure:
            Either ``"continue"`` (default — the rollback walker logs
            the failure and proceeds with earlier compensations) or
            ``"halt"`` (the rollback walker stops; remaining
            compensations are NOT invoked; the saga's terminal_reason
            becomes ``"compensation_failed:<step_name>"``). The default
            matches operator expectations from production saga
            frameworks; ``"halt"`` is the opt-in for compensations that
            depend on earlier compensations succeeding.
        """
        if on_failure not in ("continue", "halt"):
            raise ValueError(
                f"Saga {self._name!r}: compensate({step_name!r}) on_failure "
                f"must be 'continue' or 'halt', got {on_failure!r}"
            )
        self._compensations[step_name] = {
            "undo": undo,
            "on_failure": on_failure,
        }
        return self

    # ── Build ────────────────────────────────────────────────────────────────

    def build(self) -> BuiltSaga:
        """Return the frozen ``BuiltSaga``. The builder is single-use;
        calling ``build()`` more than once is allowed but discouraged."""
        if not self._steps:
            raise ValueError(f"Saga {self._name!r} has no steps")
        step_names = {s.name for s in self._steps}
        for comp_name in self._compensations:
            if comp_name not in step_names:
                raise ValueError(
                    f"Saga {self._name!r}: compensate({comp_name!r}) references "
                    f"a step that was never declared"
                )
        # Validate gate routing targets. Forward references are allowed
        # (the gate may appear before its on_true / on_false targets in
        # declaration order), which is why this check runs at build()
        # time rather than inside ``.gate()``.
        for step in self._steps:
            if step.kind is StepKind.GATE:
                for target_key in ("on_true", "on_false"):
                    target = step.payload.get(target_key)
                    if target not in step_names:
                        raise ValueError(
                            f"Saga {self._name!r} gate {step.name!r}: "
                            f"{target_key}={target!r} references a step that "
                            f"was never declared"
                        )
            if step.kind is StepKind.PARALLEL:
                self._reject_nested_parallel(step)
        return BuiltSaga(
            name=self._name,
            steps=tuple(self._steps),
            saga_modifiers=dict(self._saga_modifiers),
            compensations=dict(self._compensations),
        )

    # ── Internals ────────────────────────────────────────────────────────────

    @classmethod
    def _for_branch(cls, *, saga_name: str) -> SagaBuilder:
        return cls(saga_name, _branch_mode=True)

    def build_branch(self) -> BuiltBranch:
        """Internal extractor for branches declared inside ``.parallel()``."""
        if not self._branch_mode:
            raise RuntimeError("build_branch() is only valid for parallel branch builders")
        if not self._steps:
            raise ValueError(f"Saga {self._name!r}: parallel branch must be non-empty")
        step_names = {s.name for s in self._steps}
        for comp_name in self._compensations:
            if comp_name not in step_names:
                raise ValueError(
                    f"Saga {self._name!r}: compensate({comp_name!r}) references "
                    f"a step that was never declared"
                )
        for step in self._steps:
            if step.kind is StepKind.GATE:
                for target_key in ("on_true", "on_false"):
                    target = step.payload.get(target_key)
                    if target not in step_names:
                        raise ValueError(
                            f"Saga {self._name!r} gate {step.name!r}: "
                            f"{target_key}={target!r} references a step that "
                            f"was never declared"
                        )
            if step.kind is StepKind.PARALLEL:
                raise ValueError(
                    f"Saga {self._name!r}: nested parallel steps are not "
                    f"supported in milestone E"
                )
        return BuiltBranch(
            name=self._steps[0].name,
            steps=tuple(self._steps),
            compensations=dict(self._compensations),
        )

    def _reject_nested_parallel(self, step: Step) -> None:
        for branch in step.payload.get("branches", ()):
            for branch_step in branch.steps:
                if branch_step.kind is StepKind.PARALLEL:
                    raise ValueError(
                        f"Saga {self._name!r} parallel step {step.name!r}: "
                        f"nested parallel steps are not supported in milestone E"
                    )

    def _reject_duplicate_name(self, name: str) -> None:
        """Step names must be unique within a saga — step name is the
        primary key for telemetry, persistence, and runner dispatch."""
        if any(s.name == name for s in self._steps):
            raise ValueError(
                f"Saga {self._name!r}: duplicate step name {name!r}"
            )
