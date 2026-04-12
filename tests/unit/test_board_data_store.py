from __future__ import annotations

from quadro.a2a.contracts import A2ARequest
from quadro.a2a.dispatch import LocalA2ANetwork
from quadro.board.backends.sqlite import SqliteBoardBackend
from quadro.board.board import QuadroBoard


def _make_board() -> tuple[QuadroBoard, LocalA2ANetwork, str]:
    network = LocalA2ANetwork()
    board_url = "a2a://board"
    board = QuadroBoard(SqliteBoardBackend(":memory:"))
    network.register_endpoint(board_url, board.handle_request)
    return board, network, board_url


def _request(network: LocalA2ANetwork, url: str, intent: str, payload: dict) -> dict:
    resp = network.request(url, A2ARequest(intent=intent, payload=payload).to_dict())
    assert resp["ok"], resp.get("error")
    return resp["result"]


def test_put_and_get_data() -> None:
    _, network, url = _make_board()
    _request(network, url, "board.put_data", {"key": "WH-MAIN", "value": {"SKU-A": 10}})
    result = _request(network, url, "board.get_data", {"key": "WH-MAIN"})
    assert result["key"] == "WH-MAIN"
    assert result["value"] == {"SKU-A": 10}


def test_get_missing_key_returns_none() -> None:
    _, network, url = _make_board()
    result = _request(network, url, "board.get_data", {"key": "nonexistent"})
    assert result["key"] == "nonexistent"
    assert result["value"] is None


def test_put_data_overwrites() -> None:
    _, network, url = _make_board()
    _request(network, url, "board.put_data", {"key": "WH-MAIN", "value": {"SKU-A": 10}})
    _request(
        network,
        url,
        "board.put_data",
        {"key": "WH-MAIN", "value": {"SKU-A": 5, "SKU-B": 20}},
    )
    result = _request(network, url, "board.get_data", {"key": "WH-MAIN"})
    assert result["value"] == {"SKU-A": 5, "SKU-B": 20}


def test_full_state_includes_data() -> None:
    _, network, url = _make_board()
    _request(network, url, "board.put_data", {"key": "WH-MAIN", "value": {"SKU-A": 10}})
    _request(
        network, url, "board.put_data", {"key": "WH-RESERVE", "value": {"SKU-A": 50}}
    )
    state = _request(network, url, "board.get_full_state", {})
    assert "data" in state
    assert state["data"]["WH-MAIN"] == {"SKU-A": 10}
    assert state["data"]["WH-RESERVE"] == {"SKU-A": 50}


def test_data_entries_emit_no_events() -> None:
    _, network, url = _make_board()
    _request(network, url, "board.put_data", {"key": "WH-MAIN", "value": {"SKU-A": 10}})
    _request(network, url, "board.put_data", {"key": "WH-MAIN", "value": {"SKU-A": 5}})
    events = _request(network, url, "board.stream_events", {"since_sequence": 0})
    assert events["events"] == []


def test_delete_data_removes_key() -> None:
    _, network, url = _make_board()
    _request(network, url, "board.put_data", {"key": "temp", "value": {"x": 1}})
    result = _request(network, url, "board.delete_data", {"key": "temp"})
    assert result["deleted"] is True
    get_result = _request(network, url, "board.get_data", {"key": "temp"})
    assert get_result["value"] is None


def test_delete_data_missing_key_returns_false() -> None:
    _, network, url = _make_board()
    result = _request(network, url, "board.delete_data", {"key": "nonexistent"})
    assert result["deleted"] is False
