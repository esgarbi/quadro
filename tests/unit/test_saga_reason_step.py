"""
Unit tests for the .reason() step kind and its dispatch path.

Uses a fake Reasoner that records every invocation and returns canned
outputs. Real reasoner adapters (MafReasoner, LangChainReasoner) are
exercised by integration tests against real LLM providers and by the
newsroom example's end-to-end run.
"""

from __future__ import annotations

import asyncio

# Brief specified `from dataclasses import dataclass, field` here, but
# those imports are unused in the test body and tripped ruff F401.
# Mirrors the milestone-A revision #2 pattern (see
# `.cursor-briefs/milestone-a-revisions.md`): ship ruff-clean rather
# than brief-verbatim when a lint rule fires on otherwise-identical code.
from typing import Any

import pytest
from pydantic import BaseModel

from quadro.pipeline import StageSpec
from quadro.runtime_plugins.base import RuntimeContext
from quadro.runtime_plugins.saga import QuadroSagaRuntime
from quadro.saga import Saga
from quadro.saga.reasoner import ReasonResult


# ── Fake reasoner ──────────────────────────────────────────────────────────────


class _FakeReasoner:
    """In-memory Reasoner implementation that records calls and returns
    canned outputs.

    The fake matches the Reasoner Protocol structurally (duck-typed) —
    no need to import the Protocol class to satisfy isinstance checks
    because Protocol is structural in Python.
    """

    reasoner_id: str = "fake"

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.canned_outputs: list[Any] = []

    def queue(self, output: Any, tokens: int = 100, raw: str = "") -> None:
        """Queue a canned output for the next reason() call."""
        self.canned_outputs.append((output, tokens, raw or str(output)))

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
                f"FakeReasoner: reason() called but no canned output queued "
                f"(call #{len(self.calls)})"
            )
        output, tokens, raw = self.canned_outputs.pop(0)
        if token_reporter is not None and tokens > 0:
            try:
                token_reporter(tokens)
            except Exception:
                pass
        return ReasonResult(output=output, tokens_used=tokens, raw_text=raw)


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


# ── Schemas used by the tests ──────────────────────────────────────────────────


class _SimpleOutput(BaseModel):
    """Minimal pydantic schema used to verify schema validation works."""

    value: str


# ── Tests ──────────────────────────────────────────────────────────────────────


def test_reason_step_dispatches_to_reasoner_and_stores_output() -> None:
    """A reason step calls the registered reasoner once, passing the
    resolved prompt and user_message, and stores the validated output
    under state.completed_steps[step_name]."""
    reasoner = _FakeReasoner()
    reasoner.queue(_SimpleOutput(value="hello"))

    saga = (
        Saga("test")
        .reason(
            "speak",
            prompt="say something",
            user_message=lambda ctx: "give me a string",
            schema=_SimpleOutput,
        )
        .build()
    )

    runtime = QuadroSagaRuntime()
    runtime.register_reasoner(reasoner)
    spec = StageSpec(capability="x", saga=saga)
    store: dict = {}

    result = asyncio.run(runtime.run_stage(_ctx(spec, {"task_id": "t1"}, store)))

    # Reasoner was called exactly once with the right inputs.
    assert len(reasoner.calls) == 1
    call = reasoner.calls[0]
    assert call["prompt"] == "say something"
    assert call["user_message"] == "give me a string"
    assert call["schema"] is _SimpleOutput

    # Output was stored and surfaced as the saga's final output.
    assert isinstance(result.output, _SimpleOutput)
    assert result.output.value == "hello"


def test_reason_step_resolves_prompt_path_to_file_contents(tmp_path) -> None:
    """When prompt is a Path, the runtime reads the file and passes the
    contents to the reasoner."""
    prompt_file = tmp_path / "prompt.md"
    prompt_file.write_text("loaded from disk\n")

    reasoner = _FakeReasoner()
    reasoner.queue("ok")

    saga = (
        Saga("test")
        .reason(
            "speak",
            prompt=prompt_file,
            user_message=lambda ctx: "hi",
        )
        .build()
    )

    runtime = QuadroSagaRuntime()
    runtime.register_reasoner(reasoner)
    spec = StageSpec(capability="x", saga=saga)

    asyncio.run(runtime.run_stage(_ctx(spec, {"task_id": "t1"}, {})))

    assert reasoner.calls[0]["prompt"] == "loaded from disk\n"


def test_reason_step_serializes_dict_user_message_to_json() -> None:
    """When the user_message lambda returns a dict, the runtime
    json-serializes it before passing to the reasoner. When it returns
    a string, the string is passed through unchanged."""
    reasoner = _FakeReasoner()
    reasoner.queue("ok")

    saga = (
        Saga("test")
        .reason(
            "speak",
            prompt="p",
            user_message=lambda ctx: {"topic": "x", "items": [1, 2, 3]},
        )
        .build()
    )

    runtime = QuadroSagaRuntime()
    runtime.register_reasoner(reasoner)
    spec = StageSpec(capability="x", saga=saga)

    asyncio.run(runtime.run_stage(_ctx(spec, {"task_id": "t1"}, {})))

    # The dict was JSON-serialized before reaching the reasoner.
    msg = reasoner.calls[0]["user_message"]
    assert isinstance(msg, str)
    import json

    parsed = json.loads(msg)
    assert parsed == {"topic": "x", "items": [1, 2, 3]}


