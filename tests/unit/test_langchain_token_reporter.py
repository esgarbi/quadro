"""Tests for the LangChain adapter's token-usage extraction and reporting helpers.

These helpers are pure-Python and don't require ``langchain-core`` /
``langchain-openai`` to be installed, so the tests run in the standard
``quadro`` dev environment.

The test matrix intentionally mirrors ``test_maf_token_reporter.py`` so
the two files can be diff-compared by reviewers. The only real
difference is the shape of the payloads the helpers probe: LangChain
surfaces usage on ``AIMessage.usage_metadata`` and
``AIMessage.response_metadata["token_usage"]`` / ``["usage"]`` rather
than on MAF's ``WorkflowEvent.usage_details`` /
``event.data.usage`` / ``event.data.raw_representation.usage``.
"""

from __future__ import annotations

from types import SimpleNamespace as NS

import pytest

from quadro.integrations.langchain import (
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


def test_find_usage_payload_usage_metadata_shape() -> None:
    """LangChain's normalised ``AIMessage.usage_metadata`` TypedDict."""
    msg = NS(
        usage_metadata={
            "input_tokens": 30,
            "output_tokens": 20,
            "total_tokens": 50,
        },
        response_metadata={},
    )
    payload = _find_usage_payload(msg)
    assert payload is not None
    assert payload["input_tokens"] == 30


def test_find_usage_payload_response_metadata_token_usage_shape() -> None:
    """OpenAI-style raw usage under ``response_metadata["token_usage"]``."""
    msg = NS(
        usage_metadata=None,
        response_metadata={
            "token_usage": {"prompt_tokens": 100, "completion_tokens": 50}
        },
    )
    payload = _find_usage_payload(msg)
    assert payload is not None
    assert payload["prompt_tokens"] == 100


def test_find_usage_payload_response_metadata_usage_shape() -> None:
    """Some providers nest an aggregate total under ``response_metadata["usage"]``."""
    msg = NS(
        usage_metadata=None,
        response_metadata={"usage": {"total_tokens": 9}},
    )
    payload = _find_usage_payload(msg)
    assert payload is not None
    assert payload["total_tokens"] == 9


def test_find_usage_payload_absent_returns_none() -> None:
    msg = NS(usage_metadata=None, response_metadata={}, content="hello")
    assert _find_usage_payload(msg) is None


# ── _extract_token_usage — sums across the message list ──────────────────────


def test_extract_token_usage_prefers_prompt_plus_completion() -> None:
    msg = NS(
        usage_metadata=None,
        response_metadata={
            "token_usage": {"prompt_tokens": 100, "completion_tokens": 50}
        },
    )
    assert _extract_token_usage([msg]) == 150


def test_extract_token_usage_falls_back_to_total_tokens() -> None:
    msg = NS(
        usage_metadata=None,
        response_metadata={"usage": {"total_tokens": 77}},
    )
    assert _extract_token_usage([msg]) == 77


def test_extract_token_usage_accepts_input_output_naming() -> None:
    msg = NS(
        usage_metadata={"input_tokens": 30, "output_tokens": 20},
        response_metadata={},
    )
    assert _extract_token_usage([msg]) == 50


def test_extract_token_usage_sums_multiple_messages() -> None:
    m1 = NS(
        usage_metadata=None,
        response_metadata={
            "token_usage": {"prompt_tokens": 100, "completion_tokens": 50}
        },
    )
    m2 = NS(
        usage_metadata={"input_tokens": 30, "output_tokens": 20},
        response_metadata={},
    )
    empty = NS(usage_metadata=None, response_metadata={}, content="noop")
    assert _extract_token_usage([m1, m2, empty]) == 200


def test_extract_token_usage_empty_or_missing_messages_returns_zero() -> None:
    assert _extract_token_usage([]) == 0
    assert _extract_token_usage(None) == 0
    assert (
        _extract_token_usage(
            [NS(usage_metadata=None, response_metadata={}, content="x")]
        )
        == 0
    )


# ── _report_tokens — the public hook for runners ─────────────────────────────


def test_report_tokens_calls_reporter_with_the_total() -> None:
    calls: list[int] = []
    m1 = NS(
        usage_metadata=None,
        response_metadata={
            "token_usage": {"prompt_tokens": 10, "completion_tokens": 5}
        },
    )
    m2 = NS(
        usage_metadata={"input_tokens": 3, "output_tokens": 2},
        response_metadata={},
    )

    _report_tokens(calls.append, [m1, m2])

    assert calls == [20]


def test_report_tokens_is_noop_when_reporter_is_none() -> None:
    # Must not raise even with rich message data.
    _report_tokens(
        None,
        [
            NS(
                usage_metadata={"input_tokens": 5, "output_tokens": 0},
                response_metadata={},
            )
        ],
    )


def test_report_tokens_does_not_call_reporter_on_zero_usage() -> None:
    calls: list[int] = []
    _report_tokens(
        calls.append,
        [NS(usage_metadata=None, response_metadata={}, content="x")],
    )
    assert calls == []


def test_report_tokens_swallows_reporter_exceptions() -> None:
    """A flaky telemetry sink must never fail a worker."""

    def _bad(_: int) -> None:
        raise RuntimeError("reporter blew up")

    msg = NS(
        usage_metadata={"input_tokens": 10, "output_tokens": 5},
        response_metadata={},
    )
    # Must not raise — telemetry failures are swallowed.
    _report_tokens(_bad, [msg])


def test_report_tokens_swallows_extraction_exceptions() -> None:
    """A malformed message must not bring down a worker."""

    class _Explodes:
        @property
        def usage_metadata(self):  # noqa: ANN202
            raise RuntimeError("booby-trapped message")

        @property
        def response_metadata(self):  # noqa: ANN202
            raise RuntimeError("booby-trapped message")

    # Defensive: extraction probes return None for this message; total is 0,
    # reporter is not called. The important thing is we do not raise.
    calls: list[int] = []
    _report_tokens(calls.append, [_Explodes()])
    assert calls == []


# ── Sanity: importing langchain.py does not require LangChain ────────────────


def test_langchain_module_imports_without_langchain() -> None:
    """These helpers must be usable in the zero-dep core test environment."""
    import quadro.integrations.langchain as mod

    assert callable(mod._extract_token_usage)
    assert callable(mod._report_tokens)


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
