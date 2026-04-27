"""Runtime plugin contracts and framework-specific runtime adapters."""

from .base import FrameworkRuntime, RuntimeContext, StageRunResult
from .maf_workflow import MafWorkflowRuntime
from .stage_spec import native_runtime_entrypoint
from .telemetry import SCHEMA_VERSION, build_runtime_event, emit_runtime_event

__all__ = [
    "FrameworkRuntime",
    "MafWorkflowRuntime",
    "RuntimeContext",
    "SCHEMA_VERSION",
    "StageRunResult",
    "build_runtime_event",
    "emit_runtime_event",
    "native_runtime_entrypoint",
]
