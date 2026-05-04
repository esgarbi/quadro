"""
LangChain / LangGraph chief runtime for Quadro.

:class:`LangChainChiefRuntime` implements the ``FrameworkRuntime``
protocol against LangChain runnables. It provides chief-loop LLM
execution (``run_chief_turn``) and native-stage delegation for
``stage(graph=...)`` / ``stage(supervisor=...)`` paths (``run_stage``).

Replaces the pre-J1 pair of ``LangChainRuntime`` and
``LangChainPipeline._run_chief_llm_turn``.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from quadro.runtime_plugins.base import FrameworkRuntime, RuntimeContext, StageRunResult
from quadro.runtime_plugins.telemetry import build_runtime_event

from ._internal import (
    _clean_llm_output,
    _content_as_str,
    _decorate_descriptors,
    _extract_token_usage,
    _run_chief_workflow,
)

logger = logging.getLogger(__name__)
_STAGE_RESULT_MARKER = "quadro_stage_result"


def _task_input_for_stage(task: dict[str, Any]) -> Any:
    """Best-effort extraction of stage input from task payload."""
    output = task.get("output")
    if output is not None:
        return output
    notes = task.get("notes")
    if isinstance(notes, list) and notes:
        return notes[0]
    if isinstance(notes, str) and notes:
        return notes
    return "{}"


def _message_content(message: Any, flatten_content: Any) -> str:
    """Extract textual content from a message-like object."""
    if isinstance(message, dict):
        content = message.get("content")
        return flatten_content(content)
    return flatten_content(getattr(message, "content", ""))


def _message_tool_calls(message: Any) -> list[dict[str, Any]]:
    """Best-effort extraction of tool-call descriptors from a message."""
    if isinstance(message, dict):
        direct = message.get("tool_calls")
        if isinstance(direct, list):
            return [c for c in direct if isinstance(c, dict)]
        additional = message.get("additional_kwargs")
        if isinstance(additional, dict):
            nested = additional.get("tool_calls")
            if isinstance(nested, list):
                return [c for c in nested if isinstance(c, dict)]
        return []
    calls = getattr(message, "tool_calls", None)
    if isinstance(calls, list):
        normalized: list[dict[str, Any]] = []
        for call in calls:
            if isinstance(call, dict):
                normalized.append(call)
                continue
            name = getattr(call, "name", None)
            call_id = getattr(call, "id", None)
            if name is not None or call_id is not None:
                normalized.append({"name": name, "id": call_id})
        return normalized
    return []


@dataclass
class LangChainChiefRuntime(FrameworkRuntime):
    """Framework runtime for LangChain chief loops and graph/supervisor stages."""

    client_factory: Callable[[], Any] = field(default=None)  # type: ignore[assignment]
    token_reporter: Callable[[int], None] | None = None
    chief_name_prefix: str = "chief"
    runtime_id: str = "langchain_chief"

    def __post_init__(self) -> None:
        if self.client_factory is None:
            raise ValueError(
                "LangChainChiefRuntime requires a client_factory (zero-arg "
                "callable returning a ChatOpenAI)."
            )

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _entrypoint(self, spec: Any) -> tuple[str, Any] | None:
        graph = getattr(spec, "graph", None)
        if graph is not None:
            return ("graph", graph)
        supervisor = getattr(spec, "supervisor", None)
        if supervisor is not None:
            return ("supervisor", supervisor)
        return None

    def _resolve_runnable(self, ref: Any, ctx: RuntimeContext) -> Any:
        obj = ref
        if callable(obj):
            try:
                obj = obj(ctx)
            except TypeError:
                obj = obj()
        if hasattr(obj, "build") and callable(obj.build):
            obj = obj.build()
        if (
            hasattr(obj, "compile")
            and callable(obj.compile)
            and not hasattr(obj, "ainvoke")
            and not hasattr(obj, "invoke")
        ):
            obj = obj.compile()
        if obj is None or (not hasattr(obj, "ainvoke") and not hasattr(obj, "invoke")):
            raise TypeError(
                "LangChain runtime stage requires a runnable with .ainvoke(...) or .invoke(...)"
            )
        return obj

    async def _invoke_runnable(
        self,
        runnable: Any,
        attempts: list[Any],
    ) -> tuple[Any, int]:
        shape_errors: list[Exception] = []
        for idx, payload in enumerate(attempts):
            try:
                if hasattr(runnable, "ainvoke"):
                    return await runnable.ainvoke(payload), idx
                return await asyncio.to_thread(runnable.invoke, payload), idx
            except TypeError as exc:
                # Only TypeError is treated as input-shape mismatch worthy of
                # trying the next payload variant. Other errors should surface
                # directly to avoid duplicate LLM turns.
                shape_errors.append(exc)
                continue
            except Exception as exc:  # noqa: BLE001
                # Preserve shutdown race wording so existing example-level log
                # filters can suppress post-stop noise.
                msg = str(exc)
                if "cannot schedule new futures after shutdown" in msg:
                    raise
                raise RuntimeError(
                    f"LangChain runnable invocation failed: {exc}"
                ) from exc

        if shape_errors:
            detail = "; ".join(str(e) for e in shape_errors[:3])
            raise RuntimeError(
                f"LangChain runnable invocation failed across payload shapes: {detail}"
            ) from shape_errors[-1]
        raise RuntimeError("LangChain runnable invocation failed across payload shapes")

    def _messages_from_result(self, result: Any) -> list[Any]:
        if isinstance(result, dict):
            messages = result.get("messages")
            if isinstance(messages, list):
                return messages
        messages_attr = getattr(result, "messages", None)
        if isinstance(messages_attr, list):
            return messages_attr
        if isinstance(result, list):
            return result
        return []

    # ── FrameworkRuntime protocol ────────────────────────────────────────────

    def can_handle(self, spec: Any) -> bool:
        return self._entrypoint(spec) is not None

    def decorate_tools(self, descriptors: list[Any]) -> list:
        return _decorate_descriptors(descriptors)

    async def run_chief_turn(
        self,
        board_summary: str,
        instructions: str,
        tools: list,
        *,
        chief_name_prefix: str,
    ) -> str | None:
        return await _run_chief_workflow(
            board_summary=board_summary,
            instructions=instructions,
            tools=tools,
            client_factory=self.client_factory,
            agent_name_prefix=chief_name_prefix or self.chief_name_prefix,
            token_reporter=self.token_reporter,
        )

    async def run_stage(self, ctx: RuntimeContext) -> StageRunResult:
        task = ctx.task
        stage = ctx.stage
        selected = self._entrypoint(stage)
        if selected is None:
            raise ValueError(
                "LangChainChiefRuntime received a stage without graph/supervisor"
            )
        entrypoint_kind, entrypoint_ref = selected

        runnable = self._resolve_runnable(entrypoint_ref, ctx)
        task_input = _task_input_for_stage(task)
        if isinstance(task_input, dict):
            task_text = json.dumps(task_input)
        else:
            task_text = str(task_input)

        invoke_attempts = [
            {
                "input": task_text,
                "messages": [{"role": "user", "content": task_text}],
                "task": task,
                "context": ctx.context,
            },
            {"messages": [{"role": "user", "content": task_text}], "input": task_text},
            task_text,
        ]
        result, attempt_index = await self._invoke_runnable(runnable, invoke_attempts)

        messages = self._messages_from_result(result)
        token_total = _extract_token_usage(messages)

        output_value: Any = None
        checkpoint_id: str | None = None
        resume_id: str | None = None
        status: str | None = getattr(stage, "success_status", None)
        notes_append: str | None = None
        update_fields: dict[str, Any] = {}
        terminal_reason = f"{entrypoint_kind}_completed"
        telemetry: list[dict[str, Any]] = []
        task_id = task.get("task_id")
        start_ts = datetime.now(UTC)

        if isinstance(result, dict):
            maybe_checkpoint = result.get("checkpoint_id") or result.get("checkpoint")
            maybe_resume = result.get("resume_id") or result.get("continuation_token")
            if maybe_checkpoint is not None:
                checkpoint_id = str(maybe_checkpoint)
            if maybe_resume is not None:
                resume_id = str(maybe_resume)
            maybe_terminal = result.get("terminal_reason")
            if isinstance(maybe_terminal, str) and maybe_terminal.strip():
                terminal_reason = maybe_terminal.strip()

        telemetry.append(
            build_runtime_event(
                runtime=self.runtime_id,
                event_type="framework.stage_start",
                stage=stage.capability,
                task_id=task_id,
                payload={
                    "entrypoint": entrypoint_kind,
                    "invoke_attempt_count": len(invoke_attempts),
                    "selected_attempt": attempt_index,
                },
            )
        )

        telemetry.append(
            build_runtime_event(
                runtime=self.runtime_id,
                event_type="framework.step",
                stage=stage.capability,
                task_id=task_id,
                step_name="invoke",
                payload={"result_type": type(result).__name__},
            )
        )

        for msg in messages:
            for call in _message_tool_calls(msg):
                telemetry.append(
                    build_runtime_event(
                        runtime=self.runtime_id,
                        event_type="framework.tool_call",
                        stage=stage.capability,
                        task_id=task_id,
                        tool_name=str(call.get("name") or ""),
                        tool_call_id=str(call.get("id") or ""),
                        payload={"call": call},
                    )
                )

        raw_output: Any = None
        if isinstance(result, dict) and "output" in result:
            raw_output = result.get("output")
        elif messages:
            raw_output = _message_content(messages[-1], _content_as_str)
        else:
            raw_output = result

        marker_obj: dict[str, Any] | None = None
        if isinstance(raw_output, dict):
            if _STAGE_RESULT_MARKER in raw_output and isinstance(
                raw_output[_STAGE_RESULT_MARKER], dict
            ):
                marker_obj = raw_output[_STAGE_RESULT_MARKER]
            else:
                output_value = raw_output
        elif isinstance(raw_output, str):
            cleaned = _clean_llm_output(raw_output)
            try:
                maybe_json = json.loads(cleaned)
            except Exception:  # noqa: BLE001
                output_value = cleaned
            else:
                if isinstance(maybe_json, dict) and isinstance(
                    maybe_json.get(_STAGE_RESULT_MARKER), dict
                ):
                    marker_obj = maybe_json[_STAGE_RESULT_MARKER]
                else:
                    output_value = maybe_json
        else:
            output_value = raw_output

        if marker_obj is not None:
            if "output" in marker_obj:
                output_value = marker_obj["output"]
            if marker_obj.get("status") is not None:
                status = str(marker_obj["status"])
            if marker_obj.get("notes_append") is not None:
                notes_append = str(marker_obj["notes_append"])
            if isinstance(marker_obj.get("update_fields"), dict):
                update_fields.update(marker_obj["update_fields"])
            if marker_obj.get("token_total") is not None:
                try:
                    token_total += int(marker_obj["token_total"])
                except Exception:  # noqa: BLE001
                    pass
            if marker_obj.get("terminal_reason") is not None:
                terminal_reason = str(marker_obj["terminal_reason"])

        if output_value is None:
            raise RuntimeError("LangChain graph/supervisor stage produced no output")

        telemetry.append(
            build_runtime_event(
                runtime=self.runtime_id,
                event_type="framework.stage_end",
                stage=stage.capability,
                task_id=task_id,
                status=status,
                token_total=token_total,
                terminal_reason=terminal_reason,
                checkpoint_id=checkpoint_id,
                resume_id=resume_id,
                duration_ms=max(
                    0,
                    int((datetime.now(UTC) - start_ts).total_seconds() * 1000),
                ),
            )
        )

        return StageRunResult(
            output=output_value,
            status=status,
            notes_append=notes_append,
            update_fields=update_fields or None,
            token_total=token_total,
            telemetry=telemetry,
            checkpoint_id=checkpoint_id,
            resume_id=resume_id,
            terminal_reason=terminal_reason,
        )
