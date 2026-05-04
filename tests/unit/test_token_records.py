from __future__ import annotations

import asyncio
import time
from typing import Any

from quadro import BoardClient, LocalA2ANetwork, QuadroBoard
from quadro.board.backends.sqlite import SqliteBoardBackend
from quadro.pipeline import StageSpec
from quadro.runtime_plugins.base import RuntimeContext
from quadro.runtime_plugins.saga import QuadroSagaRuntime
from quadro.saga import Saga
from quadro.saga.reasoner import ReasonResult


class _FakeReasoner:
    def __init__(self, reasoner_id: str | None = "fake") -> None:
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
            raise AssertionError("FakeReasoner: no canned output queued")
        output, tokens = self.canned_outputs.pop(0)
        if token_reporter is not None and tokens > 0:
            try:
                token_reporter(tokens)
            except Exception:
                pass
        return ReasonResult(output=output, tokens_used=tokens, raw_text=str(output))


def _fake_board_fn(store: dict) -> Any:
    def _fn(intent: str, payload: dict) -> dict:
        if intent == "board.put_data":
            key = payload["key"]
            if key.startswith("_token_record:") and store.get("_fail_token_record_put"):
                raise RuntimeError("token record write failed")
            if key.startswith("_saga:") and store.get("_fail_saga_persist"):
                raise RuntimeError("saga persist failed")
            store[key] = payload["value"]
            return {"key": key}
        if intent == "board.get_data":
            return {"key": payload["key"], "value": store.get(payload["key"])}
        if intent == "board.update_task":
            store.setdefault("_updates", []).append(payload)
            return {"ok": True}
        if intent == "board.get_full_state":
            return {"tasks": store.get("_tasks") or []}
        raise AssertionError(f"unexpected intent: {intent}")

    return _fn


def _ctx(
    spec: StageSpec,
    task: dict,
    *,
    store: dict | None = None,
    client: BoardClient | None = None,
) -> RuntimeContext:
    if client is not None:
        board_fn = client.request
    else:
        board_fn = _fake_board_fn({} if store is None else store)
    return RuntimeContext(
        stage=spec,
        task=task,
        context={"payload": {"task": task}},
        board_fn=board_fn,
    )


def _client() -> BoardClient:
    network = LocalA2ANetwork()
    board = QuadroBoard(
        SqliteBoardBackend(":memory:"),
        network=network,
        url="a2a://board",
    )
    return board.client()


def _run_with_fake_store(
    saga,
    reasoner: _FakeReasoner,
    *,
    task_id: str = "t1",
    stage: str = "research",
    store: dict | None = None,
) -> tuple[Any, dict]:
    runtime = QuadroSagaRuntime()
    runtime.register_reasoner(reasoner)
    actual_store = {} if store is None else store
    spec = StageSpec(capability=stage, saga=saga, failure_status="failed")
    result = asyncio.run(
        runtime.run_stage(_ctx(spec, {"task_id": task_id}, store=actual_store))
    )
    return result, actual_store


def _run_with_client(
    saga,
    reasoners: list[_FakeReasoner],
    client: BoardClient,
    *,
    task_id: str = "t1",
    stage: str = "research",
) -> Any:
    runtime = QuadroSagaRuntime()
    for reasoner in reasoners:
        runtime.register_reasoner(reasoner)
    spec = StageSpec(capability=stage, saga=saga, failure_status="failed")
    return asyncio.run(
        runtime.run_stage(_ctx(spec, {"task_id": task_id}, client=client))
    )


def test_record_written_after_successful_reason_step() -> None:
    reasoner = _FakeReasoner("maf")
    reasoner.queue("ok", tokens=37)
    saga = (
        Saga("test").reason("draft", prompt="p", user_message=lambda ctx: "u").build()
    )

    result, store = _run_with_fake_store(saga, reasoner)

    assert result.output == "ok"
    record = store["_token_record:t1:draft"]
    assert record["task_id"] == "t1"
    assert record["stage"] == "research"
    assert record["step_name"] == "draft"
    assert record["reasoner_id"] == "maf"
    assert record["token_prompt"] is None
    assert record["token_completion"] is None
    assert record["token_total"] == 37
    assert isinstance(record["timestamp"], str)


def test_record_overwrites_on_resume() -> None:
    reasoner = _FakeReasoner("maf")
    reasoner.queue("first", tokens=11)
    reasoner.queue("second", tokens=12)
    saga = (
        Saga("test").reason("draft", prompt="p", user_message=lambda ctx: "u").build()
    )
    store = {"_fail_saga_persist": True}

    _run_with_fake_store(saga, reasoner, store=store)
    first_record = dict(store["_token_record:t1:draft"])
    time.sleep(0.001)
    store["_fail_saga_persist"] = False
    _run_with_fake_store(saga, reasoner, store=store)

    records = [key for key in store if key.startswith("_token_record:t1:")]
    assert records == ["_token_record:t1:draft"]
    record = store["_token_record:t1:draft"]
    assert record["token_total"] == 12
    assert record["timestamp"] != first_record["timestamp"]


