"""Render two run.json artefacts from main.py into a combined REPORT.md.

Pure stdlib, no runtime dependency on quadro or agent-framework. Designed
to be idempotent: given the same two run.json inputs, it always produces
the same markdown.

Usage::

    python render_report.py \\
        --generous output/generous/run.json \\
        --tight    output/tight/run.json \\
        --out      output_sample/REPORT.md

Each run.json must have the shape main.py writes (see
``_write_run_json`` in main.py): ``meta``, ``summary``, ``sponsor_log``,
``tickets``.
"""

from __future__ import annotations

import argparse
import json
import sys
import textwrap
from pathlib import Path
from typing import Any

BAR_WIDTH = 36
REPLY_WIDTH = 68


# ─── Rendering helpers ───────────────────────────────────────────────────────


def _bar(used: int, total: int, width: int = BAR_WIDTH) -> str:
    if total <= 0:
        return "." * width
    pct = min(1.0, used / total)
    filled = int(round(pct * width))
    return "#" * filled + "." * (width - filled)


def _fmt_tokens(n: int) -> str:
    return f"{n:,}"


def _truncate(text: str | None, width: int = REPLY_WIDTH) -> str:
    if not text:
        return "-"
    collapsed = " ".join(text.split())
    if len(collapsed) <= width:
        return collapsed
    return collapsed[: width - 1].rstrip() + "\u2026"


def _md_escape(text: str) -> str:
    """Escape pipe so a one-line reply survives a markdown table cell."""
    return text.replace("|", "\\|")


# ─── Section builders ────────────────────────────────────────────────────────


def _headline_table(generous: dict, tight: dict) -> str:
    rows = []
    for label, run in (("Generous", generous), ("Tight", tight)):
        meta = run["meta"]
        summary = run["summary"]
        rows.append(
            "| {label:<8} | {budget:>7,} | {classified:>2} / {total:<2} | "
            "{tokens:>7} ({util:>5.1f}%) | {decision:<8} | {wall:>5.1f} s | {reason} |".format(
                label=label,
                budget=summary["budget"],
                classified=summary["classified"],
                total=meta["total_tickets"],
                tokens=_fmt_tokens(summary["tokens_used"]),
                util=summary["budget_utilisation_pct"],
                decision=(meta["final_decision"] or "-").capitalize(),
                wall=meta["wall_time_s"],
                reason=(meta["final_reason"] or "-"),
            )
        )
    return "\n".join(
        [
            "| Run      | Budget  | Classified | Tokens used       | Decision | Wall time | Reason |",
            "|----------|--------:|-----------:|------------------:|:---------|----------:|:-------|",
            *rows,
        ]
    )


def _decision_table(run: dict) -> str:
    rows: list[str] = []
    for idx, entry in enumerate(run["sponsor_log"]):
        meters = entry.get("meters") or {}
        tokens_at = int(meters.get("llm_tokens") or 0)
        reason = entry.get("reason") or "-"
        # Trim the always-present "all_of:" prefix and any deadline suffix for readability.
        if reason.startswith("all_of:"):
            reason = reason[len("all_of:") :]
        reason = reason.split(" & deadline:")[0]
        rows.append(
            f"| {idx:>5} | {entry['decision']:<8} | {tokens_at:>9,} | {reason} |"
        )
    return "\n".join(
        [
            "| Cycle | Decision | Tokens at | Reason |",
            "|------:|:---------|----------:|:-------|",
            *rows,
        ]
    )


def _tickets_table(run: dict) -> str:
    rows: list[str] = []
    for t in run["tickets"]:
        status = t.get("status") or "-"
        if status == "classified":
            urgency = t.get("urgency") or "-"
            category = t.get("category") or "-"
            reply = _md_escape(_truncate(t.get("suggested_reply")))
        else:
            urgency = category = "-"
            reply = f"_(not reached: status={status})_"
        rows.append(
            f"| {t['ticket_id']:<6} | {urgency:<8} | {category:<16} | {reply} |"
        )
    return "\n".join(
        [
            "| Ticket | Urgency  | Category         | Suggested reply (truncated) |",
            "|:-------|:---------|:-----------------|:----------------------------|",
            *rows,
        ]
    )


