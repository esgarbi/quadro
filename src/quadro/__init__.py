"""Quadro public API."""

from .a2a.dispatch import LocalA2ANetwork
from .agents.chief import ChiefAgent
from .agents.pool import WorkerPool
from .agents.worker import WorkerAgent
from .board.board import QuadroBoard
from .board.client import BoardClient
from .board.id_provider import DefaultTaskIdProvider
from .board.lifecycle_loader import load_lifecycle
from .board.state_machine import LifecycleBuilder, lifecycle
from .dispatch import (
    acknowledge_task,
    dispatch_batch,
    find_idle_worker,
    fire_worker,
    get_acknowledged,
)
from .errors import (
    ConflictError,
    NotFoundError,
    QuadroError,
    TransitionError,
    ValidationError,
)
from .ombudsman import Ombudsman
from .pipeline import (
    BuiltPipeline,
    Pipeline,
    StageSpec,
    ToolDescriptor,
    generate_tool_descriptors,
)
from .runner import RunLoop
from .runtime import QuadroRuntime
from .ui import serve_board

__all__ = [
    "BoardClient",
    "BuiltPipeline",
    "ChiefAgent",
    "ConflictError",
    "DefaultTaskIdProvider",
    "LifecycleBuilder",
    "LocalA2ANetwork",
    "Ombudsman",
    "Pipeline",
    "QuadroBoard",
    "QuadroError",
    "QuadroRuntime",
    "RunLoop",
    "StageSpec",
    "ToolDescriptor",
    "TransitionError",
    "ValidationError",
    "WorkerAgent",
    "WorkerPool",
    "acknowledge_task",
    "dispatch_batch",
    "find_idle_worker",
    "fire_worker",
    "generate_tool_descriptors",
    "get_acknowledged",
    "lifecycle",
    "load_lifecycle",
    "serve_board",
]
