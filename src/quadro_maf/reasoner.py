"""
Microsoft Agent Framework reasoner for Quadro sagas.

:class:`MafReasoner` implements the structural protocol declared in
``quadro.saga.reasoner.Reasoner``. Each :meth:`reason` call resolves to
one MAF agent invocation with optional Pydantic schema validation.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ._internal import _ensure_maf, _run_single_agent


class MafReasoner:
    """Reasoner protocol implementation backed by Microsoft Agent Framework.

    The constructor takes a zero-arg ``client_factory`` returning an
    ``OpenAIChatClient`` and an optional ``token_reporter`` callable.
    Both are used for every :meth:`reason` call. The indirection through
    getter callables that the pre-J1 ``MafPipeline``-coupled reasoner
    carried has been removed — with construction now linear (the user
    builds the factory first, then constructs the reasoner with it)
    the getters no longer serve a purpose.
    """

    reasoner_id: str = "maf"

    def __init__(
        self,
        *,
        client_factory: Callable[[], Any],
        token_reporter: Callable[[int], None] | None = None,
    ) -> None:
        _ensure_maf()
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
        """Execute one reasoning episode and return a ``ReasonResult``.

        The ``token_reporter`` parameter takes precedence over the
        constructor-supplied reporter — the parameter is the per-call
        reporter from the saga runtime; the constructor value is the
        pipeline-wide default.
        """
        from quadro.saga.reasoner import ReasonResult

        opts: dict | None = None
        if schema is not None:
            try:
                if hasattr(schema, "model_json_schema"):
                    opts = {"response_format": schema}
                else:
                    opts = {"response_format": {"type": "json_object"}}
            except Exception:
                opts = {"response_format": {"type": "json_object"}}

        del step_name
        reporter = token_reporter or self._token_reporter

        # Counted reporter: wrap the supplied reporter so we can return
        # the token total in the ReasonResult (otherwise the saga
        # runtime sees the tokens reported but cannot read the count).
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
