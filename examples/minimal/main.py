"""
Minimal Quadro example - substrate-only with a bare-OpenAI adapter.

Demonstrates the smallest possible end-to-end Quadro pipeline:

  - A custom reasoner adapter (openai_reasoner.py) that wraps the OpenAI SDK
    directly, with no LLM framework.
  - A tiny saga: extract a question, ask the LLM to summarize, then persist.
  - A single-stage Pipeline driven by the deterministic chief.

Usage:
    export OPENAI_API_KEY=sk-...
    python main.py

Read openai_reasoner.py to see how a small adapter connects any LLM SDK to the
Reasoner protocol.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
sys.path.insert(0, str(Path(__file__).parent))

from quadro import LifecycleBuilder, Pipeline, QuadroRuntime, Saga  # noqa: E402
from quadro.board.backends import SqliteBoardBackend  # noqa: E402
from quadro.sponsor import GoalSponsor  # noqa: E402

from openai_reasoner import OpenAIReasoner  # noqa: E402

load_dotenv(Path(__file__).resolve().parent / ".env")


QUESTION_PROFILE = (
    LifecycleBuilder()
    .phase("UNASSIGNED", "summarizing")
    .phase("summarizing", "answered")
    .build()
)


def _extract_question(ctx):
    """Read the task notes for the question to answer."""
    notes = ctx.task.get("notes") or []
    return notes[0] if notes else "What is Quadro?"


def _persist_answer(ctx):
    """Write the LLM's answer back to the task and mark it complete."""
    board_fn = ctx.task["_board_fn"]
    answer = ctx.step["summarize"]
    board_fn(
        "board.update_task",
        {
            "task_id": ctx.task["task_id"],
            "to_status": "answered",
            "output": answer,
        },
    )
    return {"persisted": True}


summarize_saga = (
    Saga("summarize")
    .deterministic("extract_question", _extract_question)
    .reason(
        "summarize",
        prompt="You are a concise technical writer. Answer in one paragraph.",
        user_message=lambda ctx: ctx.step["extract_question"],
    )
    .deterministic("persist_answer", _persist_answer)
    .build()
)


def main() -> None:
    runtime = QuadroRuntime(SqliteBoardBackend(":memory:")).with_profiles(
        profile_resolver={"question": "question"},
        custom_profiles={"question": QUESTION_PROFILE},
    )
    board = runtime.board

    model = os.environ.get("OPENAI_MODEL_ID", "gpt-4o-mini")
    pipeline = (
        Pipeline(board)
        .reasoner(OpenAIReasoner(client=OpenAI(), model=model))
        .workers(1)
        .stage("summarize", saga=summarize_saga, active_status="summarizing")
        .build()
    )

    runtime.client.post_task(
        "question",
        "Quadro overview",
        notes=["What is Quadro and what problem does it solve?"],
    )

    final_state = (
        runtime.sponsor(
            GoalSponsor(
                lambda state: any(
                    t.get("status") == "answered" for t in state.get("tasks", [])
                )
            )
        )
        .poll_every(1.0)
        .run(pipeline)
    )

    answered = [
        t for t in final_state.get("tasks", []) if t.get("status") == "answered"
    ]
    if not answered:
        print("No task reached 'answered' status.")
        sys.exit(1)

    print("\n=== Answer ===\n")
    print(answered[0].get("output"))


if __name__ == "__main__":
    main()
