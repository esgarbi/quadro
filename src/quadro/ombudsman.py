from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .a2a.contracts import A2ARequest
from .a2a.dispatch import LocalA2ANetwork
from .board.records import TaskStatus, utc_now


class Ombudsman:
    """
    Scans IN_PROGRESS tasks and transitions those with a stale (or absent)
    heartbeat to STALE.  After marking a task STALE, the chief's existing
    STALE → UNASSIGNED logic handles reassignment on its next nudge.

    When ``working_statuses`` is provided, also scans tasks in those custom-
    profile statuses and transitions stale ones to FAILED (not STALE — custom
    profiles may not have STALE in their valid transitions, but they always
    have FAILED via ``build_custom_profile`` auto-expansion).
    """

    def __init__(
        self,
        *,
        network: LocalA2ANetwork,
        board_url: str,
        heartbeat_timeout_seconds: int = 3000,  # 50min — generous for LLM calls
        working_statuses: set[str] | None = None,
        status_timeouts: dict[str, int] | None = None,
    ) -> None:
        self.network = network
        self.board_url = board_url
        self.heartbeat_timeout_seconds = heartbeat_timeout_seconds
        self._working_statuses: frozenset[str] = frozenset(working_statuses or set())
        self._status_timeouts: dict[str, int] = dict(status_timeouts or {})

    def nudge(self) -> int:
        """
        Scan IN_PROGRESS tasks.  For each one where heartbeat_at is None or
        (utc_now() - heartbeat_at) > timeout, transition to STALE.
        Returns the count of tasks transitioned.
        """
        state_resp = self.network.request(
            self.board_url,
            A2ARequest(intent="board.get_full_state", payload={}).to_dict(),
        )
        if not state_resp["ok"]:
            raise RuntimeError(state_resp["error"])

        tasks = state_resp["result"]["tasks"]
        timeout = timedelta(seconds=self.heartbeat_timeout_seconds)
        now = utc_now()
        count = 0

        for task in tasks:
            if task["status"] != TaskStatus.IN_PROGRESS:
                continue

            heartbeat_raw = task.get("heartbeat_at")
            if heartbeat_raw is None:
                is_stale = True
            else:
                hb_dt = datetime.fromisoformat(heartbeat_raw)
                if hb_dt.tzinfo is None:
                    hb_dt = hb_dt.replace(tzinfo=timezone.utc)
                is_stale = (now - hb_dt) > timeout

            if not is_stale:
                continue

            update_resp = self.network.request(
                self.board_url,
                A2ARequest(
                    intent="board.update_task",
                    payload={
                        "task_id": task["task_id"],
                        "to_status": TaskStatus.STALE.value,
                    },
                ).to_dict(),
            )
            if not update_resp["ok"]:
                raise RuntimeError(update_resp["error"])
            count += 1

        # ── Custom-profile working statuses ────────────────────────────────────
        for task in tasks:
            if task["status"] not in self._working_statuses:
                continue

            status_timeout_seconds = self._status_timeouts.get(
                task["status"], self.heartbeat_timeout_seconds
            )
            status_timeout = timedelta(seconds=status_timeout_seconds)

            heartbeat_raw = task.get("heartbeat_at")
            if heartbeat_raw is None:
                is_stale = True
            else:
                hb_dt = datetime.fromisoformat(heartbeat_raw)
                if hb_dt.tzinfo is None:
                    hb_dt = hb_dt.replace(tzinfo=timezone.utc)
                is_stale = (now - hb_dt) > status_timeout

            if not is_stale:
                continue

            update_resp = self.network.request(
                self.board_url,
                A2ARequest(
                    intent="board.update_task",
                    payload={
                        "task_id": task["task_id"],
                        "to_status": "HUMAN_REVIEW",
                        "notes_append": (
                            f"Ombudsman: stale heartbeat in status {task['status']!r} "
                            f"(timeout={status_timeout_seconds}s)"
                        ),
                    },
                ).to_dict(),
            )
            if not update_resp["ok"]:
                raise RuntimeError(update_resp["error"])
            count += 1

        return count
