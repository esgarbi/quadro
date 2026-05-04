from __future__ import annotations

import warnings
from dataclasses import dataclass

from ..errors import TransitionError
from .records import TaskStatus

__all__ = ["TransitionError"]


REVIEW_REQUIRED_TRANSITIONS: set[tuple[TaskStatus, TaskStatus]] = {
    (TaskStatus.UNASSIGNED, TaskStatus.IN_PROGRESS),
    (TaskStatus.IN_PROGRESS, TaskStatus.PENDING_REVIEW),
    (TaskStatus.PENDING_REVIEW, TaskStatus.IN_PROGRESS),
    (TaskStatus.IN_PROGRESS, TaskStatus.APPROVED),
    (TaskStatus.IN_PROGRESS, TaskStatus.REVISION_NEEDED),
    (TaskStatus.APPROVED, TaskStatus.COMPLETE),
    (TaskStatus.REVISION_NEEDED, TaskStatus.IN_PROGRESS),
    (TaskStatus.IN_PROGRESS, TaskStatus.STALE),
    (TaskStatus.STALE, TaskStatus.UNASSIGNED),
}

FAST_TRANSITIONS: set[tuple[TaskStatus, TaskStatus]] = {
    (TaskStatus.UNASSIGNED, TaskStatus.IN_PROGRESS),
    (TaskStatus.IN_PROGRESS, TaskStatus.COMPLETE),
    (TaskStatus.IN_PROGRESS, TaskStatus.STALE),
    (TaskStatus.STALE, TaskStatus.UNASSIGNED),
}


def _expand_with_global(
    transitions: set[tuple[TaskStatus, TaskStatus]],
) -> set[tuple[TaskStatus, TaskStatus]]:
    expanded = set(transitions)
    all_states = list(TaskStatus)
    for state in all_states:
        if state not in {TaskStatus.HUMAN_REVIEW, TaskStatus.ON_HOLD}:
            expanded.add((state, TaskStatus.HUMAN_REVIEW))
            expanded.add((state, TaskStatus.ON_HOLD))
    return expanded


PROFILE_TRANSITIONS: dict[str, set[tuple[TaskStatus, TaskStatus]]] = {
    "review_required": _expand_with_global(REVIEW_REQUIRED_TRANSITIONS),
    "fast": _expand_with_global(FAST_TRANSITIONS),
}

# Standard-profile terminal states — statuses that release an agent to IDLE.
# Derived from PROFILE_TRANSITIONS: states with no further outgoing transitions
# (other than the globally-expanded HUMAN_REVIEW / ON_HOLD exits).
STANDARD_TERMINAL_STATUSES: frozenset[TaskStatus] = frozenset(
    {
        TaskStatus.COMPLETE,
        TaskStatus.HUMAN_REVIEW,
        TaskStatus.ON_HOLD,
    }
)


def compute_custom_terminal_statuses(
    transitions: set[tuple[str, str]],
) -> frozenset[str]:
    """
    Derive the terminal statuses from a custom profile's transition set.

    A terminal status is any state that appears only as a destination — it
    never appears as the source of a transition in the *original* set
    (before FAILED / ON_HOLD auto-expansion). Agents assigned to tasks in
    a terminal status are released to IDLE automatically.

    Example — newsroom profile:
        from_states = {UNASSIGNED, ideating, idea_ready, researching,
                       research_ready, writing, draft_ready, reviewing}
        all_states  = from_states ∪ {published}
        terminals   = {"published"}

    Example — ordering profile:
        terminals   = {"delivered", "cancelled"}

    FAILED and ON_HOLD are excluded because they are auto-expanded for every
    state by build_custom_profile and handled separately by the board.
    """
    _always_excluded = {"HUMAN_REVIEW", "ON_HOLD"}
    from_states = {s for s, d in transitions if d not in _always_excluded}
    all_states = {s for pair in transitions for s in pair}
    return frozenset(
        s for s in all_states if s not in from_states and s not in _always_excluded
    )


@dataclass(frozen=True)
class Lifecycle:
    """Richer return type from lifecycle() — carries transitions and derived column order."""

    transitions: frozenset[tuple[str, str]]
    col_order: tuple[str, ...]

    def __iter__(self):
        return iter(self.transitions)

    def __contains__(self, item):
        return item in self.transitions


def _derive_col_order(transitions: set[tuple[str, str]]) -> tuple[str, ...]:
    """
    Derive a stable column order from the transition graph via DFS.

    Starts from the source node (the state that never appears as a
    destination in non-back-edge transitions — typically UNASSIGNED).
    Back-edges (revision paths) are handled by skipping already-visited nodes.
    Terminal states naturally appear at the end because they have no outgoing
    edges to further DFS traversal.
    """
    graph: dict[str, list[str]] = {}
    all_states: set[str] = set()
    for frm, to in transitions:
        graph.setdefault(frm, []).append(to)
        all_states.add(frm)
        all_states.add(to)

    destinations = {to for _, to in transitions}
    sources = all_states - destinations
    start = next(iter(sources)) if sources else next(iter(all_states))

    order: list[str] = []
    visited: set[str] = set()
    stack = [start]
    while stack:
        node = stack.pop()
        if node in visited:
            continue
        visited.add(node)
        order.append(node)
        for neighbour in reversed(graph.get(node, [])):
            if neighbour not in visited:
                stack.append(neighbour)

    for state in all_states:
        if state not in visited:
            order.append(state)

    return tuple(order)


def _expand_custom(transitions: set[tuple[str, str]]) -> set[tuple[str, str]]:
    """Expand a custom transition set with HUMAN_REVIEW and ON_HOLD exits from every state."""
    expanded = set(transitions)
    all_states = {s for pair in transitions for s in pair}
    for state in all_states:
        if state not in {"HUMAN_REVIEW", "ON_HOLD"}:
            expanded.add((state, "HUMAN_REVIEW"))
            expanded.add((state, "ON_HOLD"))
    return expanded


