from __future__ import annotations

import warnings


from quadro.board.state_machine import (
    Lifecycle,
    build_custom_profile,
    compute_custom_terminal_statuses,
    lifecycle,
)


def test_lifecycle_derives_correct_col_order_linear() -> None:
    lc = lifecycle(
        {
            ("A", "B"),
            ("B", "C"),
            ("C", "D"),
        }
    )
    assert lc.col_order == ("A", "B", "C", "D")


def test_lifecycle_handles_revision_back_edge() -> None:
    """Newsroom-style: main path + reviewing→idea_ready back-edge."""
    lc = lifecycle(
        {
            ("UNASSIGNED", "ideating"),
            ("ideating", "idea_ready"),
            ("idea_ready", "researching"),
            ("researching", "research_ready"),
            ("research_ready", "writing"),
            ("writing", "draft_ready"),
            ("draft_ready", "reviewing"),
            ("reviewing", "published"),
            ("reviewing", "idea_ready"),
        }
    )
    assert lc.col_order == (
        "UNASSIGNED",
        "ideating",
        "idea_ready",
        "researching",
        "research_ready",
        "writing",
        "draft_ready",
        "reviewing",
        "published",
    )


def test_lifecycle_expands_with_failed_and_on_hold() -> None:
    lc = lifecycle(
        {
            ("A", "B"),
            ("B", "C"),
        }
    )
    assert ("A", "HUMAN_REVIEW") in lc
    assert ("B", "HUMAN_REVIEW") in lc
    assert ("C", "HUMAN_REVIEW") in lc
    assert ("A", "ON_HOLD") in lc
    assert ("B", "ON_HOLD") in lc
    assert ("C", "ON_HOLD") in lc


def test_lifecycle_terminal_statuses_derived_correctly() -> None:
    lc = lifecycle(
        {
            ("UNASSIGNED", "ideating"),
            ("ideating", "idea_ready"),
            ("idea_ready", "researching"),
            ("researching", "research_ready"),
            ("research_ready", "writing"),
            ("writing", "draft_ready"),
            ("draft_ready", "reviewing"),
            ("reviewing", "published"),
            ("reviewing", "idea_ready"),
        }
    )
    terminals = compute_custom_terminal_statuses(lc.transitions)
    assert "published" in terminals
    assert "UNASSIGNED" not in terminals
    assert "ideating" not in terminals


def test_build_custom_profile_still_works_with_deprecation_warning() -> None:
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        result = build_custom_profile({("A", "B"), ("B", "C")})

    assert isinstance(result, set)
    assert not isinstance(result, Lifecycle)
    assert ("A", "B") in result
    assert ("A", "HUMAN_REVIEW") in result
    assert any(issubclass(w.category, DeprecationWarning) for w in caught)
