"""Load lifecycle profiles from .lifecycle.toml files.

Uses stdlib tomllib (Python 3.11+) — zero external dependencies.
Produces the same Lifecycle object as LifecycleBuilder.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from ..errors import ValidationError
from .state_machine import Lifecycle, LifecycleBuilder


def _validate_transitions(key: str, entries: list) -> list[tuple[str, str]]:
    result: list[tuple[str, str]] = []
    for i, entry in enumerate(entries):
        if not isinstance(entry, list) or len(entry) != 2:
            raise ValidationError(
                f"{key}[{i}]: expected [from, to] pair, got {entry!r}"
            )
        frm, to = entry
        if not isinstance(frm, str) or not isinstance(to, str):
            raise ValidationError(
                f"{key}[{i}]: both values must be strings, got {entry!r}"
            )
        result.append((frm, to))
    return result


def _build_from_dict(data: dict) -> tuple[str, Lifecycle]:
    if "name" not in data or not isinstance(data["name"], str):
        raise ValidationError("Lifecycle TOML must have a 'name' string field")

    steps = data.get("steps", [])
    if not steps:
        raise ValidationError("Lifecycle TOML must have a non-empty 'steps' list")

    builder = LifecycleBuilder()

    for frm, to in _validate_transitions("steps", steps):
        builder.step(frm, to)
    for frm, to in _validate_transitions("branches", data.get("branches", [])):
        builder.branch(frm, to)
    for frm, to in _validate_transitions("revisions", data.get("revisions", [])):
        builder.revision(frm, to)
    for frm, to in _validate_transitions("loops", data.get("loops", [])):
        builder.loop(frm, to)

    return data["name"], builder.build()


def load_lifecycle(path: str | Path) -> tuple[str, Lifecycle]:
    """
    Load a lifecycle profile from a .lifecycle.toml file.

    Returns (name, Lifecycle) — the name can be used as the profile key
    in QuadroBoard's custom_profiles and profile_resolver.
    """
    path = Path(path)
    with open(path, "rb") as f:
        data = tomllib.load(f)
    return _build_from_dict(data)


def load_lifecycle_string(toml_string: str) -> tuple[str, Lifecycle]:
    """
    Load a lifecycle profile from a TOML string.

    Useful for tests, inline configuration, and dynamic generation.
    Returns (name, Lifecycle).
    """
    data = tomllib.loads(toml_string)
    return _build_from_dict(data)
