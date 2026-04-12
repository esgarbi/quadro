from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from quadro import ValidationError, load_lifecycle
from quadro.board.lifecycle_loader import load_lifecycle_string
from quadro.board.state_machine import LifecycleBuilder


def test_load_lifecycle_from_string() -> None:
    toml = """
name = "simple"
steps = [
    ["UNASSIGNED", "working"],
    ["working", "done"],
]
"""
    name, lc = load_lifecycle_string(toml)
    assert name == "simple"
    assert ("UNASSIGNED", "working") in lc.transitions
    assert ("working", "done") in lc.transitions
    assert ("working", "HUMAN_REVIEW") in lc.transitions


def test_load_lifecycle_all_transition_types() -> None:
    toml = """
name = "full"
steps = [
    ["UNASSIGNED", "validating"],
    ["validating", "validated"],
    ["validated", "shipping"],
    ["shipping", "shipped"],
]
branches = [
    ["validating", "validation_failed"],
]
revisions = [
    ["validated", "UNASSIGNED"],
]
loops = [
    ["shipping", "validated"],
]
"""
    name, lc = load_lifecycle_string(toml)
    assert name == "full"
    assert ("validating", "validation_failed") in lc.transitions
    assert ("validated", "UNASSIGNED") in lc.transitions
    assert ("shipping", "validated") in lc.transitions


def test_load_lifecycle_produces_same_result_as_builder() -> None:
    builder_lc = (
        LifecycleBuilder()
        .step("UNASSIGNED", "ideating")
        .step("ideating", "idea_ready")
        .step("idea_ready", "researching")
        .step("researching", "research_ready")
        .step("research_ready", "writing")
        .step("writing", "draft_ready")
        .step("draft_ready", "reviewing")
        .step("reviewing", "published")
        .revision("reviewing", "idea_ready")
        .build()
    )

    toml_path = (
        Path(__file__).resolve().parents[2]
        / "examples"
        / "microsoft_agent_framework"
        / "newsroom"
        / "article.lifecycle.toml"
    )
    _name, toml_lc = load_lifecycle(str(toml_path))

    assert toml_lc.transitions == builder_lc.transitions
    assert toml_lc.col_order == builder_lc.col_order


def test_load_lifecycle_missing_name_raises() -> None:
    toml = """
steps = [["UNASSIGNED", "working"]]
"""
    with pytest.raises(ValidationError, match="name"):
        load_lifecycle_string(toml)


def test_load_lifecycle_missing_steps_raises() -> None:
    toml = """
name = "empty"
"""
    with pytest.raises(ValidationError, match="steps"):
        load_lifecycle_string(toml)


def test_load_lifecycle_from_file() -> None:
    content = b"""
name = "from_file"
steps = [
    ["UNASSIGNED", "active"],
    ["active", "complete"],
]
"""
    with tempfile.NamedTemporaryFile(suffix=".lifecycle.toml", delete=False) as f:
        f.write(content)
        f.flush()
        name, lc = load_lifecycle(f.name)

    assert name == "from_file"
    assert ("UNASSIGNED", "active") in lc.transitions
    assert ("active", "complete") in lc.transitions
