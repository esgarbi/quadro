"""
LangChain integration adapter for Quadro.

Provides LangChain-specific extensions on top of the framework-agnostic
``quadro.pipeline.Pipeline`` base class:

  LangChainPipeline      Declarative builder that adds ``.llm()`` config
                         and auto-generates LangChain-backed workers and
                         chief.

  llm_call               One-line LLM invocation with prompt loading and
                         optional Pydantic schema validation.

  tools_from_lifecycle   Convenience wrapper that returns LangChain
                         ``StructuredTool`` objects from a lifecycle graph.

  configure              Set module-level LLM client defaults.

Requires ``langchain-core`` and ``langchain-openai`` as runtime
dependencies.  The core ``quadro`` package remains zero-dependency.

Usage::

    from quadro.integrations.langchain import (
        llm_call,
        tools_from_lifecycle,
        LangChainPipeline,
    )
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from ..pipeline import (
    Pipeline,
    StageSpec,
    ToolDescriptor,
    generate_tool_descriptors,
)

# ═══════════════════════════════════════════════════════════════════════════════
#  Optional langchain imports
# ═══════════════════════════════════════════════════════════════════════════════
#
# The symbols below are imported at module level (rather than inside each
# function body) so that ``typing.get_type_hints`` can resolve string
# annotations under ``from __future__ import annotations`` — the same
# rationale used in ``quadro.integrations.maf``.
#
# The try/except preserves the zero-dependency property of the core
# package: ``quadro.integrations.langchain`` stays importable even
# without LangChain installed.  Any actual use goes through
# ``_ensure_langchain()`` which raises a friendly error in that case.
try:
    from langchain_core.messages import (
        AIMessage,
        HumanMessage,
        SystemMessage,
        ToolMessage,
    )
    from langchain_core.tools import StructuredTool
except ImportError:  # pragma: no cover - exercised only without the extra
    AIMessage = None  # type: ignore[assignment,misc]
    HumanMessage = None  # type: ignore[assignment,misc]
    SystemMessage = None  # type: ignore[assignment,misc]
    ToolMessage = None  # type: ignore[assignment,misc]
    StructuredTool = None  # type: ignore[assignment,misc]

logger = logging.getLogger(__name__)

_LC_IMPORT_ERROR: str = (
    "LangChain is required for this module.  "
    "Install it with:  pip install 'quadro[langchain]'"
)


def _ensure_langchain() -> None:
    if StructuredTool is None:
        raise ImportError(_LC_IMPORT_ERROR)


# ═══════════════════════════════════════════════════════════════════════════════
#  LLM client factory
# ═══════════════════════════════════════════════════════════════════════════════

_module_client_factory: Callable | None = None


def configure(
    *,
    client_factory: Callable | None = None,
    api_key: str | None = None,
    model: str | None = None,
    base_url: str | None = None,
) -> None:
    """Set module-level defaults for the LLM client.

    Either pass a *client_factory* callable (zero-arg, returns
    ``ChatOpenAI``), **or** pass explicit credentials which are
    baked into a factory.
    """
    global _module_client_factory
    if client_factory is not None:
        _module_client_factory = client_factory
        return

    if api_key is not None:
        resolved_key = api_key
        resolved_model = model or ""
        resolved_base = base_url or ""

        def _factory():  # type: ignore[return]
            from langchain_openai import ChatOpenAI

            return ChatOpenAI(
                model=resolved_model,
                api_key=resolved_key,
                base_url=resolved_base or None,
            )

        _module_client_factory = _factory


def _default_client_factory():
    """Create a ``ChatOpenAI`` from environment variables."""
    from langchain_openai import ChatOpenAI

    key = os.environ.get("OPENAI_API_KEY", "")
    if not key:
        raise RuntimeError(
            "OPENAI_API_KEY not set. Either call "
            "quadro.integrations.langchain.configure() or set the "
            "environment variable."
        )
    return ChatOpenAI(
        model=os.environ.get("OPENAI_MODEL_ID", ""),
        api_key=key,
        base_url=os.environ.get("OPENAI_BASE_URL") or None,
    )


def _get_client_factory() -> Callable:
    return _module_client_factory or _default_client_factory


# ═══════════════════════════════════════════════════════════════════════════════
#  Output cleanup
# ═══════════════════════════════════════════════════════════════════════════════

_REASONING_RE = re.compile(r"<\|[^|>]+\|>.*?(?=<\|[^|>]+\|>|$)", re.DOTALL)
_FENCE_RE = re.compile(r"```(?:json)?\s*([\s\S]*?)```")


def _clean_llm_output(text: str) -> str:
    cleaned = _REASONING_RE.sub("", text)
    fence = _FENCE_RE.search(cleaned)
    if fence:
        cleaned = fence.group(1)
    return cleaned.strip()


# ═══════════════════════════════════════════════════════════════════════════════
#  Token usage extraction
# ═══════════════════════════════════════════════════════════════════════════════
#
# LangChain surfaces token accounting on the ``AIMessage`` returned from
# ``ainvoke``:
#
#   * ``usage_metadata`` — a typed dict with ``input_tokens`` /
#     ``output_tokens`` / ``total_tokens`` (normalised across providers).
#   * ``response_metadata["token_usage"]`` — raw OpenAI-style dict with
#     ``prompt_tokens`` / ``completion_tokens`` / ``total_tokens``.
#   * ``response_metadata["usage"]`` — seen on some providers.
#
# The helpers below probe those shapes and silently return 0 when nothing
# is found — a crash here would sink a worker over a telemetry field,
# which is never the right trade-off.


_TOKEN_FIELDS = (
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
    "input_tokens",
    "output_tokens",
    "input_token_count",
    "output_token_count",
)


def _usage_field_as_int(obj: Any, *attrs: str) -> int:
    """Read the first integer-valued attribute/dict key from ``attrs`` off ``obj``."""
    if obj is None:
        return 0
    for name in attrs:
        value = getattr(obj, name, None)
        if value is None and isinstance(obj, dict):
            value = obj.get(name)
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            return value
        if isinstance(value, float) and math.isfinite(value) and value.is_integer():
            return int(value)
        if isinstance(value, str):
            stripped = value.strip()
            if stripped.startswith("+"):
                stripped = stripped[1:]
            if re.fullmatch(r"-?\d+", stripped):
                try:
                    return int(stripped)
                except ValueError:
                    pass
    return 0


def _has_any_token_field(payload: Any) -> bool:
    """True when a usage payload exposes at least one known token field."""
    for field in _TOKEN_FIELDS:
        value = getattr(payload, field, None)
        if value is None and isinstance(payload, dict):
            value = payload.get(field)
        if value is not None:
            return True
    return False


def _find_usage_payload(message: Any) -> Any | None:
    """Locate a ``usage``-like payload on a LangChain message, if any.

    Tries a short list of plausible paths in order of likelihood. The
    first payload that contains a token field wins.
    """
    accessors = (
        lambda m: getattr(m, "usage_metadata", None),
        lambda m: (getattr(m, "response_metadata", None) or {}).get("token_usage"),
        lambda m: (getattr(m, "response_metadata", None) or {}).get("usage"),
        lambda m: getattr(m, "usage", None),
    )
    first_payload: Any | None = None
    for accessor in accessors:
        try:
            payload = accessor(message)
        except Exception:  # noqa: BLE001
            payload = None
        if payload is None:
            continue
        if first_payload is None:
            first_payload = payload
        if _has_any_token_field(payload):
            return payload
    return first_payload


def _extract_token_usage(messages: Any) -> int:
    """Best-effort sum of prompt + completion tokens across all messages.

    Prefers explicit ``prompt_tokens`` / ``completion_tokens`` (and their
    LangChain cousins ``input_tokens`` / ``output_tokens``) and falls
    back to ``total_tokens`` when only an aggregate is reported.
    """
    total = 0
    for message in messages or []:
        usage = _find_usage_payload(message)
        if usage is None:
            continue
        prompt = _usage_field_as_int(
            usage, "prompt_tokens", "input_tokens", "input_token_count"
        )
        completion = _usage_field_as_int(
            usage, "completion_tokens", "output_tokens", "output_token_count"
        )
        if prompt or completion:
            total += prompt + completion
        else:
            total += _usage_field_as_int(usage, "total_tokens")
    return total


def _report_tokens(
    reporter: Callable[[int], None] | None, messages: Any
) -> None:
    """Call ``reporter`` with the extracted token total, swallowing errors."""
    if reporter is None:
        return
    payload_hits = 0
    try:
        tokens = _extract_token_usage(messages)
        payload_hits = sum(
            1 for message in (messages or []) if _find_usage_payload(message) is not None
        )
    except Exception:  # noqa: BLE001
        tokens = 0
    if tokens <= 0:
        if payload_hits and logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "token extraction resolved to zero despite %d usage payload(s)",
                payload_hits,
            )
        return
    try:
        reporter(tokens)
    except Exception as exc:  # noqa: BLE001
        logger.debug("token_reporter raised; ignoring: %s", exc)


# ═══════════════════════════════════════════════════════════════════════════════
#  Internal LangChain runners
# ═══════════════════════════════════════════════════════════════════════════════
#
# The chief runner hand-rolls a ``bind_tools`` + tool-message loop
# instead of pulling in ``langchain.agents.AgentExecutor`` or LangGraph.
# This mirrors the MAF adapter's hand-rolled ``WorkflowBuilder`` wiring
# and keeps the runtime dependency surface at just ``langchain-core`` +
# ``langchain-openai``.

_MAX_CHIEF_STEPS = 12


def _content_as_str(content: Any) -> str:
    """Flatten LangChain's ``str | list[dict]`` content shape to a ``str``."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for chunk in content:
            if isinstance(chunk, str):
                parts.append(chunk)
            elif isinstance(chunk, dict):
                text = chunk.get("text") or chunk.get("content")
                if isinstance(text, str):
                    parts.append(text)
        return "".join(parts)
    return "" if content is None else str(content)


