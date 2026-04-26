"""Coverage for :mod:`quadro.integrations.otel`.

The OTel exporter is an optional module. These tests use an in-memory
``InMemorySpanExporter`` so the full span graph can be asserted without
external infrastructure.
"""

from __future__ import annotations

import pytest

pytest.importorskip("opentelemetry")

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from quadro import BoardClient, ChiefAgent, LocalA2ANetwork, QuadroBoard
from quadro.board.backends.sqlite import SqliteBoardBackend
from quadro.integrations.otel import QuadroTracer


@pytest.fixture
def otel_exporter() -> InMemorySpanExporter:
    """Install a fresh in-memory OTel pipeline for each test."""
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    try:
        yield exporter
    finally:
        # TracerProvider is a module-global; resetting ensures isolation.
        trace._TRACER_PROVIDER = None  # type: ignore[attr-defined]
        trace._TRACER_PROVIDER_SET_ONCE._done = False  # type: ignore[attr-defined]


def _make_env() -> tuple[LocalA2ANetwork, BoardClient, ChiefAgent, QuadroBoard]:
    network = LocalA2ANetwork()
    board = QuadroBoard(
        SqliteBoardBackend(":memory:"),
        profile_resolver={"work": "fast"},
        network=network,
    )
    bc = board.client()
    chief = ChiefAgent.builder(bc).build()
    return network, bc, chief, board


# ── Board events → spans ─────────────────────────────────────────────────────


def test_board_event_becomes_span(otel_exporter: InMemorySpanExporter) -> None:
    _, bc, chief, board = _make_env()

    with QuadroTracer.install(chief=chief, board=board):
        bc.post_task("work", "do_it")

    spans = otel_exporter.get_finished_spans()
    names = [s.name for s in spans]
    assert "quadro.task_posted" in names

    posted = next(s for s in spans if s.name == "quadro.task_posted")
    attrs = dict(posted.attributes or {})
    assert attrs["quadro.event_type"] == "task_posted"
    assert attrs["quadro.to_status"] == "UNASSIGNED"
    assert "quadro.task_id" in attrs
    assert "quadro.sequence_id" in attrs


def test_tracer_detaches_cleanly(otel_exporter: InMemorySpanExporter) -> None:
    _, bc, chief, board = _make_env()

    tracer = QuadroTracer.install(chief=chief, board=board)
    bc.post_task("work", "first")
    tracer.detach()
    bc.post_task("work", "second")

    spans = otel_exporter.get_finished_spans()
    posted_spans = [s for s in spans if s.name == "quadro.task_posted"]
    # Only the first task_posted should have been traced; detach removes the
    # listener before the second post.
    assert len(posted_spans) == 1


# ── Chief wake cycles → spans ────────────────────────────────────────────────


def test_chief_wake_becomes_span(otel_exporter: InMemorySpanExporter) -> None:
    _, bc, chief, board = _make_env()

    with QuadroTracer.install(chief=chief, board=board):
        chief.nudge(trigger="seed")

    spans = otel_exporter.get_finished_spans()
    cycle_spans = [s for s in spans if s.name == "quadro.chief_cycle"]
    assert cycle_spans, "expected at least one quadro.chief_cycle span"
    cycle = cycle_spans[0]
    attrs = dict(cycle.attributes or {})
    assert attrs["quadro.chief.trigger"] == "seed"
    assert attrs["quadro.chief.cycles_run"] >= 1
    assert attrs["quadro.chief.cycles_in_wake"] >= 1
    assert "quadro.chief.duration_ms" in attrs


# ── Double-attach safety ─────────────────────────────────────────────────────


def test_attach_twice_raises(otel_exporter: InMemorySpanExporter) -> None:
    _, _, chief, board = _make_env()

    tracer = QuadroTracer.install(chief=chief, board=board)
    try:
        with pytest.raises(RuntimeError, match="already attached"):
            tracer.attach(chief, board)
    finally:
        tracer.detach()


# ── Graceful import when OTel is not installed ──────────────────────────────

# Covered implicitly by pytest.importorskip at the top of this file:
# running the test suite without opentelemetry installed skips everything
# rather than crashing at import.
