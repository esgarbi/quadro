"""
Microsoft Agent Framework integration adapter for Quadro.

Provides MAF-specific extensions on top of the framework-agnostic
``quadro.pipeline.Pipeline`` base class:

  MafPipeline            Declarative builder that adds ``.llm()`` config
                         and auto-generates MAF-backed workers and chief.

  llm_call               One-line LLM invocation with prompt loading and
                         optional Pydantic schema validation.

  tools_from_lifecycle   Convenience wrapper that returns MAF ``@tool``-
                         decorated functions from a lifecycle graph.

  configure              Set module-level LLM client defaults.

Requires ``agent-framework`` as a runtime dependency.
The core ``quadro`` package remains zero-dependency.

Usage::

    from quadro.integrations.maf import llm_call, tools_from_lifecycle, MafPipeline
"""

from __future__ import annotations

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
#  Optional agent_framework import
# ═══════════════════════════════════════════════════════════════════════════════
#
# The symbols below are imported at module level (rather than inside each
# function body) so that ``typing.get_type_hints`` can resolve string
# annotations like ``WorkflowContext[AgentExecutorRequest]`` on nested
# ``@executor``-decorated functions. agent-framework 1.x added a strict
# validator that calls ``get_type_hints`` and rejects annotations it cannot
# resolve — with ``from __future__ import annotations`` the annotations are
# strings at runtime, so the resolver needs these names in ``__globals__``.
#
# The try/except preserves the zero-dependency property of the core package:
# ``quadro.integrations.maf`` stays importable even without agent-framework
# installed. Any actual use goes through ``_ensure_maf()`` which raises a
# friendly error in that case.
try:
    from agent_framework import (
        AgentExecutorRequest,
        Message,
        WorkflowBuilder,
        WorkflowContext,
        WorkflowEvent,
        executor,
    )
    from agent_framework import tool as maf_tool
except ImportError:  # pragma: no cover - exercised only without the extra
    AgentExecutorRequest = None  # type: ignore[assignment,misc]
    Message = None  # type: ignore[assignment,misc]
    WorkflowBuilder = None  # type: ignore[assignment,misc]
    WorkflowContext = None  # type: ignore[assignment,misc]
    WorkflowEvent = None  # type: ignore[assignment,misc]
    executor = None  # type: ignore[assignment,misc]
    maf_tool = None  # type: ignore[assignment,misc]

logger = logging.getLogger(__name__)

_MAF_IMPORT_ERROR: str = (
    "Microsoft Agent Framework is required for this module.  "
    "Install it with:  pip install agent-framework"
)


def _ensure_maf() -> None:
    if executor is None:
        raise ImportError(_MAF_IMPORT_ERROR)


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
    ``OpenAIChatClient``), **or** pass explicit credentials which are
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
            from agent_framework.openai import OpenAIChatClient

            return OpenAIChatClient(
                model=resolved_model,
                api_key=resolved_key,
                base_url=resolved_base,
            )

        _module_client_factory = _factory


