"""Minimal LangChain supervisor-stage proof for Quadro runtime plugins.

Demonstrates native stage entrypoint usage:

    stage(supervisor=build_supervisor, ...)

Quadro remains the governance control plane (lifecycle, sponsors, run
loop), while LangGraph/LangChain owns supervisor execution semantics.

A note on the saga DSL: this example deliberately stays as the
smallest possible demonstration of ``stage(supervisor=...)`` with
LangChain. Sagas would also work, but the value of this file is
being short — every line earns its keep, and adding a saga would
add lines without teaching anything new.
"""

from __future__ import annotations

import json
import os
import sys
from importlib import import_module
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from quadro import LifecycleBuilder, Pipeline, QuadroRuntime  # noqa: E402
from quadro.board.backends.sqlite import SqliteBoardBackend  # noqa: E402
from quadro.sponsor import AllOf, DeadlineSponsor, GoalSponsor, LlmTokenBudgetSponsor  # noqa: E402
from quadro_langchain import LangChainChiefRuntime, LangChainReasoner  # noqa: E402

HERE = Path(__file__).parent
DB_PATH = HERE / "supervisor_stage_minimal.db"

load_dotenv(HERE / ".env")

TICKET_LIFECYCLE = (
    LifecycleBuilder()
    .phase("UNASSIGNED", "classifying")
    .phase("classifying", "classified")
    .branch("classifying", "classify_failed")
    .build()
)


def _flatten_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for chunk in content:
            if isinstance(chunk, str):
                parts.append(chunk)
            elif isinstance(chunk, dict):
                maybe = chunk.get("text") or chunk.get("content")
                if isinstance(maybe, str):
                    parts.append(maybe)
        return "".join(parts)
    return "" if content is None else str(content)


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


def build_supervisor():
    """Return a supervisor runnable used by ``stage(supervisor=...)``."""
    try:
        from langchain.agents import create_agent
    except ImportError as exc:  # pragma: no cover - example runtime guard
        try:  # Backward-compatible fallback for environments with older helper.
            create_react_agent = getattr(
                import_module("langgraph.prebuilt"), "create_react_agent"
            )
        except Exception:
            raise RuntimeError(
                "This example requires langchain/langgraph agents support. "
                "Install: pip install quadro[langchain]"
            ) from exc
        else:
            create_agent = None
    from langchain_core.tools import tool

    @tool
    def ticket_policy_hint() -> str:
        """Return a compact policy the supervisor should follow."""
        return (
            "Return compact JSON with keys: urgency, category, suggested_reply. "
            "Urgency must be one of low|medium|high|critical."
        )

    instructions = (
        "You are a support triage supervisor. Classify the input ticket and "
        "respond with compact JSON only."
    )
    if create_agent is not None:
        try:
            agent = create_agent(
                model=_model_from_env(),
                tools=[ticket_policy_hint],
                system_prompt=instructions,
            )
        except TypeError:
            # Some intermediate versions still accept `prompt`.
            agent = create_agent(
                model=_model_from_env(),
                tools=[ticket_policy_hint],
                prompt=instructions,
            )
    else:
        agent = create_react_agent(
            model=_model_from_env(),
            tools=[ticket_policy_hint],
            prompt=instructions,
        )

    class _SupervisorAdapter:
        async def ainvoke(self, payload: dict[str, Any]) -> dict[str, Any]:
            text = str((payload or {}).get("input") or "{}")
            state = await agent.ainvoke(
                {"messages": [{"role": "user", "content": text}]}
            )
            messages = state.get("messages") if isinstance(state, dict) else None
            if not isinstance(messages, list):
                messages = []
            final_text = (
                _flatten_content(getattr(messages[-1], "content", ""))
                if messages
                else ""
            )
            return {
                "messages": messages,
                "output": {
                    "classifier_output": final_text or "{}",
                    "source": "langchain.create_agent",
                },
            }

    return _SupervisorAdapter()


def _seed(runtime: QuadroRuntime) -> None:
    runtime.put_data("tickets_goal", {"total": 1, "domain": "supervisor-stage-proof"})
    runtime.client.post_task(
        "classify",
        "Ticket T-001: Cannot access dashboard",
        notes=["User can log in, but dashboard requests fail with timeout."],
    )


def build_runtime_and_pipeline():
    if DB_PATH.exists():
        DB_PATH.unlink()

    runtime = QuadroRuntime(SqliteBoardBackend(str(DB_PATH))).with_profiles(
        profile_resolver={"classify": "ticket"},
        custom_profiles={"ticket": TICKET_LIFECYCLE},
    )

    token_reporter = runtime.meters.report_llm_tokens

    pipeline = (
        Pipeline(runtime.board)
        .reasoner(
            LangChainReasoner(
                client_factory=_model_from_env,
                token_reporter=token_reporter,
            )
        )
        .with_framework_runtime(
            LangChainChiefRuntime(
                client_factory=_model_from_env,
                token_reporter=token_reporter,
            )
        )
        .runtime_observability(token_reporter=token_reporter)
        .workers(1)
        .stage(
            "classify",
            supervisor=build_supervisor,
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

    done_when = lambda state: any(  # noqa: E731
        t["status"] in {"classified", "classify_failed"} for t in state.get("tasks", [])
    )
    final = runtime.sponsor(
        AllOf(
            GoalSponsor(done_when),
            LlmTokenBudgetSponsor(5_000, name="tokens"),
            DeadlineSponsor.from_now(minutes=2, name="deadline"),
        )
    ).run(pipeline)

    print(
        json.dumps(
            {
                "tasks": final.get("tasks", []),
                "sponsor": final.get("data", {}).get("_sponsor_status"),
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
