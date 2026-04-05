"""Quadro public API."""

from .a2a.dispatch import LocalA2ANetwork
from .agents.chief import ChiefAgent
from .agents.pool import WorkerPool
from .agents.worker import WorkerAgent
from .board.board import QuadroBoard
from .board.client import BoardClient
from .board.state_machine import LifecycleBuilder, lifecycle
from .ombudsman import Ombudsman
from .runner import RunLoop
from .ui import serve_board

__all__ = [
    "BoardClient",
    "ChiefAgent",
    "LifecycleBuilder",
    "LocalA2ANetwork",
    "Ombudsman",
    "QuadroBoard",
    "RunLoop",
    "WorkerAgent",
    "WorkerPool",
    "lifecycle",
    "serve_board",
]
