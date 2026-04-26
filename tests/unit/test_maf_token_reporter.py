"""Tests for the MAF adapter's token-usage extraction and reporting helpers.

These helpers are pure-Python and don't require ``agent-framework`` to be
installed, so the tests run in the standard ``quadro`` dev environment.
"""

from __future__ import annotations

from types import SimpleNamespace as NS

import pytest

from quadro.integrations.maf import (
    _extract_token_usage,
    _find_usage_payload,
    _report_tokens,
    _usage_field_as_int,
)


# ── _usage_field_as_int ──────────────────────────────────────────────────────


def test_usage_field_as_int_reads_attributes_in_order() -> None:
    obj = NS(prompt_tokens=10, input_tokens=99)
    assert _usage_field_as_int(obj, "prompt_tokens", "input_tokens") == 10
    assert _usage_field_as_int(obj, "input_tokens", "prompt_tokens") == 99


def test_usage_field_as_int_reads_dict_keys() -> None:
    assert _usage_field_as_int({"total_tokens": 77}, "total_tokens") == 77


def test_usage_field_as_int_ignores_non_ints() -> None:
    obj = NS(total_tokens="a string")
    assert _usage_field_as_int(obj, "total_tokens") == 0


def test_usage_field_as_int_on_none_is_zero() -> None:
    assert _usage_field_as_int(None, "whatever") == 0


# ── _find_usage_payload — tries multiple shapes ──────────────────────────────


def test_find_usage_payload_openai_classic_shape() -> None:
    event = NS(data=NS(usage=NS(prompt_tokens=10, completion_tokens=5)))
    payload = _find_usage_payload(event)
    assert payload is not None
    assert payload.prompt_tokens == 10


def test_find_usage_payload_maf_usage_details_shape() -> None:
    event = NS(usage_details=NS(input_tokens=3, output_tokens=2))
    payload = _find_usage_payload(event)
    assert payload is not None
    assert payload.input_tokens == 3


def test_find_usage_payload_raw_representation_shape() -> None:
    event = NS(data=NS(raw_representation=NS(usage=NS(total_tokens=9))))
    payload = _find_usage_payload(event)
    assert payload is not None
    assert payload.total_tokens == 9


def test_find_usage_payload_absent_returns_none() -> None:
    event = NS(type="output", data=NS(text="hello"))
    assert _find_usage_payload(event) is None


# ── _extract_token_usage — sums across the event list ────────────────────────


def test_extract_token_usage_prefers_prompt_plus_completion() -> None:
    event = NS(data=NS(usage=NS(prompt_tokens=100, completion_tokens=50)))
    assert _extract_token_usage([event]) == 150


def test_extract_token_usage_falls_back_to_total_tokens() -> None:
    event = NS(data=NS(usage=NS(total_tokens=77)))
    assert _extract_token_usage([event]) == 77


def test_extract_token_usage_accepts_input_output_naming() -> None:
    event = NS(usage_details=NS(input_tokens=30, output_tokens=20))
    assert _extract_token_usage([event]) == 50


def test_extract_token_usage_sums_multiple_events() -> None:
    e1 = NS(data=NS(usage=NS(prompt_tokens=100, completion_tokens=50)))
    e2 = NS(usage_details=NS(input_tokens=30, output_tokens=20))
    empty = NS(type="output", data=NS(text="noop"))
    assert _extract_token_usage([e1, e2, empty]) == 200


def test_extract_token_usage_empty_or_missing_events_returns_zero() -> None:
    assert _extract_token_usage([]) == 0
    assert _extract_token_usage(None) == 0
    assert _extract_token_usage([NS(type="output")]) == 0


# ── _report_tokens — the public hook for runners ─────────────────────────────


def test_report_tokens_calls_reporter_with_the_total() -> None:
    calls: list[int] = []
    e1 = NS(data=NS(usage=NS(prompt_tokens=10, completion_tokens=5)))
    e2 = NS(usage_details=NS(input_tokens=3, output_tokens=2))

    _report_tokens(calls.append, [e1, e2])

    assert calls == [20]


def test_report_tokens_is_noop_when_reporter_is_none() -> None:
    # Must not raise even with rich event data.
    _report_tokens(None, [NS(data=NS(usage=NS(prompt_tokens=5)))])


def test_report_tokens_does_not_call_reporter_on_zero_usage() -> None:
    calls: list[int] = []
    _report_tokens(calls.append, [NS(type="output", data=NS(text="x"))])
    assert calls == []


def test_report_tokens_swallows_reporter_exceptions() -> None:
    """A flaky telemetry sink must never fail a worker."""

    def _bad(_: int) -> None:
        raise RuntimeError("reporter blew up")

    event = NS(data=NS(usage=NS(prompt_tokens=10, completion_tokens=5)))
    # Must not raise — telemetry failures are swallowed.
    _report_tokens(_bad, [event])


def test_report_tokens_swallows_extraction_exceptions() -> None:
    """A malformed event must not bring down a worker."""

    class _Explodes:
        @property
        def data(self):  # noqa: ANN202
            raise RuntimeError("booby-trapped event")

    # Defensive: extraction probes return None for this event; total is 0,
    # reporter is not called. The important thing is we do not raise.
    calls: list[int] = []
    _report_tokens(calls.append, [_Explodes()])
    assert calls == []


# ── Sanity: importing maf.py does not require agent-framework ────────────────


def test_maf_module_imports_without_agent_framework() -> None:
    """These helpers must be usable in the zero-dep core test environment."""
    import quadro.integrations.maf as mod

    assert callable(mod._extract_token_usage)
    assert callable(mod._report_tokens)


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
