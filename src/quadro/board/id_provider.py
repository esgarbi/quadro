from __future__ import annotations

import random
import string
from typing import Protocol, runtime_checkable

_BASE36_CHARS = string.digits + string.ascii_lowercase
_DEFAULT_LENGTH = 5


@runtime_checkable
class TaskIdProvider(Protocol):
    """
    Generates short, unique task IDs for the board.

    Task IDs are LLM-facing: the Chief must reproduce them accurately in tool
    calls. Smaller models (e.g. Ollama) struggle with long hex strings, so IDs
    should be short and distinctive. Implement this protocol to control the
    format — sequential, prefixed, domain-specific, etc.
    """

    def generate(self, existing_ids: set[str]) -> str: ...


class DefaultTaskIdProvider:
    """
    Produces 5-character base36 IDs (digits + lowercase letters).

    36^5 = ~60M possibilities — 60x the namespace of hex[:5] at the same
    length. Each generated ID is checked against ``existing_ids`` to prevent
    collisions within the board.
    """

    def __init__(self, length: int = _DEFAULT_LENGTH) -> None:
        self._length = length

    def generate(self, existing_ids: set[str]) -> str:
        for _ in range(100):
            candidate = "".join(
                random.choices(_BASE36_CHARS, k=self._length)  # noqa: S311
            )
            if candidate not in existing_ids:
                return candidate
        raise RuntimeError(
            f"Failed to generate unique task ID after 100 attempts "
            f"(length={self._length}, existing={len(existing_ids)})"
        )