async def _run_single_agent(
    instructions: str,
    user_message: str,
    client_factory: Callable,
    default_options: dict | None = None,
    executor_prefix: str = "_agent",
    token_reporter: Callable[[int], None] | None = None,
) -> str:
    _ensure_langchain()
    uid = uuid4().hex[:8]
    _ = (executor_prefix, uid)  # retained for log-prefix parity with MAF

    llm = client_factory()
    messages = [
        SystemMessage(content=instructions),
        HumanMessage(content=user_message),
    ]

    response_format = (default_options or {}).get("response_format")

    raw_msg: Any = None
    text: str = ""

    if response_format is not None and hasattr(response_format, "model_json_schema"):
        structured_llm = llm.with_structured_output(response_format, include_raw=True)
        result = await structured_llm.ainvoke(messages)
        raw_msg = result.get("raw") if isinstance(result, dict) else None
        parsed = result.get("parsed") if isinstance(result, dict) else None
        if parsed is not None and hasattr(parsed, "model_dump_json"):
            text = parsed.model_dump_json()
        elif raw_msg is not None:
            text = _content_as_str(getattr(raw_msg, "content", ""))
    else:
        bound_llm = llm
        if response_format is not None:
            try:
                bound_llm = llm.bind(response_format=response_format)
            except Exception:  # noqa: BLE001
                bound_llm = llm
        ai_msg = await bound_llm.ainvoke(messages)
        raw_msg = ai_msg
        text = _content_as_str(getattr(ai_msg, "content", ""))

    _report_tokens(token_reporter, [raw_msg] if raw_msg is not None else [])

    cleaned = _clean_llm_output(text)
    if not cleaned and raw_msg is None:
        raise RuntimeError("LLM produced no output")
    return cleaned


