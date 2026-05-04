"""
Estimator example using Claude pricing and a translation-shaped saga.

Run:
    export ANTHROPIC_API_KEY=sk-ant-...
    python examples/estimator/main.py

The dry-run estimator scans a heterogeneous 50-task queue, executes a bounded
sample against the real Anthropic API, and prints token/dollar projections with
variance. Use ``--run-all`` to execute the full queue after the estimate.
"""

from __future__ import annotations

import argparse
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

from quadro import Estimator, LifecycleBuilder, Pipeline, QuadroRuntime, Saga  # noqa: E402
from quadro.board.backends import SqliteBoardBackend  # noqa: E402
from quadro.sponsor import GoalSponsor  # noqa: E402
from quadro_anthropic import AnthropicReasoner  # noqa: E402

DEFAULT_MODEL = "claude-sonnet-4-6"

if load_dotenv is not None:
    load_dotenv(Path(__file__).resolve().parents[1] / "anthropic_minimal" / ".env")

TRANSLATION_PROFILE = (
    LifecycleBuilder()
    .phase("UNASSIGNED", "translating")
    .phase("translating", "translated")
    .build()
)


class Translation(BaseModel):
    translated_text: str = Field(description="The translated article text")
    quality_notes: list[str] = Field(description="Brief notes on translation choices")


def _extract_text(ctx):
    notes = ctx.task.get("notes") or []
    return {
        "text": notes[0] if notes else "",
        "target_language": ctx.task.get("target_language", "Spanish"),
    }


def _persist_translation(ctx):
    board_fn = ctx.task["_board_fn"]
    result: Translation = ctx.step["translate"]
    board_fn(
        "board.update_task",
        {
            "task_id": ctx.task["task_id"],
            "to_status": "translated",
            "output": result.model_dump_json(),
        },
    )
    return {"persisted": True}


translation_saga = (
    Saga("translate_article")
    .deterministic("extract_text", _extract_text)
    .reason(
        "translate",
        prompt="Translate the supplied article into the target language. Preserve meaning.",
        user_message=lambda ctx: ctx.step["extract_text"],
        schema=Translation,
    )
    .deterministic("persist_translation", _persist_translation)
    .build()
)

ARTICLES = [
    "Quadro separates durable coordination from stage-local work.",
    "Cost visibility matters because production queues turn small per-call waste into real spend.",
    (
        "Governed multi-agent systems need repeatable state transitions, clear ownership, "
        "and recovery paths when workers fail halfway through a task."
    ),
    (
        "A dry-run estimator scans the shape of queued work, samples representative "
        "items, and projects total cost with confidence intervals instead of pretending "
        "a point estimate is enough."
    ),
    (
        "Enterprise CRM teams often process heterogeneous records: terse lead notes, "
        "long account histories, multilingual call summaries, and richly structured "
        "opportunity updates. The input-size spread is exactly why variance belongs in "
        "the projection, not in an afterthought."
    ),
]
LANGUAGES = [
    "Spanish",
    "French",
    "German",
    "Italian",
    "Portuguese",
    "Japanese",
    "Korean",
    "Arabic",
    "Hindi",
    "Swedish",
]


def build_queue() -> list[dict]:
    tasks = []
    for article_index, article in enumerate(ARTICLES):
        for language in LANGUAGES:
            tasks.append(
                {
                    "task_type": "translation",
                    "label": f"Translate article {article_index + 1} to {language}",
                    "notes": [article],
                    "target_language": language,
                }
            )
    return tasks


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-all", action="store_true", help="Run the full queue")
    args = parser.parse_args()

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ERROR: ANTHROPIC_API_KEY environment variable not set.")
        sys.exit(1)

    model = os.environ.get("ANTHROPIC_MODEL_ID", DEFAULT_MODEL)
    runtime = (
        QuadroRuntime(SqliteBoardBackend("anthropic_minimal.db"))
        .with_profiles(
            profile_resolver={"translation": "translation"},
            custom_profiles={"translation": TRANSLATION_PROFILE},
        )
        .with_pricing(
            {
                model: {
                    "input": 3.0,
                    "output": 15.0,
                    "io_ratio": 0.30,
                }
            },
            verify_url="https://anthropic.com/pricing",
        )
    )
    board = runtime.board

    def client_factory():
        return Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    pipeline = (
        Pipeline(board)
        .reasoner(AnthropicReasoner(client_factory=client_factory, model=model))
        .workers(1)
        .stage("translate", saga=translation_saga, active_status="translating")
    )

    queue = build_queue()
    estimator = Estimator.from_dry_run(
        pipeline=pipeline,
        queue=queue,
        max_sample_cost_dollars=1.0,
        max_samples=6,
    )
    print(estimator.format())

    if args.run_all:
        for task in queue:
            runtime.client.post_task(
                task["task_type"],
                task["label"],
                notes=task["notes"],
                target_language=task["target_language"],
            )
        built = pipeline.build()
        runtime.sponsor(
            GoalSponsor(
                lambda state: sum(
                    1 for task in state.get("tasks", []) if task.get("status") == "translated"
                )
                >= len(queue)
            )
        ).poll_every(1.0).run(built)


if __name__ == "__main__":
    main()
