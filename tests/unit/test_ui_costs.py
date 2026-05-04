from __future__ import annotations

from types import MethodType
from typing import Any

from quadro import LocalA2ANetwork, QuadroBoard
from quadro.board.backends.sqlite import SqliteBoardBackend
from quadro.ui import (
    _DataSource,
    _Handler,
    _aggregate_token_records,
    _render_prometheus_metrics,
)


def _record(
    task_id: str,
    stage: str,
    step_name: str,
    reasoner_id: str,
    token_total: int,
    timestamp: str = "2026-01-01T00:00:00+00:00",
) -> dict[str, Any]:
    return {
        "task_id": task_id,
        "stage": stage,
        "step_name": step_name,
        "reasoner_id": reasoner_id,
        "token_prompt": None,
        "token_completion": None,
        "token_total": token_total,
        "timestamp": timestamp,
    }


def _data(*records: dict[str, Any]) -> dict[str, Any]:
    return {
        f"_token_record:{record['task_id']}:{record['step_name']}": record
        for record in records
    }


def test_aggregate_returns_none_when_no_records() -> None:
    assert _aggregate_token_records(data={}, tasks=[]) is None


def test_aggregate_by_stage_sums_correctly() -> None:
    aggs = _aggregate_token_records(
        _data(
            _record("t1", "research", "a", "maf", 100),
            _record("t1", "research", "b", "maf", 150),
            _record("t2", "writing", "c", "maf", 75),
        ),
        tasks=[],
    )

    assert aggs is not None
    assert aggs["by_stage"] == {"research": 250, "writing": 75}


def test_aggregate_by_reasoner_sums_correctly() -> None:
    aggs = _aggregate_token_records(
        _data(
            _record("t1", "research", "a", "maf", 100),
            _record("t1", "research", "b", "small", 50),
            _record("t2", "writing", "c", "maf", 75),
        ),
        tasks=[],
    )

    assert aggs is not None
    assert aggs["by_reasoner"] == {"maf": 175, "small": 50}


def test_aggregate_by_task_top_n_capped_and_labeled() -> None:
    records = [
        _record(f"t{i}", "research", f"s{i}", "maf", i * 10)
        for i in range(1, 16)
    ]
    tasks = [{"task_id": f"t{i}", "label": f"Task {i}"} for i in range(1, 16)]

    aggs = _aggregate_token_records(_data(*records), tasks=tasks)

    assert aggs is not None
    assert len(aggs["by_task"]) == 10
    assert aggs["by_task"][0]["task_id"] == "t15"
    assert aggs["by_task"][0]["label"] == "Task 15"
    assert aggs["by_task"][-1]["task_id"] == "t6"


def test_aggregate_by_task_stages_deduplicated() -> None:
    aggs = _aggregate_token_records(
        _data(
            _record("t1", "research", "a", "maf", 100),
            _record("t1", "research", "b", "maf", 50),
            _record("t1", "writing", "c", "maf", 75),
        ),
        tasks=[],
    )

    assert aggs is not None
    assert aggs["by_task"][0]["stages"] == ["research", "writing"]


def test_aggregate_total_and_avg_per_task() -> None:
    aggs = _aggregate_token_records(
        _data(
            _record("t1", "research", "a", "maf", 100),
            _record("t2", "writing", "b", "maf", 101),
        ),
        tasks=[],
    )

    assert aggs is not None
    assert aggs["total"] == 201
    assert aggs["task_count"] == 2
    assert aggs["record_count"] == 2
    assert aggs["avg_per_task"] == 100


def test_aggregate_avg_per_task_handles_zero() -> None:
    aggs = _aggregate_token_records(
        _data(_record("t1", "research", "a", "maf", 0)),
        tasks=[],
    )

    assert aggs is not None
    assert aggs["total"] == 0
    assert aggs["task_count"] == 1
    assert aggs["avg_per_task"] == 0


