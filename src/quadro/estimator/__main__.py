"""CLI implementation for estimator reports."""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path
from typing import Any

from .estimator import Estimator, _format_tokens
from .pricing import Pricing


class _SqliteTokenClient:
    def __init__(self, path: str) -> None:
        self._conn = sqlite3.connect(path)
        self._conn.row_factory = sqlite3.Row

    def token_records(self, *, task_id: str | None = None) -> list[dict[str, Any]]:
        prefix = f"_token_record:{task_id}:" if task_id else "_token_record:"
        rows = self._conn.execute(
            "SELECT value_json FROM data_entries WHERE key LIKE ? ORDER BY updated_at ASC",
            (f"{prefix}%",),
        ).fetchall()
        records = [json.loads(row["value_json"]) for row in rows]
        return [record for record in records if isinstance(record, dict)]

    def get_data(self, key: str) -> object:
        row = self._conn.execute(
            "SELECT value_json FROM data_entries WHERE key=?",
            (key,),
        ).fetchone()
        return json.loads(row["value_json"]) if row else None


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Estimate Quadro token and dollar costs")
    parser.add_argument("board_db", help="Path to a Quadro SQLite Board database")
    parser.add_argument("--project-tasks", type=int, default=None)
    parser.add_argument("--no-project", action="store_true")
    parser.add_argument("--pricing-file", type=Path)
    parser.add_argument("--confidence", type=float, default=0.95)
    args = parser.parse_args(argv)

    pricing = None
    if args.pricing_file is not None:
        pricing = Pricing.from_dict(json.loads(args.pricing_file.read_text()))

    client = _SqliteTokenClient(args.board_db)
    if args.no_project:
        print(_format_historical_report(client, pricing=pricing))
        return

    estimator = Estimator.from_history(client, pricing=pricing, confidence=args.confidence)
    n_tasks = args.project_tasks or estimator.default_n_tasks
    print(estimator.format(projection=estimator.project(n_tasks=n_tasks)))


def _format_historical_report(
    client: _SqliteTokenClient,
    *,
    pricing: Pricing | None,
) -> str:
    records = client.token_records()
    by_task: dict[str, int] = {}
    by_stage: dict[str, int] = {}
    by_reasoner: dict[str, int] = {}
    total = 0
    for record in records:
        tokens = int(record.get("token_total") or 0)
        total += tokens
        task_id = str(record.get("task_id") or "<unknown>")
        by_task[task_id] = by_task.get(task_id, 0) + tokens
        stage = record.get("stage")
        reasoner = record.get("reasoner_id")
        if stage:
            by_stage[str(stage)] = by_stage.get(str(stage), 0) + tokens
        if reasoner:
            by_reasoner[str(reasoner)] = by_reasoner.get(str(reasoner), 0) + tokens

    if pricing is None:
        raw = client.get_data("_pricing")
        if isinstance(raw, dict):
            pricing = Pricing.from_dict(raw)

    task_count = len(by_task)
    lines = ["=== Token usage (historical) ===", ""]
    lines.append(
        f"Total: {_format_tokens(total)} tokens across {task_count} task(s) "
        f"({len(records)} reason step records)"
    )
    lines.append(
        f"Average per task: {_format_tokens(round(total / task_count))} tokens"
        if task_count
        else "Average per task: 0 tokens"
    )
    if pricing is not None:
        model = next(iter(pricing.models.keys()))
        lines.append(f"Approx dollars: ${pricing.cost_for_tokens(model, total):.2f}")
    lines.append("")
    lines.append("By stage:")
    for stage, tokens in sorted(by_stage.items(), key=lambda item: item[1], reverse=True):
        pct = (tokens / total * 100) if total else 0
        lines.append(f"  {stage:<12} {_format_tokens(tokens):>8}  ({pct:.1f}%)")
    if by_reasoner:
        lines.append("")
        lines.append("By reasoner:")
        for reasoner, tokens in sorted(
            by_reasoner.items(), key=lambda item: item[1], reverse=True
        ):
            pct = (tokens / total * 100) if total else 0
            lines.append(f"  {reasoner:<12} {_format_tokens(tokens):>8}  ({pct:.1f}%)")
    if pricing is not None:
        lines.append("")
        lines.append(f"Pricing source: {pricing.source_label}")
        if pricing.verify_url:
            lines.append(f"Verify current rates at {pricing.verify_url}")
    return "\n".join(lines)


if __name__ == "__main__":
    main()
