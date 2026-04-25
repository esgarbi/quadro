"""Composers — combine Sponsors with ``AllOf`` / ``AnyOf`` / ``Priority``.

Each composer is itself a Sponsor and can be nested arbitrarily. The
decision logic follows the truth tables in ``docs/design/sponsor.md``:

- ``AllOf(*sponsors)``: every child must return a non-``Stop`` decision.
  The effective lease is the axis-wise **minimum** of the children's leases.
  Any ``Stop`` short-circuits. ``Drain`` + ``Continue`` folds to ``Drain``.
- ``AnyOf(*sponsors)``: at least one child must return ``Continue``. The
  effective lease is the axis-wise **maximum** of the continuing children's
  leases. A single ``Continue`` outranks siblings' ``Drain`` or ``Stop``.
- ``Priority(*sponsors)``: first non-``Stop`` wins outright.

All composers preserve the decision semantics of drain deadlines: when the
aggregate decision is ``Drain``, the deadline is the nearest non-``None``
child deadline (``min``), or ``None`` if all contributing children specified
no deadline.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from typing import Iterable

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


def _min_opt(a: int | None, b: int | None) -> int | None:
    """Min of two optional ints where None means 'unbounded' (no constraint)."""
    if a is None:
        return b
    if b is None:
        return a
    return min(a, b)


def _max_opt(a: int | None, b: int | None) -> int | None:
    """Max of two optional ints where None means 'unbounded' (dominates)."""
    if a is None or b is None:
        return None
    return max(a, b)


def _min_deadline(a: datetime | None, b: datetime | None) -> datetime | None:
    if a is None:
        return b
    if b is None:
        return a
    return min(a, b)


def _max_deadline(a: datetime | None, b: datetime | None) -> datetime | None:
    if a is None or b is None:
        return None
    return max(a, b)


def _intersect_lease(a: Lease, b: Lease, source: str) -> Lease:
    """Axis-wise minimum intersection; used by AllOf."""
    return Lease(
        id=new_lease_id(),
        issued_at=max(a.issued_at, b.issued_at),
        ticks=_min_opt(a.ticks, b.ticks),
        deadline=_min_deadline(a.deadline, b.deadline),
        worker_invocations=_min_opt(a.worker_invocations, b.worker_invocations),
        llm_tokens=_min_opt(a.llm_tokens, b.llm_tokens),
        board_events=_min_opt(a.board_events, b.board_events),
        source=source,
        reason=" & ".join(r for r in (a.reason, b.reason) if r),
    )


def _union_lease(a: Lease, b: Lease, source: str) -> Lease:
    """Axis-wise maximum union; used by AnyOf. None 'wins' (dominates)."""
    return Lease(
        id=new_lease_id(),
        issued_at=max(a.issued_at, b.issued_at),
        ticks=_max_opt(a.ticks, b.ticks),
        deadline=_max_deadline(a.deadline, b.deadline),
        worker_invocations=_max_opt(a.worker_invocations, b.worker_invocations),
        llm_tokens=_max_opt(a.llm_tokens, b.llm_tokens),
        board_events=_max_opt(a.board_events, b.board_events),
        source=source,
        reason=" | ".join(r for r in (a.reason, b.reason) if r),
    )


def _patch_renewal(lease: Lease, prior: Lease | None) -> Lease:
    if prior is None or lease.renewal_of == prior.id:
        return lease
    return replace(lease, renewal_of=prior.id)


def _safe_propose(
    sponsor: Sponsor, ctx: SponsorContext, prior: Lease | None
) -> LeaseDecision:
    """Invoke a child Sponsor honouring its fail_open preference.

    Child exceptions are absorbed here so a composite's aggregate decision
    reflects the child's configured error behaviour. The outer runtime still
    wraps the composite's own call, so any exception leaking past this helper
    is treated according to the *composite's* fail_open setting.
    """
    try:
        return sponsor.propose_lease(ctx, prior)
    except Exception as exc:  # noqa: BLE001
        if getattr(sponsor, "fail_open", False) and prior is not None:
            return Continue(
                lease=replace(prior, reason=f"child_fail_open:{exc}"),
                reason=f"child_fail_open:{exc}",
            )
        return Stop(reason=f"child_sponsor_error:{exc}")


# ═══════════════════════════════════════════════════════════════════════════════
#  AllOf
# ═══════════════════════════════════════════════════════════════════════════════


class AllOf:
    """Every child must agree to continue. Any Stop halts; Drain dominates Continue.

    Effective lease = axis-wise minimum of the children's leases.
    """

    def __init__(
        self, *sponsors: Sponsor, name: str = "all_of", fail_open: bool = False
    ) -> None:
        if not sponsors:
            raise ValueError("AllOf requires at least one child Sponsor")
        self.name = name
        self.fail_open = fail_open
        self._sponsors = tuple(sponsors)

    @property
    def children(self) -> tuple[Sponsor, ...]:
        return self._sponsors

    def propose_lease(
        self, ctx: SponsorContext, prior: Lease | None
    ) -> LeaseDecision:
        decisions = [_safe_propose(s, ctx, prior) for s in self._sponsors]

        # Stop short-circuit: any Stop turns the whole AllOf into Stop.
        for d in decisions:
            if isinstance(d, Stop):
                return Stop(reason=f"{self.name}:{d.reason}")

        # Collect Drain deadlines; if any Drain exists among non-Stop, result is Drain.
        drains = [d for d in decisions if isinstance(d, Drain)]
        continues = [d for d in decisions if isinstance(d, Continue)]

        if drains:
            deadline: datetime | None = None
            reasons = []
            for d in drains:
                deadline = _min_deadline(deadline, d.deadline)
                if d.reason:
                    reasons.append(d.reason)
            return Drain(
                deadline=deadline,
                reason=f"{self.name}:" + (" & ".join(reasons) if reasons else "drain"),
            )

        # All Continue — intersect leases.
        if not continues:
            # Shouldn't happen given the checks above, but guard anyway.
            return Stop(reason=f"{self.name}:empty")

        lease = continues[0].lease
        for c in continues[1:]:
            lease = _intersect_lease(lease, c.lease, source=self.name)
        lease = _patch_renewal(
            replace(lease, source=self.name, renewal_of=prior.id if prior else None),
            prior,
        )
        reason = f"{self.name}:" + " & ".join(
            c.reason or c.lease.reason for c in continues if c.reason or c.lease.reason
        )
        return Continue(lease=lease, reason=reason or self.name)


# ═══════════════════════════════════════════════════════════════════════════════
#  AnyOf
# ═══════════════════════════════════════════════════════════════════════════════


class AnyOf:
    """Any child may authorise continuation. Continue > Drain > Stop.

    Effective lease = axis-wise maximum of continuing children's leases.
    """

    def __init__(
        self, *sponsors: Sponsor, name: str = "any_of", fail_open: bool = False
    ) -> None:
        if not sponsors:
            raise ValueError("AnyOf requires at least one child Sponsor")
        self.name = name
        self.fail_open = fail_open
        self._sponsors = tuple(sponsors)

    @property
    def children(self) -> tuple[Sponsor, ...]:
        return self._sponsors

    def propose_lease(
        self, ctx: SponsorContext, prior: Lease | None
    ) -> LeaseDecision:
        decisions = [_safe_propose(s, ctx, prior) for s in self._sponsors]
        continues = [d for d in decisions if isinstance(d, Continue)]
        drains = [d for d in decisions if isinstance(d, Drain)]

        if continues:
            lease = continues[0].lease
            for c in continues[1:]:
                lease = _union_lease(lease, c.lease, source=self.name)
            lease = _patch_renewal(
                replace(
                    lease, source=self.name, renewal_of=prior.id if prior else None
                ),
                prior,
            )
            reason = f"{self.name}:" + " | ".join(
                c.reason or c.lease.reason for c in continues if c.reason or c.lease.reason
            )
            return Continue(lease=lease, reason=reason or self.name)

        if drains:
            # For AnyOf we pick the *latest* drain deadline — let the
            # most patient Sponsor set the tempo. Any child asking for
            # "no deadline" (None) implies unbounded patience and wins.
            deadline: datetime | None
            if any(d.deadline is None for d in drains):
                deadline = None
            else:
                deadline = max(
                    d.deadline for d in drains if d.deadline is not None
                )
            reasons = [d.reason for d in drains if d.reason]
            return Drain(
                deadline=deadline,
                reason=f"{self.name}:" + (" | ".join(reasons) if reasons else "drain"),
            )

        # All Stop.
        stop_reasons = [
            d.reason
            for d in decisions
            if isinstance(d, Stop) and d.reason
        ]
        return Stop(
            reason=f"{self.name}:"
            + (" | ".join(stop_reasons) if stop_reasons else "all_stop")
        )


# ═══════════════════════════════════════════════════════════════════════════════
#  Priority
# ═══════════════════════════════════════════════════════════════════════════════


class Priority:
    """Cascade sponsors by priority. First non-Stop wins outright.

    The first sponsor that returns ``Continue`` or ``Drain`` determines the
    result; later sponsors are not consulted. If every sponsor returns
    ``Stop``, the composite returns ``Stop``.

    Use Priority when one authority takes precedence over a fallback (e.g.
    "if the CRM ticket says stop, stop; otherwise fall back to a local
    deadline").
    """

    def __init__(
        self, *sponsors: Sponsor, name: str = "priority", fail_open: bool = False
    ) -> None:
        if not sponsors:
            raise ValueError("Priority requires at least one child Sponsor")
        self.name = name
        self.fail_open = fail_open
        self._sponsors = tuple(sponsors)

    @property
    def children(self) -> tuple[Sponsor, ...]:
        return self._sponsors

    def propose_lease(
        self, ctx: SponsorContext, prior: Lease | None
    ) -> LeaseDecision:
        last_stop_reason = ""
        for sponsor in self._sponsors:
            decision = _safe_propose(sponsor, ctx, prior)
            if isinstance(decision, Continue):
                lease = _patch_renewal(
                    replace(
                        decision.lease,
                        source=self.name,
                        renewal_of=prior.id if prior else None,
                    ),
                    prior,
                )
                return Continue(
                    lease=lease, reason=f"{self.name}:{decision.reason or lease.reason}"
                )
            if isinstance(decision, Drain):
                return Drain(
                    deadline=decision.deadline,
                    reason=f"{self.name}:{decision.reason or 'drain'}",
                )
            last_stop_reason = decision.reason
        return Stop(reason=f"{self.name}:{last_stop_reason or 'all_stop'}")
