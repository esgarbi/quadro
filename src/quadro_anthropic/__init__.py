"""
Anthropic SDK adapter package for Quadro.

Provides :class:`AnthropicReasoner`, a per-step LLM call adapter conforming to
``quadro.saga.reasoner.Reasoner`` and backed by the Anthropic Python SDK.

Unlike :mod:`quadro_maf` and :mod:`quadro_langchain`, this package is
reasoner-only — there is no ``AnthropicChiefRuntime`` for chief-loop
integration. Anthropic ships an SDK plus a tool-use API rather than a full
agent framework with its own workflow runtime, so the reasoner-only shape is
the natural fit. If you need Claude-driven chief loops, the substrate's
``Pipeline.stage(execute_fn=...)`` path with a custom execute function gives
you full control without needing a ``FrameworkRuntime`` adapter.

The substrate (``quadro``) has zero Anthropic imports; this package imports
``quadro`` but is never imported by it. Install with::

    pip install quadro[anthropic]

Usage::

    import os
    from anthropic import Anthropic
    from quadro import Pipeline
    from quadro_anthropic import AnthropicReasoner

    def client_factory():
        return Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    pipeline = (
        Pipeline(board)
        .reasoner(AnthropicReasoner(client_factory=client_factory))
        .stage(...)
        .build()
    )
"""

from .reasoner import AnthropicReasoner

__all__ = ["AnthropicReasoner"]
