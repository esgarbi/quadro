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

    def _fetch_tasks(self, statuses: set[str]) -> list[dict]:
        """Fetch only tasks in the given statuses via filtered query."""
        resp = self.network.request(
            self.board_url,
            A2ARequest(
                intent="board.list_tasks_by_status",
                payload={"statuses": sorted(statuses)},
            ).to_dict(),
        )
        if not resp["ok"]:
            raise RuntimeError(resp["error"])
        return resp["result"]["tasks"]

    def _is_stale(self, task: dict, timeout: timedelta, now: datetime) -> bool:
        heartbeat_raw = task.get("heartbeat_at")
        if heartbeat_raw is None:
            return True
        hb_dt = datetime.fromisoformat(heartbeat_raw)
        if hb_dt.tzinfo is None:
            hb_dt = hb_dt.replace(tzinfo=timezone.utc)
        return (now - hb_dt) > timeout

    def nudge(self) -> int:
        """
        Scan IN_PROGRESS tasks (and custom working statuses) for stale
        heartbeats.  Returns the count of tasks transitioned.

        Uses board.list_tasks_by_status to avoid deserializing the entire
        task table — only the statuses that matter are fetched.
        """
        now = utc_now()
        count = 0

        # ── Standard IN_PROGRESS scan ──────────────────────────────────────────
        timeout = timedelta(seconds=self.heartbeat_timeout_seconds)
        for task in self._fetch_tasks({TaskStatus.IN_PROGRESS.value}):
            if not self._is_stale(task, timeout, now):
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
        if self._working_statuses:
            for task in self._fetch_tasks(set(self._working_statuses)):
                status_timeout_seconds = self._status_timeouts.get(
                    task["status"], self.heartbeat_timeout_seconds
                )
                status_timeout = timedelta(seconds=status_timeout_seconds)
                if not self._is_stale(task, status_timeout, now):
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
