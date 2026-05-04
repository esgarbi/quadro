"""
Anthropic SDK reasoner for Quadro sagas.

:class:`AnthropicReasoner` implements the structural protocol declared in
``quadro.saga.reasoner.Reasoner``. Each :meth:`reason` call resolves to one
Anthropic SDK ``messages.create`` call with optional Pydantic schema validation
via JSON-mode prompting.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any

from ._internal import _ensure_anthropic, _extract_tokens

DEFAULT_MODEL = "claude-3-5-sonnet-latest"
DEFAULT_MAX_TOKENS = 4096


class AnthropicReasoner:
    """Reasoner protocol implementation backed by the Anthropic Python SDK."""

    reasoner_id: str = "claude"

    def __init__(
        self,
        *,
        client_factory: Callable[[], Any],
        model: str = DEFAULT_MODEL,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        token_reporter: Callable[[int], None] | None = None,
    ) -> None:
        _ensure_anthropic()
        self._client_factory = client_factory
        self._model = model
        self._max_tokens = max_tokens
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
        system_prompt = prompt
        if schema is not None:
            system_prompt = f"{prompt}{_json_mode_instruction(schema)}"

        client = self._client_factory()
        response = await asyncio.to_thread(
            client.messages.create,
            model=self._model,
            max_tokens=self._max_tokens,
            system=system_prompt,
            messages=[{"role": "user", "content": str(user_message)}],
        )

        tokens_used = _extract_tokens(response)
        reporter = token_reporter or self._token_reporter
        if reporter is not None:
            try:
                reporter(tokens_used)
            except Exception:  # noqa: BLE001
                pass

        raw = _extract_text(response)
        cleaned = _strip_markdown_fence(raw)
        output = schema.model_validate_json(cleaned) if schema is not None else cleaned
        return ReasonResult(output=output, tokens_used=tokens_used, raw_text=cleaned)


def _json_mode_instruction(schema: type) -> str:
    instruction = (
        "\n\nRespond with valid JSON only, no preamble, no markdown code fences, "
        "no commentary."
    )
    try:
        schema_json = (
            schema.model_json_schema() if hasattr(schema, "model_json_schema") else None
        )
    except Exception:  # noqa: BLE001
        schema_json = None
    if schema_json is not None:
        instruction += (
            "\n\nThe JSON must conform to this schema:\n"
            f"{json.dumps(schema_json, indent=2)}"
        )
    return instruction


def _extract_text(response: Any) -> str:
    blocks = getattr(response, "content", None) or []
    text_parts: list[str] = []
    for block in blocks:
        text = getattr(block, "text", None)
        if isinstance(text, str) and text:
            text_parts.append(text)
            continue
        if isinstance(block, dict):
            dict_text = block.get("text")
            if isinstance(dict_text, str) and dict_text:
                text_parts.append(dict_text)
    return "".join(text_parts)


def _strip_markdown_fence(raw: str) -> str:
    cleaned = raw.strip()
    if not cleaned.startswith("```"):
        return cleaned

    lines = cleaned.splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines).strip()
