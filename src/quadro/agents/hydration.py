from __future__ import annotations

import hashlib
import json
from typing import Any


def _stable_hash(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def hydrate_chief_context(
    board_state: dict[str, Any],
    triggering_event: dict[str, Any] | None,
) -> dict[str, Any]:
    payload = {
        "tasks": board_state["tasks"],
        "agents": board_state["agents"],
        "triggering_event": triggering_event,
        "data": board_state["data"],
    }
    return {
        "snapshot_hash": _stable_hash(payload),
        "payload": payload,
    }


def hydrate_worker_context(
    task: dict[str, Any], notes: list[str] | None = None
) -> dict[str, Any]:
    payload = {"task": task, "notes": notes or []}
    return {
        "snapshot_hash": _stable_hash(payload),
        "payload": payload,
    }
