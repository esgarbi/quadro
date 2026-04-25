"""Test fixtures for Sponsor integration tests and replay-style debugging.

``AlwaysOnSponsor`` and ``AlwaysStopSponsor`` are trivial baselines.
``ScriptedSponsor`` is the primary tool for deterministic end-to-end tests and
is productionised (not private) because it is also useful for demos and
"replay" debugging where a known sequence of decisions is needed.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator

from .types import (
    Continue,
    Drain,
    Lease,
    LeaseDecision,
    Sponsor,
    SponsorContext,
    Stop,
    new_lease_id,
)


class AlwaysOnSponsor:
    """Continue with a fresh Lease on every consultation.

    The lease defaults to ``ticks=None``/``deadline=None`` which means this
    Sponsor never triggers renewal on its own — the runtime will only
    consult it again if a sibling in a composite exhausts an axis. For a
    standalone AlwaysOn that *does* re-consult, pass explicit ``ticks`` or
    ``deadline`` kwargs.

    Primarily used as a test baseline: it lets the runtime run freely and
    is tombstoned only by another condition (a goal predicate elsewhere,
    a max-cycles budget in a composite, etc.).
    """

    def __init__(
        self,
        name: str = "always_on",
        *,
        ticks: int | None = None,
        deadline_seconds: float | None = None,
        fail_open: bool = False,
    ) -> None:
        self.name = name
        self.fail_open = fail_open
        self._ticks = ticks
        self._deadline_seconds = deadline_seconds

    def propose_lease(
        self, ctx: SponsorContext, prior: Lease | None
    ) -> LeaseDecision:
        from datetime import timedelta

        deadline = (
            ctx.now + timedelta(seconds=self._deadline_seconds)
            if self._deadline_seconds is not None
            else None
        )
        lease = Lease(
            id=new_lease_id(),
            issued_at=ctx.now,
            ticks=self._ticks,
            deadline=deadline,
            source=self.name,
            reason="always_on",
            renewal_of=prior.id if prior is not None else None,
        )
        return Continue(lease=lease, reason="always_on")


class AlwaysStopSponsor:
    """Return ``Stop`` on every consultation. Useful for sanity-checking termination paths."""

    def __init__(
        self, name: str = "always_stop", *, reason: str = "always_stop"
    ) -> None:
        self.name = name
        self.fail_open = False
        self._reason = reason

    def propose_lease(
        self, ctx: SponsorContext, prior: Lease | None
    ) -> LeaseDecision:
        return Stop(reason=self._reason)


class ScriptedSponsor:
    """Return a pre-scripted sequence of decisions.

    On each consultation, the next scripted entry is returned. If the script
    is exhausted, the Sponsor returns the ``default`` (Stop by default). The
    ``Continue`` entries may omit the lease; in that case a fresh, unbounded
    lease is synthesised at consultation time so the caller does not have to
    construct leases with the right ``issued_at`` value.

    This Sponsor is safe to use in production for "replay" debugging where a
    captured sequence of real decisions is played back against a different
    run.

    Example::

        script = [
            Continue(lease=Lease(ticks=3)),
            Continue(lease=Lease(ticks=3)),
            Drain(deadline=None, reason="script_drain"),
            Stop(reason="script_end"),
        ]
        sponsor = ScriptedSponsor(script)
    """

    def __init__(
        self,
        script: Iterable[LeaseDecision],
        *,
        name: str = "scripted",
        default: LeaseDecision | None = None,
        fail_open: bool = False,
    ) -> None:
        self.name = name
        self.fail_open = fail_open
        self._script: list[LeaseDecision] = list(script)
        self._iter: Iterator[LeaseDecision] = iter(self._script)
        self._default: LeaseDecision = default or Stop(reason="script_exhausted")
        self._calls = 0

    @property
    def calls(self) -> int:
        """Number of ``propose_lease`` invocations so far."""
        return self._calls

    def propose_lease(
        self, ctx: SponsorContext, prior: Lease | None
    ) -> LeaseDecision:
        self._calls += 1
        try:
            decision = next(self._iter)
        except StopIteration:
            decision = self._default

        if isinstance(decision, Continue):
            lease = decision.lease
            patched = Lease(
                id=new_lease_id(),
                issued_at=ctx.now,
                ticks=lease.ticks,
                deadline=lease.deadline,
                worker_invocations=lease.worker_invocations,
                llm_tokens=lease.llm_tokens,
                board_events=lease.board_events,
                source=lease.source or self.name,
                reason=lease.reason or decision.reason,
                renewal_of=prior.id if prior is not None else None,
            )
            return Continue(lease=patched, reason=decision.reason)
        return decision


# Ensure the fixtures satisfy the Protocol at import-time.
_sponsor_proto: type[Sponsor] = Sponsor  # noqa: F841 — documented