def lifecycle(
    transitions: list[tuple[str, str]] | set[tuple[str, str]],
) -> Lifecycle:
    """
    Declare the lifecycle of a task type.

    For simple linear pipelines, pass a list of tuples to preserve order:
        SIMPLE = lifecycle([
            ("UNASSIGNED", "working"),
            ("working",    "done"),
        ])

    For complex pipelines with branches and loops, use LifecycleBuilder instead:
        COMPLEX = (
            LifecycleBuilder()
            .phase("UNASSIGNED", "working")
            .branch("working",  "failed")
            .phase("working",    "done")
            .build()
        )

    When a set is passed, col_order is derived via DFS (legacy behaviour).
    When a list is passed, col_order preserves declaration order.
    """
    if isinstance(transitions, list):
        col_order = _derive_col_order_from_list(transitions)
    else:
        col_order = _derive_col_order(transitions)
    expanded = _expand_custom(set(transitions))
    return Lifecycle(transitions=frozenset(expanded), col_order=col_order)


def _derive_col_order_from_list(
    transitions: list[tuple[str, str]],
) -> tuple[str, ...]:
    """Derive col_order from an ordered list by first-seen state insertion."""
    seen: set[str] = set()
    order: list[str] = []
    for frm, to in transitions:
        for state in (frm, to):
            if state not in seen:
                seen.add(state)
                order.append(state)
    return tuple(order)


def build_custom_profile(
    transitions: set[tuple[str, str]],
) -> set[tuple[str, str]]:
    """
    Build a custom profile transition set from a set of (from, to) string pairs.

    .. deprecated::
        Use :func:`lifecycle` instead, which returns a richer ``Lifecycle``
        object with a derived column order for the Board UI.
    """
    warnings.warn(
        "build_custom_profile() is deprecated — use lifecycle() instead",
        DeprecationWarning,
        stacklevel=2,
    )
    return _expand_custom(transitions)


class LifecycleBuilder:
    """
    Fluent builder for declaring a task lifecycle.

    Collects transitions in declaration order and builds a Lifecycle whose
    col_order reflects exactly the sequence the developer wrote — no graph
    traversal, no hash-ordering surprises.

    Usage:
        ARTICLE_LIFECYCLE = (
            LifecycleBuilder()
            .phase("UNASSIGNED",     "ideating")
            .phase("ideating",       "idea_ready")
            ...
            .phase("reviewing",      "published")
            .revision("reviewing",  "idea_ready")
            .build()
        )
    """

    def __init__(self) -> None:
        self._transitions: list[tuple[str, str]] = []
        self._col_order: list[str] = []
        self._col_order_set: set[str] = set()

    def _add_to_col_order(self, *states: str) -> None:
        for state in states:
            if state not in self._col_order_set:
                self._col_order.append(state)
                self._col_order_set.add(state)

    def phase(self, from_status: str, to_status: str) -> LifecycleBuilder:
        """
        Main progression — declares a transition between two phases of a
        task's lifecycle. Both phase names are added to the column order
        in the sequence declared, so the Board UI reflects the happy path.

        The method declares an edge between two phases rather than a phase
        itself; the phase names are introduced implicitly the first time they
        appear in either position. This matches how the previous ``.step()``
        method behaved — only the name has changed.
        """
        self._transitions.append((from_status, to_status))
        self._add_to_col_order(from_status, to_status)
        return self

    def branch(self, from_status: str, to_status: str) -> LifecycleBuilder:
        """
        Alternative exit from a state (e.g. validation_failed alongside validated).
        The destination is added to col_order after the current position if not
        already present. Use when the alternative path leads to a new state.
        """
        self._transitions.append((from_status, to_status))
        self._add_to_col_order(to_status)
        return self

    def revision(self, from_status: str, to_status: str) -> LifecycleBuilder:
        """
        Back-edge for revision paths (e.g. reviewing -> idea_ready).
        The transition is recorded for validation but col_order is unchanged —
        the destination state was already declared earlier in the pipeline.
        """
        self._transitions.append((from_status, to_status))
        return self

    def loop(self, from_status: str, to_status: str) -> LifecycleBuilder:
        """
        Self-healing loop back to an earlier stage (e.g. procured -> checking_stock).
        The transition is recorded for validation but col_order is unchanged —
        the destination state was already declared earlier in the pipeline.
        """
        self._transitions.append((from_status, to_status))
        return self

    def build(self) -> Lifecycle:
        """
        Build the Lifecycle. Expands transitions with HUMAN_REVIEW and ON_HOLD
        exits from every declared state.
        """
        raw = set(self._transitions)
        expanded = _expand_custom(raw)
        return Lifecycle(
            transitions=frozenset(expanded),
            col_order=tuple(self._col_order),
        )


def validate_transition(
    profile: str,
    from_status: TaskStatus | str,
    to_status: TaskStatus | str,
    custom_profiles: dict[str, set[tuple[str, str]]] | None = None,
) -> None:
    allowed = PROFILE_TRANSITIONS.get(profile)
    if allowed is not None:
        if (from_status, to_status) not in allowed:
            raise TransitionError(
                f"Illegal transition for {profile}: {from_status} -> {to_status}"
            )
        return

    if custom_profiles:
        custom_allowed = custom_profiles.get(profile)
        if custom_allowed is not None:
            from_val = str(from_status)
            to_val = str(to_status)
            if (from_val, to_val) not in custom_allowed:
                raise TransitionError(
                    f"Illegal transition for {profile}: {from_val} -> {to_val}"
                )
            return

    raise TransitionError(f"Unknown task profile: {profile}")
