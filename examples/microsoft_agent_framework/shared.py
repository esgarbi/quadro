"""
Shared infrastructure for Microsoft Agent Framework examples.

Centralises LLM client creation, prompt loading, output cleanup, the
single-agent workflow runner, and tools-layer helpers (idle-worker lookup,
fire-and-forget dispatch, acknowledgement tracking, batch dispatch).

Each example imports from this module instead of duplicating the code.
"""

from __future__ import annotations

import logging
import random
import re
import os
import threading
from collections.abc import Callable
from pathlib import Path
from uuid import uuid4
from dotenv import load_dotenv

load_dotenv()


from agent_framework import (
    AgentExecutorRequest,
    Message,
    WorkflowBuilder,
    WorkflowContext,
    WorkflowEvent,
    executor,
)
from agent_framework.openai import OpenAIChatClient

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
# Tools-layer helpers  (used by each project's tools.py)
# ═══════════════════════════════════════════════════════════════════════════════

ACKNOWLEDGED_KEY = "_acknowledged_failures"


def get_acknowledged(board_fn: Callable[[str, dict], dict]) -> set[str]:
    """Return the set of task_ids the chief has already acknowledged as failed."""
    try:
        result = board_fn("board.get_data", {"key": ACKNOWLEDGED_KEY})
        val = result.get("value") or []
        return set(val) if isinstance(val, list) else set()
    except Exception:
        return set()


def acknowledge_task(
    board_fn: Callable[[str, dict], dict],
    task_id: str,
) -> None:
    """Mark a *task_id* as acknowledged (stored in board data, not task state)."""
    try:
        acked = get_acknowledged(board_fn)
        acked.add(task_id)
        board_fn(
            "board.put_data",
            {"key": ACKNOWLEDGED_KEY, "value": list(acked)},
        )
    except Exception as exc:
        logger.warning("Failed to acknowledge task %s: %s", task_id[:8], exc)


def find_idle_worker(
    board_fn: Callable[[str, dict], dict],
    worker_registry: dict[str, list[tuple[str, str]]],
    capability: str,
) -> tuple[str, str] | None:
    """Return ``(agent_id, url)`` for a random IDLE worker, or ``None``."""
    workers = worker_registry.get(capability, [])
    if not workers:
        return None
    state = board_fn("board.get_full_state", {})
    idle_ids = {
        a["agent_id"] for a in state.get("agents", []) if a.get("status") == "IDLE"
    }
    idle_workers = [(aid, url) for aid, url in workers if aid in idle_ids]
    if not idle_workers:
        return None
    return random.choice(idle_workers)


def fire_worker(network, url: str, task_id: str) -> None:
    """Dispatch a task to a worker in a daemon thread -- fire and forget.

    The board transition MUST be written before calling this function.
    """
    from quadro.a2a.contracts import A2ARequest

    def _run() -> None:
        try:
            network.request(
                url,
                A2ARequest(
                    intent="worker.execute_task",
                    payload={"task_id": task_id},
                ).to_dict(),
            )
        except Exception as exc:
            logger.warning("Worker dispatch error task=%s: %s", task_id[:8], exc)

    threading.Thread(target=_run, daemon=True, name=f"worker-{task_id[:8]}").start()


def dispatch_batch(
    board_fn: Callable[[str, dict], dict],
    network,
    worker_registry: dict[str, list[tuple[str, str]]],
    status_filter: str | set[str],
    target_status: str,
    capability: str,
) -> tuple[list[str], list[str]]:
    """Advance all tasks matching *status_filter* to *target_status*.

    Returns ``(dispatched_ids_short, skipped_ids_short)`` where each id is
    truncated to 8 chars for log-friendly output.

    *status_filter* may be a single status string or a set of statuses.
    """
    if isinstance(status_filter, str):
        status_filter = {status_filter}

    state = board_fn("board.get_full_state", {})
    eligible = [t for t in state.get("tasks", []) if t["status"] in status_filter]
    dispatched: list[str] = []
    skipped: list[str] = []

    for t in eligible:
        w = find_idle_worker(board_fn, worker_registry, capability)
        if w:
            agent_id, url = w
            board_fn(
                "board.update_task",
                {
                    "task_id": t["task_id"],
                    "to_status": target_status,
                    "assigned_to": agent_id,
                },
            )
            fire_worker(network, url, t["task_id"])
            dispatched.append(t["task_id"][:8])
        else:
            skipped.append(t["task_id"][:8])

    return dispatched, skipped
