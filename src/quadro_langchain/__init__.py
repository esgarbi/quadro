"""
LangChain / LangGraph adapter package for Quadro.

Provides :class:`LangChainReasoner` (per-step LLM call adapter conforming
to ``quadro.saga.reasoner.Reasoner``) and :class:`LangChainChiefRuntime`
(chief-loop LLM call + native ``stage(graph=...)`` / ``stage(supervisor=...)``
runtime conforming to ``quadro.runtime_plugins.FrameworkRuntime``).

The substrate (``quadro``) has zero LangChain imports; this package
imports ``quadro`` but is never imported by it. Install with::

    pip install quadro[langchain]

Usage::

    import os
    from langchain_openai import ChatOpenAI
    from quadro import Pipeline
    from quadro_langchain import LangChainReasoner, LangChainChiefRuntime

    def client_factory():
        return ChatOpenAI(
            model=os.environ["OPENAI_MODEL_ID"],
            api_key=os.environ["OPENAI_API_KEY"],
        )

    pipeline = (
        Pipeline(board)
        .reasoner(LangChainReasoner(client_factory=client_factory))
        .with_framework_runtime(
            LangChainChiefRuntime(client_factory=client_factory)
        )
        .stage(...)
        .build()
    )
"""

from ._internal import (
    LangChainStageSpec,
    configure,
    get_client_factory,
    llm_call,
    make_auto_execute_fn,
    tools_from_lifecycle,
)
from .reasoner import LangChainReasoner
from .runtime import LangChainChiefRuntime

__all__ = [
    "LangChainChiefRuntime",
    "LangChainReasoner",
    "LangChainStageSpec",
    "configure",
    "get_client_factory",
    "llm_call",
    "make_auto_execute_fn",
    "tools_from_lifecycle",
]