async def _run_chief_workflow(
    board_summary: str,
    instructions: str,
    tools: list,
    client_factory: Callable,
    agent_name_prefix: str = "chief",
    token_reporter: Callable[[int], None] | None = None,
) -> str | None:
    _ensure_langchain()
    uid = uuid4().hex[:8]
    _ = (agent_name_prefix, uid)  # retained for log-prefix parity with MAF

    llm = client_factory().bind_tools(tools) if tools else client_factory()
    messages: list[Any] = [
        SystemMessage(content=instructions),
        HumanMessage(content=board_summary),
    ]
    tools_by_name = {t.name: t for t in tools}
    collected: list[Any] = []

    async def _invoke_tool(tool_obj: Any, args: dict[str, Any]) -> Any:
        """Invoke a tool without blocking the event loop on sync tools."""
        if hasattr(tool_obj, "ainvoke"):
            try:
                return await tool_obj.ainvoke(args)
            except TypeError:
                # Some tool implementations expose ainvoke but reject kwargs/shape.
                pass
        return await asyncio.to_thread(tool_obj.invoke, args)

    for _step in range(_MAX_CHIEF_STEPS):
        ai_msg = await llm.ainvoke(messages)
        collected.append(ai_msg)

        tool_calls = getattr(ai_msg, "tool_calls", None) or []
        if not tool_calls:
            content = _content_as_str(getattr(ai_msg, "content", ""))
            _report_tokens(token_reporter, collected)
            return _clean_llm_output(content)

        messages.append(ai_msg)
        for call in tool_calls:
            name = call.get("name") if isinstance(call, dict) else getattr(call, "name", "")
            args = call.get("args") if isinstance(call, dict) else getattr(call, "args", {})
            call_id = call.get("id") if isinstance(call, dict) else getattr(call, "id", "")
            tool_obj = tools_by_name.get(name)
            if tool_obj is None:
                tool_output: Any = f"Tool {name!r} not found."
            else:
                try:
                    tool_output = await _invoke_tool(tool_obj, args or {})
                except Exception as exc:  # noqa: BLE001
                    tool_output = f"Tool error: {exc}"
            messages.append(
                ToolMessage(content=str(tool_output), tool_call_id=call_id or "")
            )

    _report_tokens(token_reporter, collected)
    logger.warning(
        "Chief exhausted max steps (%d) without a terminal response",
        _MAX_CHIEF_STEPS,
    )
    return "Chief exhausted max steps without producing a final response."


