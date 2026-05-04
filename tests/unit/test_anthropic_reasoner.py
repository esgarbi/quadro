"""Unit tests for the AnthropicReasoner adapter."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from pydantic import BaseModel

import quadro_anthropic._internal as anthropic_internal
from quadro_anthropic import AnthropicReasoner


class _FakeUsage:
    def __init__(self, input_tokens: int, output_tokens: int) -> None:
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


class _FakeTextBlock:
    def __init__(self, text: str) -> None:
        self.text = text


class _FakeResponse:
    def __init__(
        self,
        text: str,
        input_tokens: int = 100,
        output_tokens: int = 50,
    ) -> None:
        self.content = [_FakeTextBlock(text)]
        self.usage = _FakeUsage(input_tokens, output_tokens)


class _FakeMessages:
    def __init__(self, response: _FakeResponse) -> None:
        self._response = response
        self.last_call_kwargs: dict[str, Any] = {}

    def create(self, **kwargs: Any) -> _FakeResponse:
        self.last_call_kwargs = kwargs
        return self._response


class _FakeAnthropicClient:
    def __init__(self, response: _FakeResponse) -> None:
        self.messages = _FakeMessages(response)


class _Output(BaseModel):
    headline: str
    score: int


@pytest.fixture(autouse=True)
def _anthropic_available(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(anthropic_internal, "Anthropic", object())


def test_reasoner_id_is_claude() -> None:
    def factory() -> _FakeAnthropicClient:
        return _FakeAnthropicClient(_FakeResponse("ok"))

    reasoner = AnthropicReasoner(client_factory=factory)

    assert reasoner.reasoner_id == "claude"


def test_reason_returns_text_when_no_schema() -> None:
    response = _FakeResponse("Plain text response.")

    def factory() -> _FakeAnthropicClient:
        return _FakeAnthropicClient(response)

    reasoner = AnthropicReasoner(client_factory=factory)
    result = asyncio.run(
        reasoner.reason(
            prompt="System prompt",
            user_message="User input",
            schema=None,
            token_reporter=None,
        )
    )

    assert result.output == "Plain text response."
    assert result.raw_text == "Plain text response."


def test_reason_validates_schema() -> None:
    json_response = json.dumps({"headline": "Hello", "score": 42})
    response = _FakeResponse(json_response)

    def factory() -> _FakeAnthropicClient:
        return _FakeAnthropicClient(response)

    reasoner = AnthropicReasoner(client_factory=factory)
    result = asyncio.run(
        reasoner.reason(
            prompt="System prompt",
            user_message="User input",
            schema=_Output,
            token_reporter=None,
        )
    )

    assert isinstance(result.output, _Output)
    assert result.output.headline == "Hello"
    assert result.output.score == 42


def test_reason_strips_markdown_fences() -> None:
    fenced = "```json\n" + json.dumps({"headline": "X", "score": 1}) + "\n```"
    response = _FakeResponse(fenced)

    def factory() -> _FakeAnthropicClient:
        return _FakeAnthropicClient(response)

    reasoner = AnthropicReasoner(client_factory=factory)
    result = asyncio.run(
        reasoner.reason(
            prompt="System prompt",
            user_message="User input",
            schema=_Output,
            token_reporter=None,
        )
    )

    assert result.output.headline == "X"
    assert result.raw_text == json.dumps({"headline": "X", "score": 1})


def test_reason_reports_tokens() -> None:
    response = _FakeResponse("ok", input_tokens=1000, output_tokens=200)

    def factory() -> _FakeAnthropicClient:
        return _FakeAnthropicClient(response)

    reported_tokens: list[int] = []

    def reporter(n: int) -> None:
        reported_tokens.append(n)

    reasoner = AnthropicReasoner(client_factory=factory)
    result = asyncio.run(
        reasoner.reason(
            prompt="System prompt",
            user_message="User input",
            schema=None,
            token_reporter=reporter,
        )
    )

    assert reported_tokens == [1200]
    assert result.tokens_used == 1200


def test_constructor_token_reporter_is_fallback() -> None:
    response = _FakeResponse("ok", input_tokens=10, output_tokens=5)

    def factory() -> _FakeAnthropicClient:
        return _FakeAnthropicClient(response)

    reported_tokens: list[int] = []
    reasoner = AnthropicReasoner(
        client_factory=factory,
        token_reporter=reported_tokens.append,
    )
    result = asyncio.run(
        reasoner.reason(
            prompt="System prompt",
            user_message="User input",
            schema=None,
            token_reporter=None,
        )
    )

    assert reported_tokens == [15]
    assert result.tokens_used == 15


def test_reason_token_reporter_errors_are_swallowed() -> None:
    response = _FakeResponse("ok", input_tokens=10, output_tokens=5)

    def factory() -> _FakeAnthropicClient:
        return _FakeAnthropicClient(response)

    def bad_reporter(n: int) -> None:
        raise RuntimeError(f"reporter failed for {n}")

    reasoner = AnthropicReasoner(client_factory=factory)
    result = asyncio.run(
        reasoner.reason(
            prompt="System prompt",
            user_message="User input",
            schema=None,
            token_reporter=bad_reporter,
        )
    )

    assert result.tokens_used == 15


def test_reason_includes_schema_in_system_prompt() -> None:
    response = _FakeResponse(json.dumps({"headline": "X", "score": 1}))
    fake_client = _FakeAnthropicClient(response)

    def factory() -> _FakeAnthropicClient:
        return fake_client

    reasoner = AnthropicReasoner(client_factory=factory)
    asyncio.run(
        reasoner.reason(
            prompt="Original system prompt",
            user_message="User input",
            schema=_Output,
            token_reporter=None,
        )
    )

    sent_system = fake_client.messages.last_call_kwargs.get("system", "")
    assert "Original system prompt" in sent_system
    assert "JSON" in sent_system
    assert "headline" in sent_system


def test_reason_uses_default_model() -> None:
    response = _FakeResponse("ok")
    fake_client = _FakeAnthropicClient(response)

    def factory() -> _FakeAnthropicClient:
        return fake_client

    reasoner = AnthropicReasoner(client_factory=factory)
    asyncio.run(
        reasoner.reason(
            prompt="System prompt",
            user_message="User input",
            schema=None,
            token_reporter=None,
        )
    )

    assert fake_client.messages.last_call_kwargs["model"] == (
        "claude-3-5-sonnet-latest"
    )


def test_reason_uses_model_override() -> None:
    response = _FakeResponse("ok")
    fake_client = _FakeAnthropicClient(response)

    def factory() -> _FakeAnthropicClient:
        return fake_client

    reasoner = AnthropicReasoner(
        client_factory=factory,
        model="claude-test-model",
    )
    asyncio.run(
        reasoner.reason(
            prompt="System prompt",
            user_message="User input",
            schema=None,
            token_reporter=None,
        )
    )

    assert fake_client.messages.last_call_kwargs["model"] == "claude-test-model"


def test_constructor_requires_anthropic_sdk(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(anthropic_internal, "Anthropic", None)

    with pytest.raises(ImportError, match=r"pip install 'quadro\[anthropic\]'"):
        AnthropicReasoner(client_factory=lambda: object())
