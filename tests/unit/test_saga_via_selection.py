"""
Unit tests for per-step reasoner selection via ``.reason(via=...)``.

Milestone G adds an optional ``via=`` keyword argument to the saga
builder's ``.reason()`` method. When set, the saga runtime's
``_run_reason`` looks up the registered reasoner whose
``reasoner_id`` matches the ``via`` value and dispatches there. When
unset (or explicitly ``None``), the runtime falls back to the first
registered reasoner — preserving every existing call site unchanged.

Uses fake reasoners (two with distinct ``reasoner_id`` values) plus
the same fake-board pattern as ``test_saga_reason_step.py``. No LLM
key, no network.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from pydantic import BaseModel

from quadro.pipeline import StageSpec
from quadro.runtime_plugins.base import RuntimeContext
from quadro.runtime_plugins.saga import QuadroSagaRuntime
from quadro.saga import Saga
from quadro.saga.reasoner import ReasonResult


# ── Fake reasoners ────────────────────────────────────────────────────────────


class _FakeReasoner:
    """Fake Reasoner with configurable ``reasoner_id``. Records every
    ``reason()`` call in ``calls`` so tests can assert which reasoner
    actually ran a given step."""

    def __init__(self, reasoner_id: str) -> None:
        self.reasoner_id = reasoner_id
        self.calls: list[dict[str, Any]] = []
        self.canned_outputs: list[tuple[Any, int]] = []

    def queue(self, output: Any, tokens: int = 100) -> None:
        self.canned_outputs.append((output, tokens))

    async def reason(
        self,
        *,
        prompt: str,
        user_message: str,
        schema: type | None,
        token_reporter: Any,
    ) -> ReasonResult:
        self.calls.append(
            {
                "prompt": prompt,
                "user_message": user_message,
                "schema": schema,
            }
        )
        if not self.canned_outputs:
            raise AssertionError(
                f"FakeReasoner({self.reasoner_id!r}): no canned output queued"
            )
        output, tokens = self.canned_outputs.pop(0)
        if token_reporter is not None and tokens > 0:
            try:
                token_reporter(tokens)
            except Exception:
                pass
        return ReasonResult(output=output, tokens_used=tokens, raw_text=str(output))


# ── Fake board_fn ──────────────────────────────────────────────────────────────


def _fake_board_fn(store: dict) -> Any:
    def _fn(intent: str, payload: dict) -> dict:
        if intent == "board.put_data":
            store[payload["key"]] = payload["value"]
            return {"ok": True}
        if intent == "board.get_data":
            return {"key": payload["key"], "value": store.get(payload["key"])}
        if intent == "board.update_task":
            store.setdefault("_updates", []).append(payload)
            return {"ok": True}
        if intent == "board.get_full_state":
            return {"tasks": store.get("_tasks") or []}
        raise AssertionError(f"unexpected intent: {intent}")

    return _fn


def _ctx(spec: StageSpec, task: dict, store: dict) -> RuntimeContext:
    return RuntimeContext(
        stage=spec,
        task=task,
        context={"payload": {"task": task}},
        board_fn=_fake_board_fn(store),
    )


# ── Tests ─────────────────────────────────────────────────────────────────────


def test_via_dispatches_to_named_reasoner() -> None:
    """Two reasoners registered ('first', 'second'); a reason step
    with ``via="second"`` dispatches to the second reasoner and leaves
    the first untouched."""
    first = _FakeReasoner("first")
    second = _FakeReasoner("second")
    second.queue("routed to second")

    saga = (
        Saga("test")
        .reason(
            "speak",
            prompt="p",
            user_message=lambda ctx: "hi",
            via="second",
        )
        .build()
    )

    runtime = QuadroSagaRuntime()
    runtime.register_reasoner(first)
    runtime.register_reasoner(second)
    spec = StageSpec(capability="x", saga=saga)

    asyncio.run(runtime.run_stage(_ctx(spec, {"task_id": "t1"}, {})))

    assert len(first.calls) == 0
    assert len(second.calls) == 1


def test_via_none_falls_back_to_first_registered() -> None:
    """When ``via`` is not set (or explicitly ``None``), the runtime
    dispatches to the first registered reasoner — the milestone-B
    fallback behaviour, preserved for backward compatibility."""
    first = _FakeReasoner("first")
    second = _FakeReasoner("second")
    first.queue("fallback hit")

    saga = (
        Saga("test").reason("speak", prompt="p", user_message=lambda ctx: "hi").build()
    )

    runtime = QuadroSagaRuntime()
    runtime.register_reasoner(first)
    runtime.register_reasoner(second)
    spec = StageSpec(capability="x", saga=saga)

    asyncio.run(runtime.run_stage(_ctx(spec, {"task_id": "t1"}, {})))

    assert len(first.calls) == 1
    assert len(second.calls) == 0


def test_via_raises_when_reasoner_id_not_registered() -> None:
    """A reason step with ``via="ghost"`` and no "ghost" registered
    fails the saga with ``terminal_reason="step_failed:<step_name>"``.
    The underlying error names both the missing id and the available
    ids so the operator can fix it."""
    real = _FakeReasoner("real")

    saga = (
        Saga("test")
        .reason(
            "speak",
            prompt="p",
            user_message=lambda ctx: "hi",
            via="ghost",
        )
        .build()
    )

    runtime = QuadroSagaRuntime()
    runtime.register_reasoner(real)
    spec = StageSpec(capability="x", saga=saga, failure_status="failed")

    result = asyncio.run(runtime.run_stage(_ctx(spec, {"task_id": "t1"}, {})))
    assert result.terminal_reason == "step_failed:speak"
    # The real reasoner was never invoked.
    assert len(real.calls) == 0


def test_via_works_when_only_one_reasoner_registered() -> None:
    """Edge case: a saga with ``via="only"`` plus a single registered
    reasoner of that id dispatches successfully. The via= path and
    the fallback path produce identical results when there's exactly
    one reasoner."""
    only = _FakeReasoner("only")
    only.queue("ok")

    saga = (
        Saga("test")
        .reason("speak", prompt="p", user_message=lambda ctx: "hi", via="only")
        .build()
    )

    runtime = QuadroSagaRuntime()
    runtime.register_reasoner(only)
    spec = StageSpec(capability="x", saga=saga)

    result = asyncio.run(runtime.run_stage(_ctx(spec, {"task_id": "t1"}, {})))
    assert result.output == "ok"
    assert len(only.calls) == 1


def test_via_persists_across_resume() -> None:
    """A saga with ``via="second"`` on a reason step that has NOT yet
    completed (state.pc points at it, completed_steps is empty for
    that step) resumes by dispatching to "second" — not the fallback
    reasoner. Confirms ``via`` is in the persisted step payload, not
    a transient runtime-local state."""
    first = _FakeReasoner("first")
    second = _FakeReasoner("second")
    second.queue("resumed correctly")

    saga = (
        Saga("test")
        .deterministic("warmup", lambda ctx: "warm")
        .reason(
            "speak",
            prompt="p",
            user_message=lambda ctx: "hi",
            via="second",
        )
        .build()
    )

    runtime = QuadroSagaRuntime()
    runtime.register_reasoner(first)
    runtime.register_reasoner(second)
    spec = StageSpec(capability="x", saga=saga)

    # Pre-populate saga state as if the worker crashed after warmup
    # completed but before speak ran.
    store = {
        "_saga:t1": {
            "saga_name": "test",
            "pc": "speak",
            "idempotency_key": None,
            "completed_steps": {"warmup": "warm"},
            "evidence": {},
            "stamps": [],
            "fork_children": {},
            "waiting_for": None,
            "started_at": None,
            "sla_deadline": None,
            "compensations_run": [],
        }
    }
    asyncio.run(runtime.run_stage(_ctx(spec, {"task_id": "t1"}, store)))

    assert len(first.calls) == 0
    assert len(second.calls) == 1


def test_via_does_not_affect_token_attribution() -> None:
    """Token attribution is reasoner-agnostic — ``_tokens:{task_id}.
    by_stage[<stage>]`` reflects the reasoner's ``tokens_used``
    regardless of which reasoner ran the step."""
    maf_like = _FakeReasoner("maf")
    lc_like = _FakeReasoner("langchain")
    lc_like.queue("out", tokens=42)

    saga = (
        Saga("test")
        .reason(
            "speak",
            prompt="p",
            user_message=lambda ctx: "hi",
            via="langchain",
        )
        .build()
    )

    runtime = QuadroSagaRuntime()
    runtime.register_reasoner(maf_like)
    runtime.register_reasoner(lc_like)
    spec = StageSpec(capability="review", saga=saga)
    store: dict = {}

    asyncio.run(runtime.run_stage(_ctx(spec, {"task_id": "t1"}, store)))

    # _tokens:{task_id} is populated by _bump_stage_tokens regardless
    # of which reasoner ran. The stage capability keys it; the
    # reasoner_id does not enter the tokens.by_stage map.
    tokens_record = store.get("_tokens:t1") or {}
    assert tokens_record.get("by_stage", {}).get("review") == 42


def test_via_records_reasoner_id_in_state() -> None:
    """``state.reasoners_by_step[<reason step name>]`` records which
    reasoner ran the step. Mirrors milestone D's
    ``compensations_run`` pattern — a per-saga map written by the
    runtime, persisted through the existing SagaState round-trip."""
    maf_like = _FakeReasoner("maf")
    lc_like = _FakeReasoner("langchain")
    maf_like.queue("first")
    lc_like.queue("second")

    saga = (
        Saga("test")
        .reason("a", prompt="p", user_message=lambda ctx: "hi")
        .reason("b", prompt="p", user_message=lambda ctx: "hi", via="langchain")
        .build()
    )

    runtime = QuadroSagaRuntime()
    runtime.register_reasoner(maf_like)
    runtime.register_reasoner(lc_like)
    spec = StageSpec(capability="x", saga=saga)
    store: dict = {}

    asyncio.run(runtime.run_stage(_ctx(spec, {"task_id": "t1"}, store)))

    persisted = store["_saga:t1"]
    reasoners_by_step = persisted.get("reasoners_by_step") or {}
    assert reasoners_by_step == {"a": "maf", "b": "langchain"}


def test_builder_rejects_via_with_non_string() -> None:
    """``.reason(via=42)`` raises ``TypeError`` at build time — same
    validation discipline as every other builder parameter."""
    with pytest.raises(TypeError, match="via must be"):
        Saga("test").reason(
            "speak",
            prompt="p",
            user_message=lambda ctx: "hi",
            via=42,  # type: ignore[arg-type]
        )


def test_builder_accepts_via_none_explicitly() -> None:
    """``.reason(via=None)`` is equivalent to ``.reason()`` with no
    ``via=`` argument — the payload omits the ``via`` key entirely,
    keeping the payload shape minimal for the default case."""
    saga_none = (
        Saga("test")
        .reason("speak", prompt="p", user_message=lambda ctx: "hi", via=None)
        .build()
    )
    saga_bare = (
        Saga("test").reason("speak", prompt="p", user_message=lambda ctx: "hi").build()
    )
    # ``via`` is omitted from the payload when unset / explicitly
    # ``None``, keeping the default payload shape minimal. The two
    # builds otherwise share the same payload keys (``user_message``
    # will naturally be a different lambda object between builds,
    # which is why we compare key sets rather than object identity).
    assert "via" not in saga_none.steps[0].payload
    assert "via" not in saga_bare.steps[0].payload
    assert set(saga_none.steps[0].payload.keys()) == set(
        saga_bare.steps[0].payload.keys()
    )


def test_via_routing_does_not_affect_non_reason_steps() -> None:
    """Only reason steps consult ``via=``. Deterministic, gate, guard,
    and other step kinds dispatch identically regardless of which
    reasoners are registered or which ``via=`` is chosen.

    Verified by composing a saga that mixes step kinds, running it,
    and asserting the non-reason steps' side effects happened while
    the fake reasoner's via-selected dispatch ran exactly once."""

    class _Seed(BaseModel):
        value: str

    det_calls: list[str] = []
    gate_routes: list[str] = []
    first = _FakeReasoner("first")
    second = _FakeReasoner("second")
    second.queue(_Seed(value="from second"), tokens=50)

    saga = (
        Saga("test")
        .deterministic("det", lambda ctx: det_calls.append("ran") or "det_ok")
        .reason(
            "speak",
            prompt="p",
            user_message=lambda ctx: "hi",
            schema=_Seed,
            via="second",
        )
        .gate(
            "route",
            when=lambda ctx: gate_routes.append("routed") or True,
            on_true="approved",
            on_false="rejected",
        )
        .deterministic("approved", lambda ctx: "approved_value")
        .deterministic("rejected", lambda ctx: "rejected_value")
        .build()
    )

    runtime = QuadroSagaRuntime()
    runtime.register_reasoner(first)
    runtime.register_reasoner(second)
    spec = StageSpec(capability="x", saga=saga)

    result = asyncio.run(runtime.run_stage(_ctx(spec, {"task_id": "t1"}, {})))

    # Deterministic + gate ran normally — via= had no effect on them.
    assert det_calls == ["ran"]
    assert gate_routes == ["routed"]
    # Reason step dispatched to the via-named reasoner (second) only.
    assert len(first.calls) == 0
    assert len(second.calls) == 1
    # Saga terminated on the chosen branch (gate barrier blocks rejected path).
    assert result.output == "approved_value"
