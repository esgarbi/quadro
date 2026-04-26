"""Shared reporting helpers for llm_token_budget example artefacts."""

from __future__ import annotations

import json
import re
from typing import Any

_TICKET_ID_RE = re.compile(r"Ticket\s+([^:\s]+)\s*:")


def _ticket_id_for_task(task: dict[str, Any]) -> str | None:
    metadata = task.get("metadata")
    if isinstance(metadata, dict):
        meta_ticket_id = metadata.get("ticket_id")
        if meta_ticket_id is not None:
            return str(meta_ticket_id)

    objective = str(task.get("objective") or "")
    match = _TICKET_ID_RE.search(objective)
    if match:
        return match.group(1)
    return None


def _parse_classifier_output(task: dict[str, Any]) -> tuple[str | None, str | None, str | None]:
    raw = task.get("output") or ""
    if task.get("status") != "classified" or not raw:
        return None, None, None

    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None, None, None

    if not isinstance(parsed, dict):
        return None, None, None

    return (
        parsed.get("urgency"),
        parsed.get("category"),
        parsed.get("suggested_reply"),
    )


def build_ticket_records(
    final_tasks: list[dict[str, Any]],
    tickets: list[dict[str, Any]],
    *,
    logger: Any | None = None,
) -> list[dict[str, Any]]:
    """Build ticket report records using task-linked IDs instead of list order."""
    tickets_by_id = {str(ticket["id"]): ticket for ticket in tickets}
    records: list[dict[str, Any]] = []
    unresolved = 0

    for task in final_tasks:
        ticket_id = _ticket_id_for_task(task)
        ticket = tickets_by_id.get(ticket_id) if ticket_id else None
        if ticket is None:
            unresolved += 1

        urgency, category, suggested_reply = _parse_classifier_output(task)
        raw_output = task.get("output") or ""
        records.append(
            {
                "task_id": task.get("task_id"),
                "ticket_id": ticket["id"] if ticket else ticket_id,
                "subject": ticket["subject"] if ticket else None,
                "body": ticket["body"] if ticket else None,
                "status": task.get("status"),
                "urgency": urgency,
                "category": category,
                "suggested_reply": suggested_reply,
                "raw_output": raw_output or None,
            }
        )

    if unresolved and logger is not None and hasattr(logger, "warning"):
        logger.warning(
            "Report mapping could not resolve %d task(s) to a ticket ID", unresolved
        )

    return records