# ═══════════════════════════════════════════════════════════════════════════════
#  llm_call()
# ═══════════════════════════════════════════════════════════════════════════════


async def llm_call(
    prompt: str | Path,
    input: dict | str,  # noqa: A002
    *,
    schema: type | None = None,
    client_factory: Callable | None = None,
    executor_prefix: str = "llm_call",
    token_reporter: Callable[[int], None] | None = None,
) -> Any:
    """One-line LLM invocation with prompt loading and schema validation.

    Parameters
    ----------
    prompt:
        Either a ``Path`` to a ``.md`` prompt file, or the prompt text itself.
    input:
        The user message. If a ``dict``, it is serialised to JSON.
    schema:
        Optional Pydantic ``BaseModel`` subclass.  When provided the LLM is
        asked for JSON output and the response is validated against the schema.
    client_factory:
        Zero-arg callable returning a ``ChatOpenAI``.  Falls back to the
        module-level default (env vars or :func:`configure`).
    executor_prefix:
        Short prefix for internal log/agent IDs.
    token_reporter:
        Optional callable invoked with the sum of prompt + completion tokens
        after the call completes. Pass ``runtime.meters.report_llm_tokens``
        to feed :class:`~quadro.sponsor.LlmTokenBudgetSponsor`. Errors in
        the reporter are logged and swallowed — telemetry never fails a
        worker.

    Returns
    -------
    If *schema* is provided, returns a validated Pydantic model instance.
    Otherwise returns the raw LLM text output (cleaned).
    """
    _ensure_langchain()

    instructions: str
    if isinstance(prompt, Path):
        instructions = prompt.read_text()
    else:
        instructions = prompt

    user_message = json.dumps(input) if isinstance(input, dict) else input

    opts: dict | None = None
    if schema is not None:
        try:
            if hasattr(schema, "model_json_schema"):
                opts = {"response_format": schema}
            else:
                opts = {"response_format": {"type": "json_object"}}
        except Exception:  # noqa: BLE001
            opts = {"response_format": {"type": "json_object"}}

    factory = client_factory or _get_client_factory()

    raw = await _run_single_agent(
        instructions=instructions,
        user_message=user_message,
        client_factory=factory,
        default_options=opts,
        executor_prefix=executor_prefix,
        token_reporter=token_reporter,
    )

    if schema is not None:
        return schema.model_validate_json(raw)

    return raw


