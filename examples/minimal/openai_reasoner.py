"""
OpenAIReasoner - a minimal reasoner adapter for the substrate.

Implements the structural ``Reasoner`` protocol declared in
``quadro.saga.reasoner``: a class with a ``reasoner_id`` attribute and an
async ``reason()`` method that returns ``ReasonResult``.

The implementation is intentionally small because the protocol is small.
Mirror this shape for Anthropic, Google, or any other LLM SDK.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from openai import OpenAI

from quadro.saga.reasoner import ReasonResult


class OpenAIReasoner:
    """Reasoner that calls the OpenAI Chat Completions API directly."""

    reasoner_id: str = "openai"

    def __init__(self, *, client: OpenAI, model: str = "gpt-4o-mini") -> None:
        self._client = client
        self._model = model

    async def reason(
        self,
        *,
        prompt: str,
        user_message: str,
        schema: type | None,
        token_reporter: Callable[[int], None] | None,
    ) -> ReasonResult:
        request: dict[str, Any] = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": prompt},
                {"role": "user", "content": user_message},
            ],
        }
        if schema is not None:
            request["response_format"] = {"type": "json_object"}

        response = self._client.chat.completions.create(**request)
        tokens_used = response.usage.total_tokens if response.usage else 0

        if token_reporter is not None:
            try:
                token_reporter(tokens_used)
            except Exception:
                pass

        raw = response.choices[0].message.content or ""
        output = schema.model_validate_json(raw) if schema is not None else raw
        return ReasonResult(output=output, tokens_used=tokens_used, raw_text=raw)
