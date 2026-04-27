"""
Framework runtime plugin contracts for Quadro.

Defines the framework-agnostic execution boundary used by Pipeline to
delegate framework-specific behavior (chief turns, stage execution, tool
decoration) while keeping governance in Quadro core.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from ..pipeline import StageSpec, ToolDescriptor


@dataclass
class StageRunResult:
    """Normalized result returned by a framework runtime after one stage turn."""

    output: Any
    status: str | None = None
    notes_append: str | None = None
    update_fields: dict[str, Any] | None = None
    token_total: int = 0
    telemetry: list[dict[str, Any]] | None = None
    checkpoint_id: str | None = None
    resume_id: str | None = None
    terminal_reason: str | None = None


@dataclass
class RuntimeContext:
    """Execution context passed from Pipeline to a framework runtime."""

    stage: StageSpec
    task: dict[str, Any]
    context: dict[str, Any]
    board_fn: Callable[[str, dict], dict]
    token_reporter: Callable[[int], None] | None = None
    telemetry_sink: Callable[[dict[str, Any]], None] | None = None


class FrameworkRuntime(Protocol):
    """Framework execution plugin boundary for Quadro control-plane orchestration."""

    runtime_id: str

    def can_handle(self, spec: StageSpec) -> bool:
        """Return True if this runtime can execute the given stage."""
        ...

    def decorate_tools(self, descriptors: list[ToolDescriptor]) -> list:
        """Convert framework-agnostic chief tools into framework-native tool objects."""
        ...

    async def run_chief_turn(
        self,
        board_summary: str,
        instructions: str,
        tools: list,
        *,
        chief_name_prefix: str,
    ) -> str | None:
        """Run one chief turn using this framework runtime."""
        ...

    async def run_stage(self, ctx: RuntimeContext) -> StageRunResult:
        """Run one stage invocation and return a normalized stage result."""
        ...