# ═══════════════════════════════════════════════════════════════════════════════
#  tools_from_lifecycle() — LangChain wrapper
# ═══════════════════════════════════════════════════════════════════════════════


def _decorate_descriptors(descriptors: list[ToolDescriptor]) -> list:
    """Wrap ``ToolDescriptor`` instances with LangChain ``StructuredTool``."""
    _ensure_langchain()
    return [
        StructuredTool.from_function(
            func=d.fn,
            name=d.name,
            description=d.description,
        )
        for d in descriptors
    ]


def tools_from_lifecycle(
    lifecycle: Any,
    *,
    stage_map: dict[str, str],
    board_fn: Callable[[str, dict], dict],
    network: Any,
    worker_registry: dict[str, list[tuple[str, str]]],
    extra_tools: list | None = None,
) -> list:
    """Auto-generate LangChain tool objects from a lifecycle graph.

    Thin wrapper over ``quadro.pipeline.generate_tool_descriptors`` that
    wraps each descriptor in a LangChain ``StructuredTool``.
    """
    _ensure_langchain()
    descriptors = generate_tool_descriptors(
        lifecycle,
        stage_map=stage_map,
        board_fn=board_fn,
        network=network,
        worker_registry=worker_registry,
    )
    tools = _decorate_descriptors(descriptors)
    if extra_tools:
        tools.extend(extra_tools)
    return tools


# ═══════════════════════════════════════════════════════════════════════════════
#  LangChain stage spec (extends base StageSpec with prompt/schema)
# ═══════════════════════════════════════════════════════════════════════════════


@dataclass
class LangChainStageSpec(StageSpec):
    """Extended stage spec with LangChain-specific fields for auto-generated workers."""

    prompt: str | Path | None = None
    output_schema: type | None = None


# ═══════════════════════════════════════════════════════════════════════════════
#  LangChainPipeline
# ═══════════════════════════════════════════════════════════════════════════════


