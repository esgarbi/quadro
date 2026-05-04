"""
Minimal Quadro example using Claude as the reasoner.

Demonstrates the AnthropicReasoner adapter plugging into a saga through the
substrate's standard Reasoner protocol seam. No framework adapters needed:
just quadro plus quadro_anthropic.

The example also surfaces token usage at the end of the run — by design.
Quadro's "measure waste, not just monitor it" framing means every example
should make cost visible at completion time, not bury it behind UI clicks.
The same numbers shown here are what the Board UI's Costs tab renders in
real-time during longer runs.

Run:
    export ANTHROPIC_API_KEY=sk-ant-...
    export ANTHROPIC_MODEL_ID=claude-sonnet-4-6  # optional
    python examples/anthropic_minimal/main.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from anthropic import Anthropic
from pydantic import BaseModel, Field

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - optional local convenience only
    load_dotenv = None  # type: ignore[assignment]

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from quadro import LifecycleBuilder, Pipeline, QuadroRuntime, Saga  # noqa: E402
from quadro.board.backends import SqliteBoardBackend  # noqa: E402
from quadro.sponsor import GoalSponsor  # noqa: E402
from quadro_anthropic import AnthropicReasoner  # noqa: E402


DEFAULT_EXAMPLE_MODEL = "claude-sonnet-4-6"

if load_dotenv is not None:
    load_dotenv(Path(__file__).resolve().parent / ".env")

ARTICLE_PROFILE = (
    LifecycleBuilder()
    .phase("UNASSIGNED", "pending")
    .phase("pending", "summarized")
    .build()
)


class Summary(BaseModel):
    headline: str = Field(description="A 5-10 word headline summarizing the article")
    key_points: list[str] = Field(description="3-5 key points from the article")


def _extract_text(ctx):
    """Read the article text from task notes."""
    notes = ctx.task.get("notes") or []
    return notes[0] if notes else "No article provided."


def _persist_summary(ctx):
    """Write the structured summary back to the task and mark it complete."""
    board_fn = ctx.task["_board_fn"]
    summary: Summary = ctx.step["summarize"]
    board_fn(
        "board.update_task",
        {
            "task_id": ctx.task["task_id"],
            "to_status": "summarized",
            "output": summary.model_dump_json(),
        },
    )
    return {"persisted": True}


summarize_saga = (
    Saga("summarize")
    .deterministic("extract_text", _extract_text)
    .reason(
        "summarize",
        prompt="You are an expert technical editor. Summarize the article concisely.",
        user_message=lambda ctx: ctx.step["extract_text"],
        schema=Summary,
    )
    .deterministic("persist_summary", _persist_summary)
    .build()
)


EXAMPLE_ARTICLE = """
The Quadro project is a governed coordination substrate for multi-agent LLM
systems. It treats coordination as a first-class concern, separating the "what
should happen next" question, handled by a reactive Chief agent reading from a
durable Board, from the "how does work get done inside a stage" question,
handled by sagas: declarative pipelines with retries, validations, and
compensation rollback. The substrate is framework-neutral. It has zero
LLM-framework imports in its core package, and adapter packages plug in via a
small structural protocol.
""".strip()


def _format_tokens(n: int) -> str:
    """Format a token count with K/M suffix.

    Mirrors the Board UI's `formatTokens` convention so the numbers shown
    here read identically to the Costs tab. Below 1000 -> raw integer with
    comma separators; 1000-9999 -> one decimal K; 10000+ -> integer K;
    1_000_000+ -> one decimal M.
    """
    if n < 1000:
        return f"{n:,}"
    if n < 10_000:
        return f"{n / 1000:.1f}K"
    if n < 1_000_000:
        return f"{round(n / 1000)}K"
    return f"{n / 1_000_000:.1f}M"


def _print_token_usage(client, task_id: str) -> None:
    """Print the same token data the Board UI's Costs tab would show.

    Quadro persists per-step token records to the Board automatically when
    a reason step completes. The same data feeds the Costs tab's per-stage
    bar, the per-task drawer, and this CLI report — three views, one source
    of truth.
    """
    records = client.token_records(task_id=task_id)
    if not records:
        print("\n(no token records — reason step may not have completed)")
        return

    total = sum(int(r.get("token_total") or 0) for r in records)
    by_stage: dict[str, int] = {}
    for r in records:
        stage = r.get("stage") or "—"
        by_stage[stage] = by_stage.get(stage, 0) + int(r.get("token_total") or 0)

    print("\n=== Token usage ===\n")
    print(f"Total: {_format_tokens(total)} tokens across {len(records)} reason step(s)")
    print()

    # Per-step table — same shape as the drawer's Token usage section.
    name_w = max(len(r.get("step_name") or "") for r in records)
    stage_w = max(len(r.get("stage") or "") for r in records)
    reasoner_w = max(len(r.get("reasoner_id") or "") for r in records)
    print(
        f"  {'STEP':<{name_w}}  {'STAGE':<{stage_w}}  "
        f"{'REASONER':<{reasoner_w}}  TOKENS"
    )
    print(f"  {'-' * name_w}  {'-' * stage_w}  {'-' * reasoner_w}  {'-' * 6}")
    for r in records:
        step = r.get("step_name") or ""
        stage = r.get("stage") or ""
        reasoner = r.get("reasoner_id") or ""
        tokens = _format_tokens(int(r.get("token_total") or 0))
        print(
            f"  {step:<{name_w}}  {stage:<{stage_w}}  "
            f"{reasoner:<{reasoner_w}}  {tokens:>6}"
        )

    if len(by_stage) > 1:
        print("\n  By stage:")
        for stage, n in sorted(by_stage.items(), key=lambda kv: kv[1], reverse=True):
            pct = (n / total * 100) if total else 0
            print(f"    {stage:<12} {_format_tokens(n):>8}  ({pct:.1f}%)")


def main() -> None:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ERROR: ANTHROPIC_API_KEY environment variable not set.")
        print("Get a key at https://console.anthropic.com/ and export it.")
        sys.exit(1)

    runtime = QuadroRuntime(SqliteBoardBackend("anthropic_minimal.db")).with_profiles(
        profile_resolver={"article": "article"},
        custom_profiles={"article": ARTICLE_PROFILE},
    )
    board = runtime.board

    def client_factory():
        return Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    model = os.environ.get("ANTHROPIC_MODEL_ID", DEFAULT_EXAMPLE_MODEL)
    pipeline = (
        Pipeline(board)
        .reasoner(AnthropicReasoner(client_factory=client_factory, model=model))
        .workers(1)
        .stage("summarize", saga=summarize_saga, active_status="pending")
        .build()
    )

    posted = runtime.client.post_task(
        "article",
        "Quadro project overview",
        notes=[EXAMPLE_ARTICLE],
    )

    final_state = (
        runtime.sponsor(
            GoalSponsor(
                lambda state: any(
                    t.get("status") == "summarized" for t in state.get("tasks", [])
                )
            )
        )
        .poll_every(1.0)
        .run(pipeline)
    )

    summarized = [
        t for t in final_state.get("tasks", []) if t.get("status") == "summarized"
    ]
    if not summarized:
        print("No task reached 'summarized' status.")
        sys.exit(1)

    print("\n=== Summary ===\n")
    print(f"Model: {model}\n")
    print(summarized[0].get("output"))

    # Surface token usage by reading the Board records phase one persists.
    # This is the canonical Quadro pattern: token data lives on the Board
    # as structured records; any consumer (CLI, UI, custom dashboard) reads
    # the same data through BoardClient aggregator methods. The numbers
    # below match what `python -m quadro.ui anthropic_minimal.db --open`
    # shows in the Costs tab.
    task_id = summarized[0].get("task_id") or (posted or {}).get("task_id")
    if task_id:
        _print_token_usage(runtime.client, task_id)


if __name__ == "__main__":
    main()
