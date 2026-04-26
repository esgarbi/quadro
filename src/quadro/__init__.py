"""Quadro public API."""

__version__ = "0.1.0"

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
    DRAIN_FLAG_KEY,
    acknowledge_task,
    dispatch_batch,
    find_idle_worker,
    fire_worker,
    get_acknowledged,
    is_draining,
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
    "DRAIN_FLAG_KEY",
    "DefaultTaskIdProvider",
    "LifecycleBuilder",
    "LocalA2ANetwork",
    "NotFoundError",
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
    "is_draining",
    "lifecycle",
    "load_lifecycle",
    "serve_board",
]
