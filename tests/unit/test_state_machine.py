import pytest

from quadro.board.records import TaskStatus
from quadro.board.state_machine import (
    TransitionError,
    lifecycle,
    validate_transition,
)


def test_review_required_valid_transitions() -> None:
    validate_transition(
        "review_required", TaskStatus.UNASSIGNED, TaskStatus.IN_PROGRESS
    )
    validate_transition(
        "review_required", TaskStatus.IN_PROGRESS, TaskStatus.PENDING_REVIEW
    )
    validate_transition(
        "review_required", TaskStatus.PENDING_REVIEW, TaskStatus.IN_PROGRESS
    )
    validate_transition("review_required", TaskStatus.IN_PROGRESS, TaskStatus.APPROVED)
    validate_transition("review_required", TaskStatus.APPROVED, TaskStatus.COMPLETE)


def test_fast_valid_transitions() -> None:
    validate_transition("fast", TaskStatus.UNASSIGNED, TaskStatus.IN_PROGRESS)
    validate_transition("fast", TaskStatus.IN_PROGRESS, TaskStatus.COMPLETE)


def test_illegal_transition_rejected() -> None:
    with pytest.raises(TransitionError):
        validate_transition(
            "review_required", TaskStatus.IN_PROGRESS, TaskStatus.COMPLETE
        )


def test_custom_profile_validates_correctly() -> None:
    order_profile = lifecycle(
        [
            ("placed", "accepted"),
            ("accepted", "awaiting_stock"),
            ("awaiting_stock", "stock_ready"),
        ]
    )
    custom_profiles = {"order": order_profile}

    validate_transition("order", "placed", "accepted", custom_profiles=custom_profiles)
    validate_transition(
        "order", "accepted", "awaiting_stock", custom_profiles=custom_profiles
    )
    validate_transition(
        "order", "awaiting_stock", "stock_ready", custom_profiles=custom_profiles
    )

    # Global expansions added by build_custom_profile
    validate_transition(
        "order", "placed", "HUMAN_REVIEW", custom_profiles=custom_profiles
    )
    validate_transition("order", "accepted", "ON_HOLD", custom_profiles=custom_profiles)

    with pytest.raises(TransitionError, match="order"):
        validate_transition(
            "order", "placed", "stock_ready", custom_profiles=custom_profiles
        )

    with pytest.raises(TransitionError, match="placed -> stock_ready"):
        validate_transition(
            "order", "placed", "stock_ready", custom_profiles=custom_profiles
        )

    with pytest.raises(TransitionError):
        validate_transition("order", "placed", "accepted")  # no custom_profiles passed
