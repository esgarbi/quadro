"""
Shared infrastructure for Microsoft Agent Framework examples.

Centralises LLM client creation, prompt loading, output cleanup, the
single-agent workflow runner, and tools-layer helpers (idle-worker lookup,
fire-and-forget dispatch, acknowledgement tracking, batch dispatch).

Each example imports from this module instead of duplicating the code.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Callable
from pathlib import Path
from uuid import uuid4

from dotenv import load_dotenv

load_dotenv()


from agent_framework import (  # noqa: E402  (must import after load_dotenv so env vars are populated)
    AgentExecutorRequest,
    Message,
    WorkflowBuilder,
    WorkflowContext,
    WorkflowEvent,
    executor,
)
from agent_framework.openai import OpenAIChatClient  # noqa: E402

logger = logging.getLogger(__name__)

# ── LLM client factory ─────────────────────────────────────────────────────────


def create_llm_client(
    api_key: str = "",
    base_url: str = "",
    model_id: str = "",
) -> OpenAIChatClient:
    """Create an OpenAIChatClient with sensible defaults for local sglang."""
    resolved_key = os.environ.get("OPENAI_API_KEY", api_key)
    if not resolved_key:
        raise RuntimeError(
            "OPENAI_API_KEY not set. Copy .env.example to .env and add your key."
        )
    return OpenAIChatClient(
        model=os.environ.get("OPENAI_MODEL_ID", model_id),
        api_key=resolved_key,
        base_url=os.environ.get("OPENAI_BASE_URL", base_url),
    )


# ── Prompt loader ───────────────────────────────────────────────────────────────


def load_prompt(prompts_dir: Path, name: str) -> str:
    """Read a prompt markdown file from *prompts_dir*."""
    return (prompts_dir / f"{name}.md").read_text()


# ── Output cleanup ──────────────────────────────────────────────────────────────

REASONING_TOKEN_RE = re.compile(r"<\|[^|>]+\|>.*?(?=<\|[^|>]+\|>|$)", re.DOTALL)
JSON_FENCE_RE = re.compile(r"```(?:json)?\s*([\s\S]*?)```")


def clean_llm_output(text: str) -> str:
    """Strip reasoning tokens and markdown fences from LLM output."""
    cleaned = REASONING_TOKEN_RE.sub("", text)
    fence_match = JSON_FENCE_RE.search(cleaned)
    if fence_match:
        cleaned = fence_match.group(1)
    return cleaned.strip()


# ── Single-agent workflow runner ────────────────────────────────────────────────


async def run_single_agent(
    instructions: str,
    user_message: str,
    client_factory: Callable[[], OpenAIChatClient] = create_llm_client,
    default_options: dict | None = None,
    executor_prefix: str = "_agent",
) -> str:
    """Run a single-agent workflow and return the final text output.

    Parameters
    ----------
    instructions:
        System prompt for the agent.
    user_message:
        The user message that triggers the workflow.
    client_factory:
        Zero-arg callable returning an ``OpenAIChatClient``.  Defaults to
        :func:`create_llm_client` (local sglang).
    default_options:
        Optional dict passed as ``default_options`` to the agent (e.g.
        ``{"response_format": ...}``).
    executor_prefix:
        Short prefix used in the executor/agent id for log readability.
    """
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

    opts: dict = default_options or {}

    agent = client.as_agent(
        name=f"{executor_prefix}_{uid}",
        instructions=instructions,
        default_options=opts,
    )
    wf = WorkflowBuilder(start_executor=_start).add_edge(_start, agent).build()
    events = await wf.run(message=user_message, stream=False)

    for event in events:
        if isinstance(event, WorkflowEvent) and event.type == "output":
            return clean_llm_output(event.data.text)

    raise RuntimeError("Workflow produced no output event")


# ── Chief workflow runner ───────────────────────────────────────────────────────


async def run_chief_workflow(
    board_summary: str,
    instructions: str,
    tools: list,
    client_factory: Callable[[], OpenAIChatClient] = create_llm_client,
    agent_name_prefix: str = "chief",
) -> str | None:
    """Run the chief's single-turn LLM workflow and return output text (if any).

    Encapsulates the repeated executor→agent→workflow→run pattern used by
    every ``build_chief_policy()`` implementation.
    """
    uid = uuid4().hex[:8]

    @executor(id=f"_chief_{uid}")
    async def _chief_start(
        trigger: str, ctx: WorkflowContext[AgentExecutorRequest]
    ) -> None:
        await ctx.send_message(
            AgentExecutorRequest(
                messages=[Message("user", [board_summary])],
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
    for event in events:
        if isinstance(event, WorkflowEvent) and event.type == "output":
            return clean_llm_output(event.data.text)
    return None


# ═══════════════════════════════════════════════════════════════════════════════
# Tools-layer helpers  (re-exported from quadro.dispatch)
# ═══════════════════════════════════════════════════════════════════════════════

from quadro.dispatch import (  # noqa: E402  (must import after load_dotenv so env vars are populated)
    ACKNOWLEDGED_KEY,
    acknowledge_task,
    dispatch_batch,
    find_idle_worker,
    fire_worker,
    get_acknowledged,
)

# Listing the public surface explicitly tells ruff these re-exports are
# intentional (the sibling newsroom/ordering_system packages import them via
# ``from shared import ...``).
__all__ = [
    "ACKNOWLEDGED_KEY",
    "acknowledge_task",
    "clean_llm_output",
    "create_llm_client",
    "dispatch_batch",
    "find_idle_worker",
    "fire_worker",
    "get_acknowledged",
    "load_prompt",
    "run_chief_workflow",
    "run_single_agent",
]
