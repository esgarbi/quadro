"""
Reasoner protocol — the seam between the saga runner and any
LLM-executing framework.

The saga runner never imports an LLM client. When it encounters a
``reason`` step (added in milestone B), it looks up the registered
``Reasoner`` and delegates the actual model call. Each Quadro
integration that wraps an LLM framework (MAF, LangChain, future
LiteLLM, future Bedrock) ships its own ``Reasoner`` implementation
alongside its existing ``FrameworkRuntime`` in a sibling adapter package.

This module declares the protocol and its result type only. No
implementation lives here; concrete implementations ship in adapter packages
such as ``quadro_maf`` and ``quadro_langchain``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass
class ReasonResult:
    """Standardized return shape from any ``Reasoner`` implementation.

    Attributes
    ----------
    output:
        The validated model output. If the caller passed a ``schema``,
        this is an instance of that schema. Otherwise it is the raw
        cleaned text from the model.
    tokens_used:
        Sum of prompt and completion tokens for the call. Reporters
        upstream (``runtime.meters.report_llm_tokens``) consume this.
        Zero is a valid value when token accounting is unavailable.
    raw_text:
        The model's cleaned text output, before schema validation.
        Useful for telemetry and debugging; does not need to round-trip
        through ``output`` for correctness.
    """

    output: Any
    tokens_used: int
    raw_text: str


class Reasoner(Protocol):
    """Framework-neutral seam for a single reasoning episode.

    Implementations live in adapter packages such as ``quadro_maf`` and
    ``quadro_langchain``. The saga runner discovers them via the pipeline's
    ``.reasoner(reasoner)`` registration.
    """

    reasoner_id: str
    """Stable string identifier — e.g. ``"maf"``, ``"langchain"``. Used
    in telemetry events and, when more than one reasoner is registered,
    to select an implementation per step via ``reason(via=...)``."""

    async def reason(
        self,
        *,
        prompt: str,
        user_message: str,
        schema: type | None,
        token_reporter: Callable[[int], None] | None,
        step_name: str | None = None,
    ) -> ReasonResult:
        """Execute one reasoning episode and return the result.

        Parameters
        ----------
        prompt:
            The system prompt, already loaded from disk by the runner.
        user_message:
            The user-role message body, already serialized (JSON for
            dicts, ``str()`` for everything else) by the runner.
        schema:
            Optional pydantic ``BaseModel`` subclass. Implementations
            that support structured output should use the framework's
            native schema validation; the returned ``output`` must be
            an instance of the schema. Pass ``None`` for free-form text
            output.
        token_reporter:
            Optional callable invoked with the call's token total.
            Errors from the reporter must be swallowed — telemetry
            never fails a step.
        step_name:
            Optional saga step name for this reasoning episode. The
            estimator's dry-run collector uses it to label observations;
            production reasoners may ignore it.
        """
        ...