def test_no_record_when_tokens_zero() -> None:
    reasoner = _FakeReasoner("maf")
    reasoner.queue("ok", tokens=0)
    saga = (
        Saga("test").reason("draft", prompt="p", user_message=lambda ctx: "u").build()
    )

    _result, store = _run_with_fake_store(saga, reasoner)

    assert "_token_record:t1:draft" not in store


def test_no_record_when_reasoner_lacks_id() -> None:
    reasoner = _FakeReasoner("maf")
    reasoner.queue("ok", tokens=10)
    saga = (
        Saga("test").reason("draft", prompt="p", user_message=lambda ctx: "u").build()
    )
    runtime = QuadroSagaRuntime()
    runtime.register_reasoner(reasoner)
    reasoner.reasoner_id = None
    spec = StageSpec(capability="research", saga=saga, failure_status="failed")
    store: dict = {}

    asyncio.run(runtime.run_stage(_ctx(spec, {"task_id": "t1"}, store=store)))

    assert "_token_record:t1:draft" not in store
    assert store["_tokens:t1"]["by_stage"]["research"] == 10


def test_record_write_failure_does_not_fail_step() -> None:
    reasoner = _FakeReasoner("maf")
    reasoner.queue("ok", tokens=10)
    saga = (
        Saga("test").reason("draft", prompt="p", user_message=lambda ctx: "u").build()
    )
    store = {"_fail_token_record_put": True}

    result, store = _run_with_fake_store(saga, reasoner, store=store)

    assert result.output == "ok"
    assert "_token_record:t1:draft" not in store
    assert store["_tokens:t1"]["by_stage"]["research"] == 10


def test_token_records_returns_all_records_for_task() -> None:
    client = _client()
    reasoner = _FakeReasoner("maf")
    reasoner.queue("a", tokens=10)
    reasoner.queue("b", tokens=20)
    reasoner.queue("c", tokens=30)
    saga = (
        Saga("test")
        .reason("a", prompt="p", user_message=lambda ctx: "u")
        .reason("b", prompt="p", user_message=lambda ctx: "u")
        .reason("c", prompt="p", user_message=lambda ctx: "u")
        .build()
    )

    _run_with_client(saga, [reasoner], client)

    records = client.token_records(task_id="t1")
    assert [record["step_name"] for record in records] == ["a", "b", "c"]
    assert [record["token_total"] for record in records] == [10, 20, 30]


def test_token_records_returns_all_records_globally() -> None:
    client = _client()
    for task_id in ("t1", "t2"):
        reasoner = _FakeReasoner("maf")
        reasoner.queue(f"{task_id}-a", tokens=10)
        reasoner.queue(f"{task_id}-b", tokens=20)
        saga = (
            Saga(f"test-{task_id}")
            .reason("a", prompt="p", user_message=lambda ctx: "u")
            .reason("b", prompt="p", user_message=lambda ctx: "u")
            .build()
        )
        _run_with_client(saga, [reasoner], client, task_id=task_id)

    records = client.token_records()

    assert len(records) == 4
    assert {record["task_id"] for record in records} == {"t1", "t2"}


def test_tokens_by_stage_aggregates_correctly() -> None:
    client = _client()
    research = _FakeReasoner("maf")
    research.queue("research", tokens=10)
    writing = _FakeReasoner("maf")
    writing.queue("writing", tokens=25)

    _run_with_client(
        Saga("research")
        .reason("research_step", prompt="p", user_message=lambda ctx: "u")
        .build(),
        [research],
        client,
        stage="research",
    )
    _run_with_client(
        Saga("writing")
        .reason("writing_step", prompt="p", user_message=lambda ctx: "u")
        .build(),
        [writing],
        client,
        stage="writing",
    )

    assert client.tokens_by_stage() == {"research": 10, "writing": 25}


def test_tokens_by_reasoner_aggregates_correctly() -> None:
    client = _client()
    maf = _FakeReasoner("maf")
    langchain = _FakeReasoner("langchain")
    maf.queue("a", tokens=10)
    langchain.queue("b", tokens=20)
    saga = (
        Saga("test")
        .reason("a", prompt="p", user_message=lambda ctx: "u", via="maf")
        .reason("b", prompt="p", user_message=lambda ctx: "u", via="langchain")
        .build()
    )

    _run_with_client(saga, [maf, langchain], client)

    assert client.tokens_by_reasoner() == {"maf": 10, "langchain": 20}


def test_parallel_step_records_one_per_branch_reason_step() -> None:
    reasoner = _FakeReasoner("maf")
    reasoner.queue("a", tokens=10)
    reasoner.queue("b", tokens=20)
    reasoner.queue("c", tokens=30)
    saga = (
        Saga("test")
        .parallel(
            "fanout",
            branches=[
                lambda b: b.reason("a", prompt="p", user_message=lambda ctx: "u"),
                lambda b: b.reason("b", prompt="p", user_message=lambda ctx: "u"),
                lambda b: b.reason("c", prompt="p", user_message=lambda ctx: "u"),
            ],
        )
        .build()
    )

    _result, store = _run_with_fake_store(saga, reasoner)

    assert store["_token_record:t1:a"]["token_total"] == 10
    assert store["_token_record:t1:b"]["token_total"] == 20
    assert store["_token_record:t1:c"]["token_total"] == 30
    assert "_token_record:t1:fanout" not in store
