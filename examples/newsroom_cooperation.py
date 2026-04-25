from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from quadro import (
    BoardClient,
    ChiefAgent,
    LocalA2ANetwork,
    QuadroBoard,
    RunLoop,
    WorkerAgent,
)
from quadro.board.backends.sqlite import SqliteBoardBackend
from quadro.sponsor import AllOf, GoalSponsor, TickBudgetSponsor


def _make_chain_policy(bc: BoardClient) -> Callable:
    """
    Policy: when a research task completes, create a downstream draft task.
    This is domain logic for this example — it lives here, not in the framework.
    """

    def policy(ctx: dict) -> None:
        tasks = ctx["payload"]["tasks"]
        for task in tasks:
            if task["task_type"] != "research" or task["status"] != "COMPLETE":
                continue
            source_note = f"source_research_task={task['task_id']}"
            already_chained = any(
                t["task_type"] == "draft" and source_note in t.get("notes", [])
                for t in tasks
            )
            if not already_chained:
                bc.post_task(
                    "draft",
                    f"Draft article from: {task['label']}",
                    notes=[source_note],
                )

    return policy


def main() -> None:
    network = LocalA2ANetwork()
    board = QuadroBoard(
        SqliteBoardBackend(),
        profile_resolver={
            "research": "fast",
            "draft": "review_required",
        },
        network=network,
    )
    bc = board.client()

    researcher = (
        WorkerAgent.builder("researcher_1", bc)
        .name("Researcher")
        .capability("research")
        .at("a2a://workers/researcher_1")
        .execute(
            lambda ctx, _: f"Research summary for: {ctx['payload']['task']['label']}"
        )
        .build()
    )
    writer = (
        WorkerAgent.builder("writer_1", bc)
        .name("Writer")
        .capability("draft")
        .at("a2a://workers/writer_1")
        .execute(
            lambda ctx, _: f"Draft article generated from: {ctx['payload']['task']['label']}"
        )
        .build()
    )
    reviewer = (
        WorkerAgent.builder("reviewer_1", bc)
        .name("Reviewer")
        .capability("review")
        .at("a2a://workers/reviewer_1")
        .execute(lambda ctx, _: "Approved by reviewer_1")
        .reviewer()
        .build()
    )
    for worker in (researcher, writer, reviewer):
        worker.register()

    chief = ChiefAgent.builder(bc).policy(_make_chain_policy(bc)).build()

    bc.post_task("research", "Investigate water crisis and prepare article")

    print("Newsroom flow started.")

    def _is_done(state: dict) -> bool:
        draft_tasks = [t for t in state["tasks"] if t["task_type"] == "draft"]
        return bool(draft_tasks) and all(t["status"] == "COMPLETE" for t in draft_tasks)

    final_state = (
        RunLoop(board, chief)
        .sponsor(AllOf(GoalSponsor(_is_done), TickBudgetSponsor(25)))
        .poll_every(0.0)
        .ombudsman_every(0.0)
        .run()
    )

    print("\nFinal tasks:")
    for t in final_state["tasks"]:
        print(f"- {t['task_type']} [{t['task_id']}] status={t['status']}")

    print("\nEvent trace:")
    for event in bc.stream_events():
        print(
            f"- #{event['sequence_id']} {event['event_type']} task={event['task_id']}"
        )


if __name__ == "__main__":
    main()
