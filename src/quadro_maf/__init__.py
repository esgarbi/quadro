"""
Microsoft Agent Framework adapter package for Quadro.

Provides :class:`MafReasoner` (per-step LLM call adapter conforming to
``quadro.saga.reasoner.Reasoner``) and :class:`MafChiefRuntime`
(chief-loop LLM call adapter conforming to
``quadro.runtime_plugins.FrameworkRuntime``).

The substrate (``quadro``) has zero MAF imports; this package imports
``quadro`` but is never imported by it. Install with::

    pip install quadro[maf]

Usage::

    import os
    from agent_framework.openai import OpenAIChatClient
    from quadro import Pipeline
    from quadro_maf import MafReasoner, MafChiefRuntime

    def client_factory():
        return OpenAIChatClient(
            model=os.environ["OPENAI_MODEL_ID"],
            api_key=os.environ["OPENAI_API_KEY"],
        )

    pipeline = (
        Pipeline(board)
        .reasoner(MafReasoner(client_factory=client_factory))
        .with_framework_runtime(MafChiefRuntime(client_factory=client_factory))
        .stage(...)
        .build()
    )
"""

from ._internal import (
    MafStageSpec,
    configure,
    llm_call,
    tools_from_lifecycle,
)
from .reasoner import MafReasoner
from .runtime import MafChiefRuntime

__all__ = [
    "MafChiefRuntime",
    "MafReasoner",
    "MafStageSpec",
    "configure",
    "llm_call",
    "tools_from_lifecycle",
]