class _FakeSource:
    def __init__(self, state: dict[str, Any]) -> None:
        self.state = state

    def full_state(self) -> dict[str, Any]:
        return self.state

    def all_events(self) -> list[dict[str, Any]]:
        return []

    def token_aggregates(
        self,
        data: dict[str, Any],
        tasks: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        return _aggregate_token_records(data, tasks)


def _call_serve_state(state: dict[str, Any]) -> dict[str, Any]:
    handler = object.__new__(_Handler)
    handler.source = _FakeSource(state)
    handler.db_label = "test.db"
    handler.col_order = None
    captured: dict[str, Any] = {}

    def capture_json(self: _Handler, code: int, body: Any) -> None:
        captured["code"] = code
        captured["body"] = body

    handler._json = MethodType(capture_json, handler)
    handler._serve_state()
    assert captured["code"] == 200
    return captured["body"]


def test_serve_state_includes_token_aggregates() -> None:
    body = _call_serve_state(
        {
            "tasks": [{"task_id": "t1", "status": "research", "label": "One"}],
            "agents": [],
            "data": _data(_record("t1", "research", "a", "maf", 123)),
        }
    )

    assert body["_meta"]["token_aggregates"]["total"] == 123
    assert body["_meta"]["token_aggregates"]["by_task_total"] == {"t1": 123}


def test_serve_state_recovers_llm_token_sponsor_widget_data() -> None:
    data = _data(_record("t1", "research", "a", "maf", 123))
    data.update(
        {
            "order_goal": {"token_budget": 1_000},
            "_sponsor_status": {
                "sponsor_id": "llm_token_budget",
                "draining": False,
                "meters": {"llm_tokens": 0},
            },
        }
    )

    body = _call_serve_state(
        {
            "tasks": [{"task_id": "t1", "status": "research", "label": "One"}],
            "agents": [],
            "data": data,
        }
    )

    sponsor = body["_meta"]["sponsor"]
    assert sponsor["active_lease"]["llm_tokens"] == 1_000
    assert sponsor["meters"]["llm_tokens"] == 123


def test_serve_state_omits_token_aggregates_when_empty() -> None:
    body = _call_serve_state(
        {
            "tasks": [{"task_id": "t1", "status": "research", "label": "One"}],
            "agents": [],
            "data": {},
        }
    )

    assert "token_aggregates" not in body["_meta"]


def test_sqlite_task_token_records_filters_by_task(tmp_path) -> None:
    db_path = tmp_path / "board.db"
    backend = SqliteBoardBackend(str(db_path))
    backend.init()
    backend.put_data(
        "_token_record:t1:late",
        _record("t1", "writing", "late", "maf", 20, "2026-01-01T00:00:02+00:00"),
    )
    backend.put_data(
        "_token_record:t2:other",
        _record("t2", "research", "other", "maf", 99, "2026-01-01T00:00:01+00:00"),
    )
    backend.put_data(
        "_token_record:t1:early",
        _record("t1", "research", "early", "maf", 10, "2026-01-01T00:00:01+00:00"),
    )

    source = _DataSource(db_path=str(db_path))

    assert [r["step_name"] for r in source.task_token_records("t1")] == [
        "early",
        "late",
    ]


def test_live_token_aggregates_match_board_client_helpers() -> None:
    network = LocalA2ANetwork()
    board = QuadroBoard(SqliteBoardBackend(":memory:"), network=network)
    client = board.client()
    client.put_data("_token_record:t1:a", _record("t1", "research", "a", "maf", 10))
    client.put_data("_token_record:t1:b", _record("t1", "writing", "b", "small", 20))
    source = _DataSource(board_client=client)

    aggs = source.token_aggregates(
        client.full_state()["data"],
        [{"task_id": "t1", "label": "One"}],
    )

    assert aggs is not None
    assert aggs["by_stage"] == client.tokens_by_stage()
    assert aggs["by_reasoner"] == client.tokens_by_reasoner()


def test_prometheus_renderer_emits_llm_token_total() -> None:
    body = _render_prometheus_metrics(
        {
            "tasks": [],
            "data": _data(
                _record("t1", "research", "a", "maf", 10),
                _record("t2", "writing", "b", "maf", 20),
            ),
        }
    )

    assert "# HELP quadro_llm_tokens_total Total LLM tokens consumed." in body
    assert "quadro_llm_tokens_total 30" in body
