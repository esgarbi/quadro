"""
Microsoft Agent Framework chief runtime for Quadro.

:class:`MafChiefRuntime` implements the ``FrameworkRuntime`` protocol
against MAF's workflow machinery. It provides both the chief-loop LLM
call (``run_chief_turn``) and native-stage delegation for
``stage(workflow=...)`` paths (``run_stage``).

The class replaces the pre-J1 pair of ``MafWorkflowRuntime`` (from
``quadro.runtime_plugins.maf_workflow``) and ``MafPipeline._run_chief_llm_turn``.
Both responsibilities live here now, registered on a plain
:class:`quadro.Pipeline` via ``.with_framework_runtime(...)``.
"""

from __future__ import annotations

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
    _decorate_descriptors,
    _extract_token_usage,
    _run_chief_workflow,
)

logger = logging.getLogger(__name__)
_STAGE_RESULT_MARKER = "quadro_stage_result"


@dataclass
class MafChiefRuntime(FrameworkRuntime):
    """Framework runtime for MAF-backed chief loops and workflow stages.

    Construction is linear: the user builds an ``OpenAIChatClient``
    factory first, then instantiates ``MafChiefRuntime(client_factory=…)``
    and hands the instance to :meth:`quadro.Pipeline.with_framework_runtime`.
    An optional ``token_reporter`` is invoked with the sum of prompt +
    completion tokens after each MAF call (both chief turns and stage
    runs) for feeding :class:`~quadro.sponsor.LlmTokenBudgetSponsor`.
    """

    client_factory: Callable[[], Any] = field(default=None)  # type: ignore[assignment]
    token_reporter: Callable[[int], None] | None = None
    chief_name_prefix: str = "chief"
    runtime_id: str = "maf_chief"

    def __post_init__(self) -> None:
        if self.client_factory is None:
            raise ValueError(
                "MafChiefRuntime requires a client_factory (zero-arg "
                "callable returning an OpenAIChatClient)."
            )

    # ── FrameworkRuntime protocol ────────────────────────────────────────────

    def can_handle(self, spec: Any) -> bool:
        return getattr(spec, "workflow", None) is not None

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
        """Execute a MAF workflow stage and normalise its events into a
        :class:`StageRunResult`.

        The body is a line-for-line move of the old
        ``MafWorkflowRuntime.run_stage`` with the only change being that
        ``_clean_llm_output`` and ``_extract_token_usage`` now live in
        :mod:`quadro_maf._internal` rather than the pre-J1
        ``quadro.integrations.maf`` module.
        """
        task = ctx.task
        workflow_ref = getattr(ctx.stage, "workflow", None)
        if workflow_ref is None:
            raise ValueError("MafChiefRuntime received a stage without workflow")

        workflow = self._resolve_workflow(workflow_ref, ctx)
        task_output = task.get("output")
        notes = task.get("notes")
        if task_output is not None:
            task_input = task_output
        elif isinstance(notes, list) and notes:
            task_input = notes[0]
        elif isinstance(notes, str) and notes:
            task_input = notes
        else:
            # Fresh first-stage tasks may have no output and empty notes.
            # Keep workflow input stable and let stage-specific workflow logic
            # use task metadata from RuntimeContext when needed.
            task_input = "{}"
        if isinstance(task_input, dict):
            message = json.dumps(task_input)
        else:
            message = str(task_input)

        events = await workflow.run(message=message, stream=False)
        token_total = _extract_token_usage(events)

        output_value: Any = None
        checkpoint_id: str | None = None
        resume_id: str | None = None
        status: str | None = getattr(ctx.stage, "success_status", None)
        notes_append: str | None = None
        update_fields: dict[str, Any] = {}
        terminal_reason = "workflow_completed"
        telemetry: list[dict[str, Any]] = []
        task_id = task.get("task_id")
        start_ts = datetime.now(UTC)

        telemetry.append(
            build_runtime_event(
                runtime=self.runtime_id,
                event_type="framework.stage_start",
                stage=ctx.stage.capability,
                task_id=task_id,
                payload={"event_count_hint": len(events or [])},
            )
        )

        for idx, event in enumerate(events or []):
            event_type = str(getattr(event, "type", type(event).__name__))
            event_payload: dict[str, Any] = {"index": idx, "event_type": event_type}
            data = getattr(event, "data", None)

            if data is not None:
                maybe_text = getattr(data, "text", None)
                if event_type == "output" and maybe_text is not None:
                    cleaned = _clean_llm_output(str(maybe_text))
                    try:
                        maybe_json = json.loads(cleaned)
                    except Exception:  # noqa: BLE001
                        output_value = cleaned
                    else:
                        if (
                            isinstance(maybe_json, dict)
                            and _STAGE_RESULT_MARKER in maybe_json
                            and isinstance(maybe_json[_STAGE_RESULT_MARKER], dict)
                        ):
                            stage_result = maybe_json[_STAGE_RESULT_MARKER]
                            if "output" in stage_result:
                                output_value = stage_result["output"]
                            if "status" in stage_result and stage_result["status"] is not None:
                                status = str(stage_result["status"])
                            if (
                                "notes_append" in stage_result
                                and stage_result["notes_append"] is not None
                            ):
                                notes_append = str(stage_result["notes_append"])
                            if isinstance(stage_result.get("update_fields"), dict):
                                update_fields.update(stage_result["update_fields"])
                            if "token_total" in stage_result:
                                try:
                                    token_total += int(stage_result["token_total"])
                                except Exception:  # noqa: BLE001
                                    pass
                            if (
                                "terminal_reason" in stage_result
                                and stage_result["terminal_reason"] is not None
                            ):
                                terminal_reason = str(stage_result["terminal_reason"])
                        else:
                            output_value = maybe_json

                for key in (
                    "tool_name",
                    "tool_call_id",
                    "checkpoint_id",
                    "resume_id",
                    "continuation_token",
                ):
                    value = getattr(data, key, None)
                    if value is not None:
                        event_payload[key] = value
                        if key in {"resume_id", "continuation_token"} and resume_id is None:
                            resume_id = str(value)
                        if key == "checkpoint_id" and checkpoint_id is None:
                            checkpoint_id = str(value)

            telemetry.append(
                build_runtime_event(
                    runtime=self.runtime_id,
                    event_type="framework.step",
                    stage=ctx.stage.capability,
                    task_id=task_id,
                    step_name=event_type,
                    payload=event_payload,
                )
            )

        if output_value is None:
            raise RuntimeError("Workflow produced no output event")

        telemetry.append(
            build_runtime_event(
                runtime=self.runtime_id,
                event_type="framework.stage_end",
                stage=ctx.stage.capability,
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

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _resolve_workflow(self, workflow_ref: Any, ctx: RuntimeContext) -> Any:
        workflow_obj = workflow_ref
        if callable(workflow_obj):
            try:
                workflow_obj = workflow_obj(ctx)
            except TypeError:
                workflow_obj = workflow_obj()
        if hasattr(workflow_obj, "build") and callable(workflow_obj.build):
            workflow_obj = workflow_obj.build()
        if workflow_obj is None or not hasattr(workflow_obj, "run"):
            raise TypeError(
                "MAF workflow stage requires a workflow object with async .run(message=..., stream=...)"
            )
        return workflow_obj
