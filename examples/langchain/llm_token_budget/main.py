"""LLM token-budget demo — ticket triage under a hard token ceiling.

A queue of support tickets arrives, a single classifier stage powered by
LangChain (``ChatOpenAI`` via ``quadro.integrations.langchain``) tags
each one (``urgency``, ``category``, ``suggested_reply``), and the whole
run is governed by an ``AllOf`` composition of three sponsors:

1. :class:`~quadro.sponsor.QueueDepthSponsor` — primary. While there is
   backlog, keep working; when the queue empties, drain cleanly.
2. :class:`~quadro.sponsor.LlmTokenBudgetSponsor` — the star of the show.
   Cumulative LLM token usage is capped at ``LLM_BUDGET``; when that
   ceiling is hit, the sponsor returns ``Stop`` and the runtime halts.
3. :class:`~quadro.sponsor.DeadlineSponsor` — belt-and-braces wall-clock
   cut-off so a jammed LLM cannot keep the loop alive indefinitely.

Run it twice to see both termination paths::

    LLM_BUDGET=50000 python main.py   # queue empties first  -> Drain
    LLM_BUDGET=500   python main.py   # budget trips first   -> Stop

Token usage flows into the sponsor via the LangChain adapter's
``token_reporter`` hook, which reads ``AIMessage.usage_metadata`` (and
the provider-specific ``response_metadata["token_usage"]`` /
``["usage"]`` fallbacks) on every call and pushes the sum into the
runtime's shared ``MeterBundle``.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
import warnings
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))

from dotenv import load_dotenv

load_dotenv(Path(__file__).with_name(".env"))

from pydantic import BaseModel  # noqa: E402
from quadro import LifecycleBuilder, QuadroRuntime  # noqa: E402
from quadro.board.backends.sqlite import SqliteBoardBackend  # noqa: E402
from quadro.integrations.langchain import LangChainPipeline  # noqa: E402
from quadro.sponsor import (  # noqa: E402
    AllOf,
    DeadlineSponsor,
    LlmTokenBudgetSponsor,
    QueueDepthSponsor,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
# LangChain's ``ChatOpenAI`` uses ``httpx`` under the hood. When the outer
# RunLoop finalises, short-lived per-turn async clients can surface
# ``Event loop is closed`` messages from stale ``aclose()`` futures —
# harmless, but noisy. Suppress.
logging.getLogger("asyncio").setLevel(logging.CRITICAL)

# Post-stop shutdown race: once ``LlmTokenBudgetSponsor`` returns ``Stop``,
# the RunLoop begins tearing down workers and the chief. Meanwhile,
# ``ChatOpenAI.ainvoke`` calls already in flight (and chief policy
# coroutines queued for the next poll) land on a closed
# ``ThreadPoolExecutor`` and surface as:
#
#   quadro.agents.worker: Worker ...: execute_fn failed for task ...:
#       cannot schedule new futures after shutdown
#   quadro.pipeline: Chief policy error: cannot schedule new futures after shutdown
#   RuntimeWarning: coroutine 'Pipeline.build.<locals>._chief_policy' was never awaited
#
# The run is already over at this point — the final summary has printed,
# the sponsor log is written, and the exit code is stable. The two narrow
# filters below silence only these specific shutdown-race messages so
# debug-mode runs finish cleanly; everything else logs as usual.
class _SilenceShutdownRace(logging.Filter):
    _MATCHES = (
        "cannot schedule new futures after shutdown",
    )

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        return not any(m in msg for m in self._MATCHES)


for _name in ("quadro.agents.worker", "quadro.pipeline"):
    logging.getLogger(_name).addFilter(_SilenceShutdownRace())

warnings.filterwarnings(
    "ignore",
    message=r"coroutine 'Pipeline\.build\.<locals>\._chief_policy' was never awaited",
    category=RuntimeWarning,
)

logger = logging.getLogger("llm_token_budget")

HERE = Path(__file__).parent
DB_PATH = HERE / "triage.db"
TICKETS_PATH = HERE / "tickets.json"

# ── Configuration (read from .env / process env) ──────────────────────────────

LLM_BUDGET = int(os.environ.get("LLM_BUDGET", "5000"))
POLL_INTERVAL = float(os.environ.get("POLL_INTERVAL", "1.0"))
DEADLINE_MINUTES = int(os.environ.get("DEADLINE_MINUTES", "3"))

# ── Classification output schema ──────────────────────────────────────────────


class TicketTag(BaseModel):
    """Schema the classifier stage must return. LangChain enforces it via
    ``ChatOpenAI.with_structured_output`` and the stage marks the task
    ``classify_failed`` on a schema-validation miss."""

    urgency: Literal["low", "medium", "high", "critical"]
    category: Literal[
        "billing", "account", "outage", "feature_request", "other"
    ]
    suggested_reply: str


# ── Lifecycle: UNASSIGNED -> classifying -> classified | classify_failed ──────

TICKET_LIFECYCLE = (
    LifecycleBuilder()
    .step("UNASSIGNED", "classifying")
    .step("classifying", "classified")
    .branch("classifying", "classify_failed")
    .build()
)

_TERMINAL = frozenset({"classified", "classify_failed", "HUMAN_REVIEW", "ON_HOLD"})


# ── Helpers ───────────────────────────────────────────────────────────────────


def _render_bar(used: int, total: int, width: int = 24) -> str:
    """ASCII progress bar, clamped at 100% so overshoot is visible as a full bar."""
    if total <= 0:
        return "-" * width
    pct = min(1.0, used / total)
    filled = int(round(pct * width))
    return "#" * filled + "." * (width - filled)


def _count(tasks: list[dict], status: str) -> int:
    return sum(1 for t in tasks if t["status"] == status)


def _load_tickets() -> list[dict]:
    with TICKETS_PATH.open() as f:
        return json.load(f)


def _seed(runtime: QuadroRuntime, tickets: list[dict]) -> None:
    bc = runtime.client
    pending_ids: list[str] = []
    for ticket in tickets:
        task = bc.post_task(
            "classify",
            f"Ticket {ticket['id']}: {ticket['subject']}",
            notes=[ticket["body"]],
        )
        pending_ids.append(task["task_id"])
    # QueueDepthSponsor reads this list off board data each consult.
    # on_cycle keeps it in sync with the tasks still in flight.
    bc.put_data("tickets_queue", pending_ids)


# ── Pipeline assembly ─────────────────────────────────────────────────────────


def build_runtime_and_pipeline():
    if DB_PATH.exists():
        DB_PATH.unlink()

    runtime = QuadroRuntime(SqliteBoardBackend(str(DB_PATH))).with_profiles(
        profile_resolver={"classify": "ticket"},
        custom_profiles={"ticket": TICKET_LIFECYCLE},
    )

    pipeline = (
        LangChainPipeline(runtime.board)
        .llm(
            api_key_env="OPENAI_API_KEY",
            model_env="OPENAI_MODEL_ID",
            base_url_env="OPENAI_BASE_URL",
            # THE line that makes LlmTokenBudgetSponsor work: every
            # LangChain call (chief + classifier) now reports its usage
            # into the runtime's shared MeterBundle.
            token_reporter=runtime.meters.report_llm_tokens,
        )
        .workers(3)
        .capacity(1)
        .wakes("a2a://chief")
        .stage(
            "classify",
            prompt=HERE / "prompts" / "classify.md",
            output_schema=TicketTag,
            active_status="classifying",
            success_status="classified",
            failure_status="classify_failed",
            max_working_time=60.0,
        )
        .chief(prompt=HERE / "prompts" / "chief.md", goal_key="tickets_goal")
        .build()
    )

    return runtime, pipeline


# ── Run artefact serialisation ────────────────────────────────────────────────


def _ticket_records(
    final_tasks: list[dict], tickets: list[dict]
) -> list[dict]:
    """Pair each posted task with its source ticket and parse the classifier output.

    Order matches the order tickets were seeded in ``_seed`` — task N came
    from ticket N. This lets the report key by T-NNN without a cross-lookup.
    """
    records: list[dict] = []
    for ticket, task in zip(tickets, final_tasks, strict=False):
        raw = task.get("output") or ""
        urgency = category = suggested = None
        if task.get("status") == "classified" and raw:
            try:
                parsed = json.loads(raw)
                urgency = parsed.get("urgency")
                category = parsed.get("category")
                suggested = parsed.get("suggested_reply")
            except (json.JSONDecodeError, AttributeError):
                pass
        records.append(
            {
                "task_id": task.get("task_id"),
                "ticket_id": ticket["id"],
                "subject": ticket["subject"],
                "body": ticket["body"],
                "status": task.get("status"),
                "urgency": urgency,
                "category": category,
                "suggested_reply": suggested,
                "raw_output": raw or None,
            }
        )
    return records


def _write_run_json(
    *,
    output_dir: Path,
    final_state: dict,
    tickets: list[dict],
    tokens_final: int,
    budget: int,
    wall_time_s: float,
) -> Path:
    """Serialise the run into ``<output_dir>/run.json`` for report rendering."""
    output_dir.mkdir(parents=True, exist_ok=True)

    tasks = final_state["tasks"]
    classified = _count(tasks, "classified")
    failed = _count(tasks, "classify_failed")
    sponsor_log = final_state["data"].get("_sponsor_log") or []
    final_decision = sponsor_log[-1].get("decision") if sponsor_log else None
    final_reason = sponsor_log[-1].get("reason") if sponsor_log else None

    util_pct = round(100 * tokens_final / budget, 1) if budget else 0.0

    payload = {
        "meta": {
            "budget": budget,
            "model": os.environ.get("OPENAI_MODEL_ID", "<unset>"),
            "endpoint": os.environ.get("OPENAI_BASE_URL", "<unset>"),
            "total_tickets": len(tickets),
            "wall_time_s": round(wall_time_s, 2),
            "final_decision": final_decision,
            "final_reason": final_reason,
            "generated_at": datetime.now(timezone.utc).isoformat(
                timespec="seconds"
            ),
        },
        "summary": {
            "classified": classified,
            "failed": failed,
            "tokens_used": tokens_final,
            "budget": budget,
            "budget_utilisation_pct": util_pct,
        },
        "sponsor_log": sponsor_log,
        "tickets": _ticket_records(tasks, tickets),
    }

    path = output_dir / "run.json"
    with path.open("w") as f:
        json.dump(payload, f, indent=2)
    return path


# ── Run ───────────────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the LLM token-budget demo and optionally write artefacts."
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=HERE / "output",
        help=(
            "Directory to write run.json into. Created if missing. "
            "Defaults to ./output under the example folder, so a bare "
            "`python main.py` (including via an IDE debug launch) "
            "always produces artefacts."
        ),
    )
    args = parser.parse_args(argv)

    tickets = _load_tickets()
    total_tickets = len(tickets)

    runtime, pipeline = build_runtime_and_pipeline()

    runtime.put_data(
        "tickets_goal",
        {"total": total_tickets, "domain": "customer-support triage"},
    )
    _seed(runtime, tickets)

    sponsor = AllOf(
        QueueDepthSponsor("tickets_queue", name="queue"),
        LlmTokenBudgetSponsor(LLM_BUDGET),
        DeadlineSponsor.from_now(minutes=DEADLINE_MINUTES),
    )

    bc = runtime.client

    def on_cycle(state: dict, cycle: int) -> None:
        tasks = state["tasks"]
        done = _count(tasks, "classified")
        failed = _count(tasks, "classify_failed")
        active = sum(
            1
            for t in tasks
            if t["status"] not in _TERMINAL and t["status"] != "UNASSIGNED"
        )
        pending = [
            t["task_id"] for t in tasks if t["status"] not in _TERMINAL
        ]
        # Keep the queue key in sync so QueueDepthSponsor sees the real backlog.
        bc.put_data("tickets_queue", pending)

        status = state["data"].get("_sponsor_status") or {}
        meters = status.get("meters") or {}
        tokens = int(meters.get("llm_tokens") or 0)
        draining = status.get("draining")
        active_lease = (status.get("active_lease") or {}).get("source") or "-"

        bar = _render_bar(tokens, LLM_BUDGET)
        logger.info(
            "[cycle %3d] tickets=%2d/%-2d (failed=%d active=%d pending=%d)  "
            "tokens=[%s] %5d/%d  lease=%s%s",
            cycle,
            done,
            total_tickets,
            failed,
            active,
            len(pending),
            bar,
            tokens,
            LLM_BUDGET,
            active_lease,
            "  (DRAINING)" if draining else "",
        )

    logger.info(
        "LLM token-budget demo — tickets=%d  budget=%d  model=%s  endpoint=%s",
        total_tickets,
        LLM_BUDGET,
        os.environ.get("OPENAI_MODEL_ID", "<unset>"),
        os.environ.get("OPENAI_BASE_URL", "<unset>"),
    )

    started_at = time.monotonic()
    final = (
        runtime.sponsor(sponsor)
        .on_cycle(on_cycle)
        .poll_every(POLL_INTERVAL)
        .ombudsman_every(5.0)
        .run(pipeline)
    )
    wall_time_s = time.monotonic() - started_at

    # ── Summary ───────────────────────────────────────────────────────────────
    tasks = final["tasks"]
    done = _count(tasks, "classified")
    failed = _count(tasks, "classify_failed")

    log = final["data"].get("_sponsor_log") or []
    tokens_final = runtime.meters.snapshot().llm_tokens

    print("\n" + "=" * 72)
    print("  LLM token-budget demo complete")
    print("=" * 72)
    print(f"  Classified: {done}/{total_tickets}   (failed={failed})")
    print(f"  Tokens used: {tokens_final} / {LLM_BUDGET}")
    print(f"  Wall time:   {wall_time_s:.1f}s")
    print(f"  Sponsor decisions (last {min(len(log), 8)}):")
    for entry in log[-8:]:
        meters = entry.get("meters", {})
        tokens_at = int(meters.get("llm_tokens") or 0)
        print(
            f"    {entry['decision']:<8}  {entry.get('reason','')!r:<50}"
            f"  @ {tokens_at:>5} tok"
        )
    print("=" * 72)

    path = _write_run_json(
        output_dir=args.output_dir,
        final_state=final,
        tickets=tickets,
        tokens_final=tokens_final,
        budget=LLM_BUDGET,
        wall_time_s=wall_time_s,
    )
    print(f"  Wrote run artefact: {path}")

    # A budget-tripped run exits with a non-zero code so CI/operators can
    # distinguish "budget stopped us" from "queue naturally drained".
    last = log[-1] if log else {}
    if last.get("decision") == "stop" and "budget_exhausted" in last.get(
        "reason", ""
    ):
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
