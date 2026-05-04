"""A tiny in-memory CRM that mimics a ticket-tracking system.

Tickets drive the :class:`~quadro.sponsor.HttpSponsor` or ``CallableSponsor``
in the demo. The CRM has three observable fields the Sponsor cares about:

- ``is_open``    — ticket is active; runtime may keep working.
- ``in_review``  — stakeholder is reviewing; runtime should drain.
- ``closed``     — ticket is closed; runtime should stop.

For the demo we switch states on a timer so the run produces a readable
narrative: work for a bit, drain when "review" lands, close when review is
complete.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Literal

Status = Literal["open", "in_review", "closed"]


@dataclass
class TicketState:
    ticket_id: str
    status: Status
    reason: str = ""


class Crm:
    """Trivial in-memory CRM that evolves a single ticket over time."""

    def __init__(self, ticket_id: str = "TCKT-0001") -> None:
        self._lock = threading.Lock()
        self._ticket = TicketState(ticket_id=ticket_id, status="open")

    @property
    def ticket(self) -> TicketState:
        with self._lock:
            return TicketState(
                ticket_id=self._ticket.ticket_id,
                status=self._ticket.status,
                reason=self._ticket.reason,
            )

    def set_status(self, status: Status, reason: str = "") -> None:
        with self._lock:
            self._ticket = TicketState(
                ticket_id=self._ticket.ticket_id,
                status=status,
                reason=reason,
            )

    def schedule(self, schedule: list[tuple[float, Status, str]]) -> None:
        """Evolve the ticket on a background timer.

        Each entry is ``(delay_seconds_from_start, new_status, reason)``.
        """

        def _run() -> None:
            start = time.monotonic()
            for delay, status, reason in schedule:
                now = time.monotonic()
                to_sleep = (start + delay) - now
                if to_sleep > 0:
                    time.sleep(to_sleep)
                self.set_status(status, reason)

        t = threading.Thread(target=_run, daemon=True, name="crm-schedule")
        t.start()