def _default_client_factory():
    """Create an OpenAIChatClient from environment variables."""
    from agent_framework.openai import OpenAIChatClient

    key = os.environ.get("OPENAI_API_KEY", "")
    if not key:
        raise RuntimeError(
            "OPENAI_API_KEY not set. Either call quadro.integrations.maf.configure() "
            "or set the environment variable."
        )
    return OpenAIChatClient(
        model=os.environ.get("OPENAI_MODEL_ID", ""),
        api_key=key,
        base_url=os.environ.get("OPENAI_BASE_URL", ""),
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
# MAF's ``wf.run(..., stream=False)`` returns a list of ``WorkflowEvent``
# objects, and the chat response (with ``usage``) can surface on several
# shapes depending on the agent-framework version and the underlying
# client. The helper below probes the most common paths and silently
# returns 0 when nothing is found — a crash here would sink a worker over
# a telemetry field, which is never the right trade-off.


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


def _find_usage_payload(event: Any) -> Any | None:
    """Locate a ``usage``-like payload on a MAF event, if any.

    Tries a short list of plausible paths in order of likelihood. The
    first payload that contains a token field wins.
    """
    accessors = (
        lambda e: getattr(e, "usage_details", None),
        lambda e: getattr(e, "usage", None),
        lambda e: getattr(getattr(e, "data", None), "usage_details", None),
        lambda e: getattr(getattr(e, "data", None), "usage", None),
        lambda e: getattr(getattr(e, "response", None), "usage", None),
        lambda e: getattr(
            getattr(getattr(e, "data", None), "raw_representation", None),
            "usage",
            None,
        ),
    )
    first_payload: Any | None = None
    for accessor in accessors:
        try:
            payload = accessor(event)
        except Exception:  # noqa: BLE001
            payload = None
        if payload is None:
            continue
        if first_payload is None:
            first_payload = payload
        if _has_any_token_field(payload):
            return payload
    return first_payload


def _extract_token_usage(events: Any) -> int:
    """Best-effort sum of prompt + completion tokens across all events.

    Prefers explicit ``prompt_tokens`` / ``completion_tokens`` (and their
    openai-python cousins ``input_tokens`` / ``output_tokens``) and falls
    back to ``total_tokens`` when only an aggregate is reported.
    """
    total = 0
    for event in events or []:
        usage = _find_usage_payload(event)
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
    reporter: Callable[[int], None] | None, events: Any
) -> None:
    """Call ``reporter`` with the extracted token total, swallowing errors."""
    if reporter is None:
        return
    payload_hits = 0
    try:
        tokens = _extract_token_usage(events)
        payload_hits = sum(
            1 for event in (events or []) if _find_usage_payload(event) is not None
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
#  Internal MAF workflow runners
# ═══════════════════════════════════════════════════════════════════════════════


async def _run_single_agent(
    instructions: str,
    user_message: str,
    client_factory: Callable,
    default_options: dict | None = None,
    executor_prefix: str = "_agent",
    token_reporter: Callable[[int], None] | None = None,
) -> str:
    _ensure_maf()
    uid = uuid4().hex[:8]

    @executor(id=f"_{executor_prefix}_{uid}")
    async def _start(trigger: str, ctx: WorkflowContext[AgentExecutorRequest]) -> None:
        await ctx.send_message(
            AgentExecutorRequest(
                messages=[Message("user", [trigger])],
                should_respond=True,
            )
        )

    client = client_factory()
    agent = client.as_agent(
        name=f"{executor_prefix}_{uid}",
        instructions=instructions,
        default_options=default_options or {},
    )
    wf = WorkflowBuilder(start_executor=_start).add_edge(_start, agent).build()
    events = await wf.run(message=user_message, stream=False)

    _report_tokens(token_reporter, events)

    for event in events:
        if isinstance(event, WorkflowEvent) and event.type == "output":
            return _clean_llm_output(event.data.text)

    raise RuntimeError("Workflow produced no output event")


async def _run_chief_workflow(
    board_summary: str,
    instructions: str,
    tools: list,
    client_factory: Callable,
    agent_name_prefix: str = "chief",
    token_reporter: Callable[[int], None] | None = None,
) -> str | None:
    _ensure_maf()
    uid = uuid4().hex[:8]

    @executor(id=f"_chief_{uid}")
    async def _chief_start(
        trigger: str, ctx: WorkflowContext[AgentExecutorRequest]
    ) -> None:
        await ctx.send_message(
            AgentExecutorRequest(
                messages=[Message("user", [trigger])],
                should_respond=True,
            )
        )

    client = client_factory()
    agent = client.as_agent(
        name=f"{agent_name_prefix}_{uid}",
        instructions=instructions,
        tools=tools,
    )
    wf = (
        WorkflowBuilder(start_executor=_chief_start)
        .add_edge(_chief_start, agent)
        .build()
    )
    events = await wf.run(message=board_summary, stream=False)

    _report_tokens(token_reporter, events)

    for event in events:
        if isinstance(event, WorkflowEvent) and event.type == "output":
            return _clean_llm_output(event.data.text)
    return None


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
        Zero-arg callable returning an ``OpenAIChatClient``.  Falls back to
        the module-level default (env vars or :func:`configure`).
    executor_prefix:
        Short prefix for internal MAF executor/agent IDs.
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
    _ensure_maf()

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
        except Exception:
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
#  tools_from_lifecycle() — MAF wrapper
# ═══════════════════════════════════════════════════════════════════════════════


def _decorate_descriptors(descriptors: list[ToolDescriptor]) -> list:
    """Wrap ``ToolDescriptor`` instances with MAF ``@tool`` decorator."""
    _ensure_maf()
    return [maf_tool(name=d.name, description=d.description)(d.fn) for d in descriptors]


def tools_from_lifecycle(
    lifecycle: Any,
    *,
    stage_map: dict[str, str],
    board_fn: Callable[[str, dict], dict],
    network: Any,
    worker_registry: dict[str, list[tuple[str, str]]],
    extra_tools: list | None = None,
) -> list:
    """Auto-generate MAF ``@tool`` functions from a lifecycle graph.

    Thin wrapper over ``quadro.pipeline.generate_tool_descriptors``
    that applies the MAF ``@tool`` decorator to each descriptor.
    """
    _ensure_maf()
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
#  MAF stage spec (extends base StageSpec with prompt/schema)
# ═══════════════════════════════════════════════════════════════════════════════


@dataclass
class MafStageSpec(StageSpec):
    """Extended stage spec with MAF-specific fields for auto-generated workers."""

    prompt: str | Path | None = None
    output_schema: type | None = None


# ═══════════════════════════════════════════════════════════════════════════════
#  MafPipeline
# ═══════════════════════════════════════════════════════════════════════════════


class MafPipeline(Pipeline):
    """Declarative builder that wires Quadro + MAF into a runnable pipeline.

    Extends ``Pipeline`` with ``.llm()`` for MAF client configuration
    and overrides the three framework hooks to use MAF workflows.

    Usage::

        pipeline = (
            MafPipeline(board)
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
        _ensure_maf()
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
    ) -> MafPipeline:
        """Configure the LLM client for all stages and the chief.

        Passing ``token_reporter=runtime.meters.report_llm_tokens`` feeds
        usage from every MAF call (chief and auto-generated stage workers)
        into the shared :class:`~quadro.sponsor.meters.MeterBundle`, so
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
            from agent_framework.openai import OpenAIChatClient

            return OpenAIChatClient(model=_m, api_key=_k, base_url=_b)

        self._client_factory = _factory
        return self

    def _make_stage_spec(self, capability: str, **kwargs: Any) -> StageSpec:
        """Create a MafStageSpec that carries prompt/schema fields."""
        maf_fields = {
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
        return MafStageSpec(capability, **maf_fields)

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
        """Generate an execute_fn for a MAF prompt-in / schema-out stage."""
        if not isinstance(spec, MafStageSpec):
            raise TypeError(
                f"Cannot auto-generate execute_fn for non-MAF stage {spec.capability!r}. "
                f"Provide an explicit execute_fn."
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
                except Exception:
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
                except Exception as exc:
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
