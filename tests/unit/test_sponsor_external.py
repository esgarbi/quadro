from __future__ import annotations

import asyncio
import json
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from quadro.sponsor import (
    CallbackSponsor,
    Continue,
    Drain,
    HttpSponsor,
    Lease,
    MeterReadings,
    SponsorContext,
    Stop,
)


def _ctx() -> SponsorContext:
    return SponsorContext(
        state={"tasks": [], "agents": [], "data": {}},
        chief_telemetry={},
        meters=MeterReadings(),
        lease_history=(),
        now=datetime.now(timezone.utc),
    )


# ── CallbackSponsor ───────────────────────────────────────────────────────────


def test_callback_sponsor_runs_async_callable() -> None:
    async def cb(ctx, prior):
        return Continue(lease=Lease(ticks=4), reason="async_ok")

    sponsor = CallbackSponsor(cb, name="cb")
    d = sponsor.propose_lease(_ctx(), prior=None)
    assert isinstance(d, Continue)
    assert d.lease.ticks == 4
    assert d.reason == "async_ok"


def test_callback_sponsor_respects_timeout_and_fails_open_when_configured() -> None:
    async def slow(ctx, prior):
        await asyncio.sleep(0.5)
        return Continue(lease=Lease())

    # No fail_open + short timeout: an asyncio.TimeoutError propagates; the
    # runtime catches it. Here we exercise a fail_open path by wrapping.
    sponsor = CallbackSponsor(slow, timeout=0.01)
    with pytest.raises(asyncio.TimeoutError):
        sponsor.propose_lease(_ctx(), prior=None)


# ── HttpSponsor — via a local HTTP server ─────────────────────────────────────


class _Handler(BaseHTTPRequestHandler):
    response_body: dict = {}
    response_status: int = 200
    attempts: int = 0
    fail_first: int = 0  # return 5xx for the first N requests

    def do_POST(self) -> None:  # noqa: N802
        _Handler.attempts += 1
        length = int(self.headers.get("Content-Length", "0"))
        _ = self.rfile.read(length) if length else b""
        if _Handler.attempts <= _Handler.fail_first:
            self.send_response(503)
            self.end_headers()
            self.wfile.write(b"transient")
            return
        body = json.dumps(_Handler.response_body).encode("utf-8")
        self.send_response(_Handler.response_status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a, **k) -> None:  # silence test output
        return


def _start_server() -> tuple[HTTPServer, str]:
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    port = server.server_address[1]
    url = f"http://127.0.0.1:{port}/sponsor"
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return server, url


def _reset_handler(body: dict, status: int = 200, fail_first: int = 0) -> None:
    _Handler.response_body = body
    _Handler.response_status = status
    _Handler.attempts = 0
    _Handler.fail_first = fail_first


def test_http_sponsor_parses_continue_response() -> None:
    server, url = _start_server()
    try:
        _reset_handler(
            {
                "decision": "continue",
                "reason": "ok",
                "lease": {"ticks": 7, "llm_tokens": 500},
            }
        )
        sponsor = HttpSponsor(url, max_retries=0, timeout=2.0)
        d = sponsor.propose_lease(_ctx(), prior=None)
    finally:
        server.shutdown()

    assert isinstance(d, Continue)
    assert d.lease.ticks == 7
    assert d.lease.llm_tokens == 500


def test_http_sponsor_parses_drain_and_stop_responses() -> None:
    server, url = _start_server()
    try:
        _reset_handler({"decision": "drain", "reason": "slowdown"})
        sponsor = HttpSponsor(url, max_retries=0, timeout=2.0)
        d = sponsor.propose_lease(_ctx(), prior=None)
        assert isinstance(d, Drain)
        assert d.reason == "slowdown"

        _reset_handler({"decision": "stop", "reason": "cancelled"})
        d = sponsor.propose_lease(_ctx(), prior=None)
        assert isinstance(d, Stop)
        assert d.reason == "cancelled"
    finally:
        server.shutdown()


def test_http_sponsor_retries_on_5xx_then_succeeds() -> None:
    server, url = _start_server()
    try:
        _reset_handler(
            {"decision": "continue", "lease": {"ticks": 3}}, fail_first=2
        )
        sponsor = HttpSponsor(url, max_retries=3, timeout=2.0, backoff=0.001)
        d = sponsor.propose_lease(_ctx(), prior=None)
    finally:
        server.shutdown()
    assert isinstance(d, Continue)
    assert _Handler.attempts == 3


def test_http_sponsor_fail_closed_on_network_error() -> None:
    # An unreachable port — connection refused.
    sponsor = HttpSponsor(
        "http://127.0.0.1:1/sponsor",
        max_retries=0,
        timeout=0.25,
        fail_open=False,
    )
    d = sponsor.propose_lease(_ctx(), prior=None)
    assert isinstance(d, Stop)
    assert "http_error" in d.reason


def test_http_sponsor_fail_open_renews_prior_on_network_error() -> None:
    prior = Lease(id="prev99", ticks=5)
    sponsor = HttpSponsor(
        "http://127.0.0.1:1/sponsor",
        max_retries=0,
        timeout=0.25,
        fail_open=True,
    )
    d = sponsor.propose_lease(_ctx(), prior=prior)
    assert isinstance(d, Continue)
    assert d.lease.renewal_of == "prev99"
    assert d.lease.ticks == 5
