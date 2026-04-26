"""Coverage for the Phase 4 operational endpoints on :mod:`quadro.ui`.

The UI has always been a stdlib HTTP server. Phase 4 added ``/healthz`` and
``/metrics`` to it so single-process deployments get production-adjacent
observability without extra infrastructure or dependencies.
"""

from __future__ import annotations

import socket
import time
from http.client import HTTPConnection

from quadro import ChiefAgent, LocalA2ANetwork, QuadroBoard
from quadro.board.backends.sqlite import SqliteBoardBackend
from quadro.ui import (
    _UI_VERSION,
    _render_prometheus_metrics,
    serve_board,
)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _make_env() -> tuple[LocalA2ANetwork, QuadroBoard]:
    network = LocalA2ANetwork()
    board = QuadroBoard(
        SqliteBoardBackend(":memory:"),
        profile_resolver={"work": "fast"},
        network=network,
    )
    return network, board


# ── Pure renderer tests — no HTTP needed ─────────────────────────────────────


def test_prometheus_renderer_emits_required_metrics() -> None:
    state = {
        "tasks": [
            {"status": "UNASSIGNED"},
            {"status": "IN_PROGRESS"},
            {"status": "IN_PROGRESS"},
            {"status": "STALE"},
        ],
        "data": {
            "_chief_telemetry": {
                "cycles_run": 7,
                "consecutive_noops": 2,
                "last_cycle_duration_ms": 42,
            },
            "_sponsor_status": {"draining": True},
        },
    }
    body = _render_prometheus_metrics(state)

    assert "quadro_chief_cycles_total 7" in body
    assert "quadro_chief_consecutive_noops 2" in body
    assert "quadro_chief_last_cycle_duration_ms 42" in body
    assert "quadro_draining 1" in body
    assert 'quadro_tasks_total{status="UNASSIGNED"} 1' in body
    assert 'quadro_tasks_total{status="IN_PROGRESS"} 2' in body
    assert 'quadro_tasks_total{status="STALE"} 1' in body
    assert "quadro_ombudsman_stale_count 1" in body
    # Exposition format sanity
    assert body.startswith("# HELP ")
    assert body.endswith("\n")


def test_prometheus_renderer_is_safe_on_empty_board() -> None:
    body = _render_prometheus_metrics({})
    assert "quadro_chief_cycles_total 0" in body
    assert "quadro_draining 0" in body
    assert "quadro_ombudsman_stale_count 0" in body


# ── HTTP integration: full request/response cycle ────────────────────────────


def _wait_for_server(host: str, port: int, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.25):
                return
        except OSError:
            time.sleep(0.05)
    raise RuntimeError(f"UI server did not come up on {host}:{port}")


def test_healthz_endpoint_returns_ok() -> None:
    _, board = _make_env()
    bc = board.client()
    port = _free_port()
    server = serve_board(bc, port=port, background=True)
    try:
        _wait_for_server("127.0.0.1", port)
        conn = HTTPConnection("127.0.0.1", port, timeout=2.0)
        conn.request("GET", "/healthz")
        resp = conn.getresponse()
        body = resp.read().decode("utf-8")
        assert resp.status == 200
        assert '"status": "ok"' in body
    finally:
        server.shutdown()


def test_metrics_endpoint_returns_prometheus_text() -> None:
    _, board = _make_env()
    bc = board.client()
    bc.post_task("work", "one")
    bc.post_task("work", "two")

    port = _free_port()
    server = serve_board(bc, port=port, background=True)
    try:
        _wait_for_server("127.0.0.1", port)
        conn = HTTPConnection("127.0.0.1", port, timeout=2.0)
        conn.request("GET", "/metrics")
        resp = conn.getresponse()
        body = resp.read().decode("utf-8")
        assert resp.status == 200
        # Prometheus text format content type (version matters for scrapers).
        assert "text/plain" in resp.getheader("Content-Type", "")
        assert 'quadro_tasks_total{status="UNASSIGNED"} 2' in body
        assert "quadro_chief_cycles_total" in body
    finally:
        server.shutdown()


# ── --version CLI wiring ─────────────────────────────────────────────────────


def test_ui_version_matches_package_version() -> None:
    """The UI footer and ``--version`` must surface the installed package version."""
    import quadro

    assert _UI_VERSION == quadro.__version__


def test_ensure_chief_dependency_still_importable() -> None:
    """Guard against circular-import regressions introduced by the changes."""
    # Constructing a ChiefAgent exercises the same import paths that the UI
    # touches indirectly; if this call fails, the UI import graph is broken.
    network, board = _make_env()
    bc = board.client()
    chief = ChiefAgent.builder(bc).build()
    assert chief is not None
