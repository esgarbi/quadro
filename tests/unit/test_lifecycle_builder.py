from __future__ import annotations

from quadro.board.state_machine import Lifecycle, LifecycleBuilder, lifecycle


def test_builder_step_preserves_declaration_order() -> None:
    lc = LifecycleBuilder().step("A", "B").step("B", "C").step("C", "D").build()
    assert lc.col_order == ("A", "B", "C", "D")


def test_builder_branch_appends_to_col_order() -> None:
    lc = (
        LifecycleBuilder()
        .step("UNASSIGNED", "validating")
        .step("validating", "validated")
        .branch("validating", "validation_failed")
        .step("validated", "done")
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
        .step("UNASSIGNED", "ideating")
        .step("ideating", "idea_ready")
        .step("idea_ready", "researching")
        .step("researching", "research_ready")
        .step("research_ready", "writing")
        .step("writing", "draft_ready")
        .step("draft_ready", "reviewing")
        .step("reviewing", "published")
    )
    order_before = tuple(builder._col_order)
    builder.revision("reviewing", "idea_ready")
    lc = builder.build()

    assert lc.col_order == order_before
    assert ("reviewing", "idea_ready") in lc


def test_builder_loop_does_not_change_col_order() -> None:
    builder = (
        LifecycleBuilder()
        .step("UNASSIGNED", "checking")
        .step("checking", "procuring")
        .step("procuring", "procured")
    )
    order_before = tuple(builder._col_order)
    builder.loop("procured", "checking")
    lc = builder.build()

    assert lc.col_order == order_before
    assert ("procured", "checking") in lc


def test_builder_ordering_system_shipped_is_last() -> None:
    lc = (
        LifecycleBuilder()
        .step("UNASSIGNED", "validating")
        .step("validating", "validated")
        .branch("validating", "validation_failed")
        .step("validated", "checking_stock")
        .step("checking_stock", "stock_confirmed")
        .branch("checking_stock", "needs_procurement")
        .step("needs_procurement", "procuring")
        .step("procuring", "procured")
        .loop("procured", "checking_stock")
        .step("stock_confirmed", "shipping")
        .step("shipping", "shipped")
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
