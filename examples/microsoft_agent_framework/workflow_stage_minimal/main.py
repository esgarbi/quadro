"""Minimal MAF workflow-stage proof for Quadro runtime plugins.

Demonstrates native stage entrypoint usage:

    stage(workflow=build_classifier_workflow, ...)

Quadro remains the governance control plane (lifecycle, sponsors, run
loop), while MAF owns workflow internals and execution semantics.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))

from agent_framework import AgentExecutorRequest, Message, WorkflowBuilder, WorkflowContext, executor  # noqa: E402
from agent_framework.openai import OpenAIChatClient  # noqa: E402

from quadro import LifecycleBuilder, QuadroRuntime  # noqa: E402
from quadro.board.backends.sqlite import SqliteBoardBackend  # noqa: E402
from quadro.integrations.maf import MafPipeline  # noqa: E402
from quadro.sponsor import AllOf, DeadlineSponsor, LlmTokenBudgetSponsor, QueueDepthSponsor  # noqa: E402

HERE = Path(__file__).parent
DB_PATH = HERE / "workflow_stage_minimal.db"

load_dotenv(HERE / ".env")

TICKET_LIFECYCLE = (
    LifecycleBuilder()
    .step("UNASSIGNED", "classifying")
    .step("classifying", "classified")
    .branch("classifying", "classify_failed")
    .build()
)


def _client_from_env() -> OpenAIChatClient:
    key = os.environ.get("OPENAI_API_KEY", "")
    if not key:
        raise RuntimeError("OPENAI_API_KEY not set")
    return OpenAIChatClient(
        model=os.environ.get("OPENAI_MODEL_ID", ""),
        api_key=key,
        base_url=os.environ.get("OPENAI_BASE_URL", ""),
    )


def build_classifier_workflow():
    """Return a MAF workflow used by ``stage(workflow=...)``."""

    @executor(id="_workflow_stage_start")
    async def _start(trigger: str, ctx: WorkflowContext[AgentExecutorRequest]) -> None:
        await ctx.send_message(
            AgentExecutorRequest(
                messages=[Message("user", [trigger])],
                should_respond=True,
            )
        )

    client = _client_from_env()
    agent = client.as_agent(
        name="workflow_stage_classifier",
        instructions=(
            "Classify support-ticket text and respond with compact JSON keys: "
            "urgency, category, suggested_reply."
        ),
        default_options={"response_format": {"type": "json_object"}},
    )
    return WorkflowBuilder(start_executor=_start).add_edge(_start, agent).build()


def _seed(runtime: QuadroRuntime) -> None:
    runtime.put_data("tickets_goal", {"total": 1, "domain": "workflow-stage-proof"})
    task = runtime.client.post_task(
        "classify",
        "Ticket T-001: Cannot access dashboard",
        notes=["User reports login works but dashboard fails to load."],
    )
    runtime.put_data("tickets_queue", [task["task_id"]])


def build_runtime_and_pipeline():
    if DB_PATH.exists():
        DB_PATH.unlink()

    runtime = QuadroRuntime(SqliteBoardBackend(str(DB_PATH))).with_profiles(
        profile_resolver={"classify": "ticket"},
        custom_profiles={"ticket": TICKET_LIFECYCLE},
    )

    pipeline = (
        MafPipeline(runtime.board)
        .llm(token_reporter=runtime.meters.report_llm_tokens)
        .workers(1)
        .stage(
            "classify",
            workflow=build_classifier_workflow,
            active_status="classifying",
            success_status="classified",
            failure_status="classify_failed",
            max_working_time=5.0,
        )
        .chief(goal_key="tickets_goal")
        .build()
    )
    return runtime, pipeline


def main() -> int:
    runtime, pipeline = build_runtime_and_pipeline()
    _seed(runtime)

    final = runtime.sponsor(
        AllOf(
            QueueDepthSponsor("tickets_queue", name="queue"),
            LlmTokenBudgetSponsor(5_000, name="tokens"),
            DeadlineSponsor.from_now(minutes=2, name="deadline"),
        )
    ).run(pipeline)

    tasks = final.get("tasks", [])
    print(json.dumps({"tasks": tasks, "sponsor": final.get("data", {}).get("_sponsor_status")}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
