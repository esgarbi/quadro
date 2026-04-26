"""Tests for ``QuadroRuntime.meters`` and ``with_meters``."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from quadro import QuadroRuntime
from quadro.board.backends.sqlite import SqliteBoardBackend
from quadro.sponsor import GoalSponsor
from quadro.sponsor.meters import MeterBundle


def _runtime() -> QuadroRuntime:
    return QuadroRuntime(SqliteBoardBackend(":memory:"))


def test_meters_is_lazy_and_stable() -> None:
    runtime = _runtime()

    assert runtime._meters is None
    first = runtime.meters
    second = runtime.meters

    assert isinstance(first, MeterBundle)
    assert first is second, "property must return the same instance each time"


def test_meters_is_observable_before_run() -> None:
    runtime = _runtime()

    runtime.meters.report_llm_tokens(120)
    runtime.meters.report_llm_tokens(80)

    assert runtime.meters.snapshot().llm_tokens == 200


def test_with_meters_injects_a_pre_built_bundle() -> None:
    custom = MeterBundle()
    custom.report_llm_tokens(42)

    runtime = _runtime().with_meters(custom)

    assert runtime.meters is custom
    assert runtime.meters.snapshot().llm_tokens == 42


def test_with_meters_rejects_post_board_configuration() -> None:
    runtime = _runtime()
    _ = runtime.board  # locks configuration

    with pytest.raises(RuntimeError, match="configuration cannot change"):
        runtime.with_meters(MeterBundle())


def test_runtime_run_uses_the_shared_meter_bundle() -> None:
    """The RunLoop must write into the same bundle the caller holds.

    Meters are reset at run-start (every run is a fresh accounting), so we
    verify identity by observing that the tick meter moves during the run:
    if the caller's bundle sees non-zero ticks afterwards, it is the same
    instance the RunLoop is incrementing.
    """
    runtime = _runtime()
    bundle_before = runtime.meters
    assert bundle_before.snapshot().ticks == 0

    chief = MagicMock()
    cycles_run = 0

    def done_after_two_cycles(state: dict) -> bool:
        nonlocal cycles_run
        cycles_run += 1
        return cycles_run >= 3

    (
        runtime.sponsor(GoalSponsor(done_after_two_cycles))
        .poll_every(0.0)
        .run(SimpleNamespace(chief=chief))
    )

    # Identity: still the same bundle.
    assert runtime.meters is bundle_before
    # The RunLoop must have ticked through it — proves it was used.
    assert runtime.meters.snapshot().ticks > 0
