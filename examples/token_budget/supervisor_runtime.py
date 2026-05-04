"""Native supervisor runtime helpers for the LangChain token-budget example."""

from __future__ import annotations

import json
import os
from importlib import import_module
from pathlib import Path
from typing import Any

from quadro_langchain._internal import _clean_llm_output

_STAGE_RESULT_MARKER = "quadro_stage_result"
_ALLOWED_URGENCY = {"low", "medium", "high", "critical"}
_ALLOWED_CATEGORY = {"billing", "account", "outage", "feature_request", "other"}


def _flatten_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for chunk in content:
            if isinstance(chunk, str):
                parts.append(chunk)
                continue
            if isinstance(chunk, dict):
                maybe_text = chunk.get("text") or chunk.get("content")
                if isinstance(maybe_text, str):
                    parts.append(maybe_text)
        return "".join(parts)
    return "" if content is None else str(content)


def _extract_ticket_text(payload: Any) -> str:
    if isinstance(payload, str):
        return payload
    if isinstance(payload, dict):
        direct = payload.get("input")
        if isinstance(direct, str) and direct.strip():
            return direct
        task = payload.get("task")
        if isinstance(task, dict):
            notes = task.get("notes")
            if isinstance(notes, list) and notes and isinstance(notes[0], str):
                return notes[0]
            label = task.get("label")
            if isinstance(label, str) and label.strip():
                return label
        messages = payload.get("messages")
        if isinstance(messages, list) and messages:
            last = messages[-1]
            if isinstance(last, dict):
                content = last.get("content")
            else:
                content = getattr(last, "content", None)
            text = _flatten_content(content)
            if text.strip():
                return text
    return "{}"


def _normalize_classifier_output(raw_text: str) -> tuple[str, str, str | None]:
    """Return ``(output_json, status, notes_append)`` for runtime marker payload."""
    cleaned = _clean_llm_output(raw_text)
    try:
        parsed = json.loads(cleaned)
    except Exception:  # noqa: BLE001
        return cleaned or raw_text, "classify_failed", "Supervisor returned invalid JSON"
    if not isinstance(parsed, dict):
        return cleaned or raw_text, "classify_failed", "Supervisor returned non-object JSON"

    urgency = parsed.get("urgency")
    category = parsed.get("category")
    suggested_reply = parsed.get("suggested_reply")
    if urgency not in _ALLOWED_URGENCY:
        return cleaned, "classify_failed", f"Invalid urgency: {urgency!r}"
    if category not in _ALLOWED_CATEGORY:
        return cleaned, "classify_failed", f"Invalid category: {category!r}"
    if not isinstance(suggested_reply, str) or not suggested_reply.strip():
        return cleaned, "classify_failed", "Missing/empty suggested_reply"

    normalized = {
        "urgency": urgency,
        "category": category,
        "suggested_reply": suggested_reply.strip(),
    }
    return json.dumps(normalized), "classified", None


def _model_from_env():
    from langchain_openai import ChatOpenAI

    key = os.environ.get("OPENAI_API_KEY", "")
    if not key:
        raise RuntimeError("OPENAI_API_KEY not set")
    return ChatOpenAI(
        model=os.environ.get("OPENAI_MODEL_ID", ""),
        api_key=key,
        base_url=os.environ.get("OPENAI_BASE_URL") or None,
    )


def ensure_native_supervisor_available() -> None:
    """Raise a clear error when native supervisor dependencies are missing."""
    try:
        from langchain.agents import create_agent as _unused  # noqa: F401
    except ImportError as exc:  # pragma: no cover - runtime dependency guard
        try:
            _legacy_unused = getattr(import_module("langgraph.prebuilt"), "create_react_agent")
            _ = _legacy_unused
        except Exception:
            raise RuntimeError(
                "Native supervisor mode requires langchain/langgraph agents support. "
                "Install: pip install quadro[langchain]"
            ) from exc


def build_classifier_supervisor(prompt_path: str | Path):
    """Build a LangGraph supervisor runnable for ``stage(supervisor=...)``."""
    ensure_native_supervisor_available()
    try:
        from langchain.agents import create_agent
    except ImportError:
        create_agent = None
        create_react_agent = getattr(import_module("langgraph.prebuilt"), "create_react_agent")

    from langchain_core.tools import tool

    instructions = Path(prompt_path).read_text()

    @tool
    def classification_policy() -> str:
        """Policy hints for ticket classification output contract."""
        return (
            "Return JSON object keys: urgency, category, suggested_reply. "
            "Urgency: low|medium|high|critical. "
            "Category: billing|account|outage|feature_request|other."
        )

    if create_agent is not None:
        try:
            agent = create_agent(
                model=_model_from_env(),
                tools=[classification_policy],
                system_prompt=instructions,
            )
        except TypeError:
            agent = create_agent(
                model=_model_from_env(),
                tools=[classification_policy],
                prompt=instructions,
            )
    else:
        agent = create_react_agent(
            model=_model_from_env(),
            tools=[classification_policy],
            prompt=instructions,
        )

    class _SupervisorAdapter:
        async def ainvoke(self, payload: Any) -> dict[str, Any]:
            ticket_text = _extract_ticket_text(payload)
            state = await agent.ainvoke({"messages": [{"role": "user", "content": ticket_text}]})
            messages = state.get("messages") if isinstance(state, dict) else None
            if not isinstance(messages, list):
                messages = []
            final_text = (
                _flatten_content(getattr(messages[-1], "content", "")) if messages else ""
            )
            output_json, status, notes_append = _normalize_classifier_output(final_text)
            stage_result: dict[str, Any] = {
                "output": output_json,
                "status": status,
                "terminal_reason": "supervisor_completed",
            }
            if notes_append:
                stage_result["notes_append"] = notes_append
            return {
                "messages": messages,
                "output": {_STAGE_RESULT_MARKER: stage_result},
            }

    return _SupervisorAdapter()