def test_reason_step_user_message_lambda_sees_earlier_steps() -> None:
    """The user_message lambda receives a SagaContext whose .step dict
    contains outputs from all previously-completed steps."""
    reasoner = _FakeReasoner()
    reasoner.queue("first_response")
    reasoner.queue("second_response")

    saga = (
        Saga("test")
        .reason("first", prompt="p1", user_message=lambda ctx: "go")
        .reason(
            "second",
            prompt="p2",
            user_message=lambda ctx: f"echo: {ctx.step['first']}",
        )
        .build()
    )

    runtime = QuadroSagaRuntime()
    runtime.register_reasoner(reasoner)
    spec = StageSpec(capability="x", saga=saga)

    asyncio.run(runtime.run_stage(_ctx(spec, {"task_id": "t1"}, {})))

    # Second call's user_message references the first call's output.
    assert reasoner.calls[1]["user_message"] == "echo: first_response"


def test_reason_step_resumes_without_re_invoking_reasoner() -> None:
    """If a reason step has already completed (its output is in the
    persisted state), the runtime skips it on resume — the reasoner is
    not called a second time."""
    reasoner = _FakeReasoner()
    reasoner.queue("only_called_for_step_b")

    saga = (
        Saga("test")
        .reason("a", prompt="p", user_message=lambda ctx: "hi")
        .reason("b", prompt="p", user_message=lambda ctx: "hi")
        .build()
    )

    runtime = QuadroSagaRuntime()
    runtime.register_reasoner(reasoner)
    spec = StageSpec(capability="x", saga=saga)

    # Pre-populate the store as if step a had completed in an earlier
    # worker invocation that crashed before step b ran.
    store = {
        "_saga:t1": {
            "saga_name": "test",
            "pc": "b",
            "idempotency_key": None,
            "completed_steps": {"a": "first_response_from_previous_run"},
            "evidence": {},
            "stamps": [],
            "fork_children": {},
            "waiting_for": None,
            "started_at": None,
            "sla_deadline": None,
        }
    }

    asyncio.run(runtime.run_stage(_ctx(spec, {"task_id": "t1"}, store)))

    # The reasoner was called exactly once — for step b, not step a.
    assert len(reasoner.calls) == 1


def test_reason_step_without_schema_returns_raw_text() -> None:
    """When schema=None, the reasoner returns raw cleaned text rather
    than a validated pydantic instance."""
    reasoner = _FakeReasoner()
    reasoner.queue("just a string", tokens=42)

    saga = (
        Saga("test").reason("speak", prompt="p", user_message=lambda ctx: "hi").build()
    )

    runtime = QuadroSagaRuntime()
    runtime.register_reasoner(reasoner)
    spec = StageSpec(capability="x", saga=saga)

    result = asyncio.run(runtime.run_stage(_ctx(spec, {"task_id": "t1"}, {})))

    assert result.output == "just a string"
    assert reasoner.calls[0]["schema"] is None


def test_runtime_raises_when_reason_step_has_no_reasoner_registered() -> None:
    """Dispatching a reason step against a runtime with no registered
    reasoner raises a clear error rather than silently doing nothing."""
    saga = (
        Saga("test").reason("speak", prompt="p", user_message=lambda ctx: "hi").build()
    )

    runtime = QuadroSagaRuntime()
    # No reasoner registered.
    spec = StageSpec(capability="x", saga=saga)

    with pytest.raises(RuntimeError, match="no reasoner"):
        asyncio.run(runtime.run_stage(_ctx(spec, {"task_id": "t1"}, {})))


def test_runtime_can_register_multiple_reasoners_by_id() -> None:
    """Multiple reasoners can be registered by reasoner_id. Milestone B
    only uses the first/default; milestone G adds the per-step `via=`
    selector."""
    reasoner_a = _FakeReasoner()
    reasoner_a.reasoner_id = "alpha"
    reasoner_a.queue("from_alpha")

    runtime = QuadroSagaRuntime()
    runtime.register_reasoner(reasoner_a)

    reasoner_b = _FakeReasoner()
    reasoner_b.reasoner_id = "beta"
    runtime.register_reasoner(reasoner_b)

    saga = (
        Saga("test").reason("speak", prompt="p", user_message=lambda ctx: "hi").build()
    )
    spec = StageSpec(capability="x", saga=saga)

    asyncio.run(runtime.run_stage(_ctx(spec, {"task_id": "t1"}, {})))

    # The first registered reasoner handled the call; the second was
    # not invoked.
    assert len(reasoner_a.calls) == 1
    assert len(reasoner_b.calls) == 0


def test_builder_rejects_reason_with_neither_prompt_str_nor_path() -> None:
    """The .reason() builder validates the prompt parameter at build
    time — must be str or Path."""
    with pytest.raises(TypeError, match="prompt must be"):
        (Saga("test").reason("bad", prompt=123, user_message=lambda ctx: "hi"))


def test_builder_rejects_reason_with_non_callable_user_message() -> None:
    """The .reason() builder validates user_message at build time."""
    with pytest.raises(TypeError, match="user_message must be callable"):
        (Saga("test").reason("bad", prompt="p", user_message="not a callable"))