class LangChainPipeline(Pipeline):
    """Declarative builder that wires Quadro + LangChain into a runnable pipeline.

    Extends ``Pipeline`` with ``.llm()`` for LangChain client configuration
    and overrides the three framework hooks to use LangChain runnables.

    Usage::

        pipeline = (
            LangChainPipeline(board)
            .llm(api_key_env="OPENAI_API_KEY", model_env="OPENAI_MODEL_ID")
            .workers(4)
            .capacity(8)
            .wakes("a2a://chief")
            .stage("validation",
                   prompt=Path("prompts/validation.md"),
                   output_schema=OrderValidation,
                   active_status="validating",
                   success_status="validated",
                   failure_status="validation_failed")
            .chief(prompt=Path("prompts/chief.md"), goal_key="order_goal")
            .build()
        )

        from quadro.sponsor import GoalSponsor
        final_state = runtime.sponsor(GoalSponsor(lambda s: ...)).run(pipeline)
    """

    def __init__(self, board: Any) -> None:
        _ensure_langchain()
        super().__init__(board)
        self._client_factory: Callable | None = None
        self._token_reporter: Callable[[int], None] | None = None

    def llm(
        self,
        *,
        client_factory: Callable | None = None,
        api_key: str | None = None,
        api_key_env: str = "OPENAI_API_KEY",
        model: str | None = None,
        model_env: str = "OPENAI_MODEL_ID",
        base_url: str | None = None,
        base_url_env: str = "OPENAI_BASE_URL",
        token_reporter: Callable[[int], None] | None = None,
    ) -> LangChainPipeline:
        """Configure the LLM client for all stages and the chief.

        Passing ``token_reporter=runtime.meters.report_llm_tokens`` feeds
        usage from every LangChain call (chief and auto-generated stage
        workers) into the shared
        :class:`~quadro.sponsor.meters.MeterBundle`, so
        :class:`~quadro.sponsor.LlmTokenBudgetSponsor` can cap cumulative
        tokens across the run.
        """
        self._token_reporter = token_reporter

        if client_factory is not None:
            self._client_factory = client_factory
            return self

        resolved_key = api_key or os.environ.get(api_key_env, "")
        resolved_model = model or os.environ.get(model_env, "")
        resolved_base = base_url or os.environ.get(base_url_env, "")

        def _factory(
            _k: str = resolved_key,
            _m: str = resolved_model,
            _b: str = resolved_base,
        ):
            from langchain_openai import ChatOpenAI

            return ChatOpenAI(model=_m, api_key=_k, base_url=_b or None)

        self._client_factory = _factory
        return self

    def _make_stage_spec(self, capability: str, **kwargs: Any) -> StageSpec:
        """Create a LangChainStageSpec that carries prompt/schema fields."""
        lc_fields = {
            k: v
            for k, v in kwargs.items()
            if k
            in {
                "execute_fn",
                "active_status",
                "success_status",
                "failure_status",
                "max_working_time",
                "tool_name",
                "prompt",
                "output_schema",
            }
        }
        return LangChainStageSpec(capability, **lc_fields)

    # ── Framework hooks ───────────────────────────────────────────────────────

    def _decorate_tools(self, descriptors: list[ToolDescriptor]) -> list:
        return _decorate_descriptors(descriptors)

    async def _run_chief_llm_turn(
        self,
        board_summary: str,
        instructions: str,
        tools: list,
    ) -> str | None:
        factory = self._client_factory or _get_client_factory()
        return await _run_chief_workflow(
            board_summary=board_summary,
            instructions=instructions,
            tools=tools,
            client_factory=factory,
            agent_name_prefix=self._chief_name_prefix,
            token_reporter=self._token_reporter,
        )

    def _make_auto_execute_fn(self, spec: StageSpec) -> Callable:
        """Generate an execute_fn for a LangChain prompt-in / schema-out stage."""
        if not isinstance(spec, LangChainStageSpec):
            raise TypeError(
                f"Cannot auto-generate execute_fn for non-LangChain stage "
                f"{spec.capability!r}. Provide an explicit execute_fn."
            )

        client_factory = self._client_factory or _get_client_factory()
        token_reporter = self._token_reporter

        prompt_text: str | None = None
        if isinstance(spec.prompt, Path):
            prompt_text = spec.prompt.read_text()
        elif isinstance(spec.prompt, str):
            prompt_text = spec.prompt

        schema = spec.output_schema
        success = spec.success_status
        failure = spec.failure_status
        cap = spec.capability

        async def _execute(context: dict, board_fn: Callable[[str, dict], dict]) -> str:
            task = context["payload"]["task"]
            task_input = task.get("output") or task.get("notes", ["{}"])[0]

            if isinstance(task_input, dict):
                user_message = json.dumps(task_input)
            else:
                user_message = str(task_input)

            instructions = prompt_text or f"You are a {cap} specialist."

            opts: dict | None = None
            if schema is not None:
                try:
                    if hasattr(schema, "model_json_schema"):
                        opts = {"response_format": schema}
                    else:
                        opts = {"response_format": {"type": "json_object"}}
                except Exception:  # noqa: BLE001
                    opts = {"response_format": {"type": "json_object"}}

            raw = await _run_single_agent(
                instructions=instructions,
                user_message=user_message,
                client_factory=client_factory,
                default_options=opts,
                executor_prefix=cap,
                token_reporter=token_reporter,
            )

            output_json = raw
            if schema is not None:
                try:
                    validated = schema.model_validate_json(raw)
                    output_json = validated.model_dump_json()
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Schema validation failed for %s: %s", cap, exc)
                    failure_target = failure or "FAILED"
                    if not failure:
                        logger.warning(
                            "Stage %s has output_schema but no failure_status; "
                            "falling back to FAILED for task %s",
                            cap,
                            task["task_id"],
                        )
                    board_fn(
                        "board.update_task",
                        {
                            "task_id": task["task_id"],
                            "to_status": failure_target,
                            "output": raw,
                            "notes_append": f"{cap} schema validation failed: {exc}",
                        },
                    )
                    return raw

            target = success or task.get("status", "COMPLETE")
            board_fn(
                "board.update_task",
                {
                    "task_id": task["task_id"],
                    "to_status": target,
                    "output": output_json,
                },
            )
            return output_json

        return _execute