def _run_section(title: str, label: str, run: dict) -> str:
    meta = run["meta"]
    summary = run["summary"]
    bar = _bar(summary["tokens_used"], summary["budget"])
    header = (
        f"## {title}\n"
        f"\n"
        f"- **Budget:** `{summary['budget']:,}` tokens\n"
        f"- **Final decision:** `{meta['final_decision'] or '-'}`\n"
        f"- **Wall time:** {meta['wall_time_s']:.1f} s\n"
        f"- **Classified:** {summary['classified']} / {meta['total_tickets']} "
        f"({summary['failed']} failed)\n"
        f"- **Generated:** {meta['generated_at']}\n"
    )
    usage = (
        f"### Token usage\n"
        f"\n"
        f"```\n"
        f"[{bar}]  {_fmt_tokens(summary['tokens_used'])} / "
        f"{_fmt_tokens(summary['budget'])}  "
        f"({summary['budget_utilisation_pct']}%)\n"
        f"```\n"
    )
    return (
        f"{header}\n"
        f"{usage}\n"
        f"### Sponsor decision chain\n\n"
        f"{_decision_table(run)}\n\n"
        f"### Classifier outputs\n\n"
        f"{_tickets_table(run)}"
    )


# ─── Top-level renderer ──────────────────────────────────────────────────────


def render_report(generous: dict, tight: dict) -> str:
    gs = generous["summary"]
    ts = tight["summary"]
    gmeta = generous["meta"]
    tmeta = tight["meta"]

    model = gmeta.get("model") or tmeta.get("model") or "<unset>"
    endpoint = gmeta.get("endpoint") or tmeta.get("endpoint") or "<unset>"

    parts: list[str] = []
    parts.append("# Token-Budget Run Report")
    parts.append("")
    parts.append(
        f"*Real runs of `examples/microsoft_agent_framework/llm_token_budget/main.py` against `{model}` at `{endpoint}`.*"
    )
    parts.append("")
    parts.append("## Two runs, one binary, two termination paths")
    parts.append("")
    parts.append(_headline_table(generous, tight))
    parts.append("")
    parts.append(
        f"The **generous** run classified all {gmeta['total_tickets']} tickets "
        f"spending only **{gs['budget_utilisation_pct']}%** of the budget; "
        f"the **tight** run was stopped by the sponsor at "
        f"**{_fmt_tokens(ts['tokens_used'])} tokens** "
        f"({ts['budget_utilisation_pct']}% of its `{ts['budget']}` budget — "
        "the overshoot is the cost of concurrency: workers already in "
        "flight when the sponsor sampled below the ceiling). "
        "Both outcomes come from the same "
        "`AllOf(QueueDepthSponsor, LlmTokenBudgetSponsor, DeadlineSponsor)` "
        "composition; only `LLM_BUDGET` differs."
    )
    parts.append("")
    parts.append(_run_section("Run 1 — Generous", "generous", generous))
    parts.append("")
    parts.append(_run_section("Run 2 — Tight", "tight", tight))
    parts.append("")
    parts.append(
        textwrap.dedent(
            """
            ## How tokens reach the sponsor

            ```mermaid
            flowchart LR
              W["MAF classifier turn"] -->|usage| M["token_reporter"]
              M --> B["runtime.meters"]
              B -->|snapshot on consult| S["LlmTokenBudgetSponsor"]
              S -->|Continue / Stop| R["RunLoop"]
            ```

            Every MAF turn (chief and classifier) emits a `usage` record in
            its event stream. The adapter extracts `prompt_tokens +
            completion_tokens` and calls the `token_reporter` you wired via
            `MafPipeline.llm(token_reporter=runtime.meters.report_llm_tokens)`.
            `LlmTokenBudgetSponsor` reads `ctx.meters.llm_tokens` on each
            consultation and halts the run when cumulative usage exceeds
            the budget.

            See [../README.md](../README.md) for the wiring details and
            [../../../../docs/guides/sponsor-decision-matrix.md](../../../../docs/guides/sponsor-decision-matrix.md)
            for the full sponsor cookbook.
            """
        ).strip()
    )
    parts.append("")
    parts.append(
        textwrap.dedent(
            """
            ## Reproduce

            ```bash
            cp .env.example .env            # set OPENAI_API_KEY / _MODEL_ID / _BASE_URL

            LLM_BUDGET=50000 python main.py --output-dir output/generous
            LLM_BUDGET=500   python main.py --output-dir output/tight

            python render_report.py \\
                --generous output/generous/run.json \\
                --tight    output/tight/run.json \\
                --out      output/REPORT.md
            ```
            """
        ).strip()
    )
    parts.append("")
    return "\n".join(parts)


# ─── CLI ─────────────────────────────────────────────────────────────────────


def _load(path: Path) -> dict[str, Any]:
    with path.open() as f:
        return json.load(f)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Merge two run.json artefacts into a combined REPORT.md"
    )
    parser.add_argument("--generous", type=Path, required=True)
    parser.add_argument("--tight", type=Path, required=True)
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Output path. If omitted, prints to stdout.",
    )
    args = parser.parse_args(argv)

    generous = _load(args.generous)
    tight = _load(args.tight)

    report = render_report(generous, tight)

    if args.out is None:
        sys.stdout.write(report)
        sys.stdout.write("\n")
    else:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(report + "\n")
        print(f"Wrote {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
