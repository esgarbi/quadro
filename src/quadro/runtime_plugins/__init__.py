"""Runtime plugin contracts and the saga runtime.

LLM-framework runtime adapters (``MafChiefRuntime``, ``LangChainChiefRuntime``,
and any user-written runtimes that implement :class:`FrameworkRuntime`) live
in sibling packages (``quadro_maf``, ``quadro_langchain``, etc.) so the
substrate never imports any LLM framework.
"""

from .base import FrameworkRuntime, RuntimeContext, StageRunResult
from .saga import QuadroSagaRuntime
from .stage_spec import native_runtime_entrypoint
from .telemetry import SCHEMA_VERSION, build_runtime_event, emit_runtime_event

__all__ = [
    "FrameworkRuntime",
    "QuadroSagaRuntime",
    "RuntimeContext",
    "SCHEMA_VERSION",
    "StageRunResult",
    "build_runtime_event",
    "emit_runtime_event",
    "native_runtime_entrypoint",
]
