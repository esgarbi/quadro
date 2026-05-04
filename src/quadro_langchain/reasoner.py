"""
LangChain reasoner for Quadro sagas.

:class:`LangChainReasoner` implements the structural protocol declared
in ``quadro.saga.reasoner.Reasoner``. Each :meth:`reason` call resolves
to one LangChain agent invocation with optional Pydantic schema
validation via ``with_structured_output``.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ._internal import _ensure_langchain, _run_single_agent


class LangChainReasoner:
    """Reasoner protocol implementation backed by LangChain.

    The constructor takes a zero-arg ``client_factory`` returning a
    ``ChatOpenAI`` and an optional ``token_reporter`` callable. The
    getter-indirection pattern used pre-J1 by the ``LangChainPipeline``-
    coupled reasoner has been removed.
    """

    reasoner_id: str = "langchain"

    def __init__(
        self,
        *,
        client_factory: Callable[[], Any],
        token_reporter: Callable[[int], None] | None = None,
    ) -> None:
        _ensure_langchain()
        self._client_factory = client_factory
        self._token_reporter = token_reporter

    async def reason(
        self,
        *,
        prompt: str,
        user_message: str,
        schema: type | None,
        token_reporter: Callable[[int], None] | None,
        step_name: str | None = None,
    ) -> Any:
        """Execute one reasoning episode and return a ``ReasonResult``."""
        from quadro.saga.reasoner import ReasonResult

        del step_name
        opts: dict | None = None
        if schema is not None:
            try:
                if hasattr(schema, "model_json_schema"):
                    opts = {"response_format": schema}
                else:
                    opts = {"response_format": {"type": "json_object"}}
            except Exception:
                opts = {"response_format": {"type": "json_object"}}

        reporter = token_reporter or self._token_reporter

        tokens_seen = 0

        def _counting_reporter(n: int) -> None:
            nonlocal tokens_seen
            tokens_seen += int(n)
            if reporter is not None:
                try:
                    reporter(n)
                except Exception:
                    pass

        raw = await _run_single_agent(
            instructions=prompt,
            user_message=user_message,
            client_factory=self._client_factory,
            default_options=opts,
            executor_prefix="saga_reason",
            token_reporter=_counting_reporter,
        )

        if schema is not None:
            output = schema.model_validate_json(raw)
        else:
            output = raw

        return ReasonResult(
            output=output,
            tokens_used=tokens_seen,
            raw_text=raw,
        )
