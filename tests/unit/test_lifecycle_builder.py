from __future__ import annotations

from quadro.board.state_machine import Lifecycle, LifecycleBuilder, lifecycle


def test_builder_phase_preserves_declaration_order() -> None:
    lc = LifecycleBuilder().phase("A", "B").phase("B", "C").phase("C", "D").build()
    assert lc.col_order == ("A", "B", "C", "D")


def test_builder_branch_appends_to_col_order() -> None:
    lc = (
        LifecycleBuilder()
        .phase("UNASSIGNED", "validating")
        .phase("validating", "validated")
        .branch("validating", "validation_failed")
        .phase("validated", "done")
        .build()
    )
    assert lc.col_order == (
        "UNASSIGNED",
        "validating",
        "validated",
        "validation_failed",
        "done",
    )


def test_builder_revision_does_not_change_col_order() -> None:
    builder = (
        LifecycleBuilder()
        .phase("UNASSIGNED", "ideating")
        .phase("ideating", "idea_ready")
        .phase("idea_ready", "researching")
        .phase("researching", "research_ready")
        .phase("research_ready", "writing")
        .phase("writing", "draft_ready")
        .phase("draft_ready", "reviewing")
        .phase("reviewing", "published")
    )
    order_before = tuple(builder._col_order)
    builder.revision("reviewing", "idea_ready")
    lc = builder.build()

    assert lc.col_order == order_before
    assert ("reviewing", "idea_ready") in lc


def test_builder_loop_does_not_change_col_order() -> None:
    builder = (
        LifecycleBuilder()
        .phase("UNASSIGNED", "checking")
        .phase("checking", "procuring")
        .phase("procuring", "procured")
    )
    order_before = tuple(builder._col_order)
    builder.loop("procured", "checking")
    lc = builder.build()

    assert lc.col_order == order_before
    assert ("procured", "checking") in lc


def test_builder_ordering_system_shipped_is_last() -> None:
    lc = (
        LifecycleBuilder()
        .phase("UNASSIGNED", "validating")
        .phase("validating", "validated")
        .branch("validating", "validation_failed")
        .phase("validated", "checking_stock")
        .phase("checking_stock", "stock_confirmed")
        .branch("checking_stock", "needs_procurement")
        .phase("needs_procurement", "procuring")
        .phase("procuring", "procured")
        .loop("procured", "checking_stock")
        .phase("stock_confirmed", "shipping")
        .phase("shipping", "shipped")
        .build()
    )
    assert lc.col_order[-1] == "shipped"
    assert lc.col_order == (
        "UNASSIGNED",
        "validating",
        "validated",
        "validation_failed",
        "checking_stock",
        "stock_confirmed",
        "needs_procurement",
        "procuring",
        "procured",
        "shipping",
        "shipped",
    )


def test_lifecycle_list_preserves_order() -> None:
    lc = lifecycle(
        [
            ("A", "B"),
            ("B", "C"),
            ("C", "D"),
        ]
    )
    assert isinstance(lc, Lifecycle)
    assert lc.col_order == ("A", "B", "C", "D")


def test_lifecycle_set_still_works() -> None:
    lc = lifecycle(
        {
            ("A", "B"),
            ("B", "C"),
        }
    )
    assert isinstance(lc, Lifecycle)
    assert ("A", "B") in lc
    assert ("B", "C") in lc
    assert set(lc.col_order) == {"A", "B", "C"}
