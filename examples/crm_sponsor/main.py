"""CRM-gated Quadro runtime — the continuity story in one file.

A mocked CRM ticket drives a Quadro runtime's lifetime. The runtime:

1. Starts while the ticket is ``open``.
2. Drains when the ticket flips to ``in_review`` — no new tasks are picked
   up; in-flight ones finish.
3. Stops when the ticket becomes ``closed``.

This is what "Quadro runs as long as some primary source of truth wants it
to" looks like in code — the Sponsor answers to an external authority, not
a hard-coded predicate. Replace :class:`Crm` with a real CRM client and the
shape is unchanged.

Run::

    python examples/crm_sponsor/main.py
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
sys.path.insert(0, str(Path(__file__).parent))

from quadro import (
    ChiefAgent,
    LifecycleBuilder,
    LocalA2ANetwork,
    QuadroBoard,
    QuadroRuntime,
    WorkerPool,
)
from quadro.board.backends.sqlite import SqliteBoardBackend
from quadro.sponsor import (
    AllOf,
    CallableSponsor,
    Continue,
    DeadlineSponsor,
    Drain,
    Lease,
    Priority,
    Stop,
    TickBudgetSponsor,
)

from crm import Crm

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("crm_sponsor")


HERE = Path(__file__).parent
DB_PATH = HERE / "crm_demo.db"


def build_runtime() -> QuadroRuntime:
    if DB_PATH.exists():
        DB_PATH.unlink()
    runtime = QuadroRuntime(SqliteBoardBackend(str(DB_PATH))).with_profiles(
        profile_resolver={"work": "fast"},
    )
    return runtime


def build_pipeline(runtime: QuadroRuntime):
    def worker(context: dict, board_fn) -> str:
        task = context["payload"]["task"]
        board_fn(
            "worker.post_result",
            {
                "task_id": task["task_id"],
                "output": "ok",
                "agent_id": context.get("agent_id"),
            },
        )
        return "ok"

    bc = runtime.client
    pool = (
        WorkerPool(bc)
        .workers(2)
        .wakes("a2a://chief")
        .add("work", worker)
        .build()
    )
    chief = ChiefAgent.builder(bc).at("a2a://chief").build()
    ombudsman = pool.ombudsman()

    from types import SimpleNamespace

    return SimpleNamespace(chief=chief, ombudsman=ombudsman, pool=pool)


def make_crm_sponsor(crm: Crm):
    """Wrap the CRM in a :class:`CallableSponsor` that maps ticket status to
    ``Continue`` / ``Drain`` / ``Stop``.

    Every consultation reads the live ticket; the issued Lease is short
    (``ticks=3``) so the runtime checks back on the CRM promptly. In a real
    system you would use :class:`HttpSponsor` against the CRM's API; the
    shape of this function is the shape of that Sponsor's response parser.
    """

    def decide(ctx, prior):
        ticket = crm.ticket
        status = ticket.status
        reason = f"{ticket.ticket_id}:{status}:{ticket.reason}"
        if status == "open":
            return Continue(
                lease=Lease(
                    ticks=ctx.meters.ticks + 3,
                    source="crm",
                    reason=reason,
                ),
                reason=reason,
            )
        if status == "in_review":
            return Drain(deadline=None, reason=reason)
        return Stop(reason=reason)

    return CallableSponsor(decide, name="crm_ticket")


def main() -> int:
    runtime = build_runtime()
    pipeline = build_pipeline(runtime)

    # Plant some work.
    for i in range(5):
        runtime.client.post_task("work", f"Task {i}")

    # Start a mocked CRM with a scheduled ticket evolution.
    crm = Crm("TCKT-0001")
    crm.schedule(
        [
            (0.5, "in_review", "stakeholder review"),
            (2.0, "closed", "review complete"),
        ]
    )

    sponsor = AllOf(
        make_crm_sponsor(crm),
        # Defensive safety nets — shouldn't be hit in normal runs, but the
        # Sponsor layer makes them cheap and obvious.
        DeadlineSponsor.from_now(minutes=5),
        TickBudgetSponsor(500),
    )

    def _log_cycle(state: dict, cycle: int) -> None:
        tasks = state["tasks"]
        done = sum(1 for t in tasks if t["status"] == "COMPLETE")
        active = sum(
            1
            for t in tasks
            if t["status"] not in {"COMPLETE", "HUMAN_REVIEW", "ON_HOLD", "UNASSIGNED"}
        )
        sponsor_status = state["data"].get("_sponsor_status") or {}
        logger.info(
            "[cycle %3d]  done=%d/%d  active=%d  draining=%s",
            cycle,
            done,
            len(tasks),
            active,
            sponsor_status.get("draining"),
        )

    final = (
        runtime.sponsor(sponsor)
        .on_cycle(_log_cycle)
        .poll_every(0.1)
        .ombudsman_every(1.0)
        .run(pipeline)
    )

    tasks = final["tasks"]
    done = sum(1 for t in tasks if t["status"] == "COMPLETE")
    log = final["data"].get("_sponsor_log") or []
    decisions = [e["decision"] for e in log]

    print("\n" + "=" * 60)
    print("  CRM-gated Quadro run complete")
    print(f"  Tasks completed:   {done}/{len(tasks)}")
    print(f"  Sponsor decisions: {decisions}")
    print(f"  Ticket final:      {crm.ticket.status} ({crm.ticket.reason})")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
