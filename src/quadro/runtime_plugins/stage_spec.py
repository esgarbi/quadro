"""
Runtime plugin stage entrypoint helpers.

Declares the native framework entrypoints Quadro can route through
runtime plugins while preserving the existing prompt/schema adapter path.
"""

from __future__ import annotations

from typing import Any

_NATIVE_RUNTIME_ENTRYPOINTS = ("workflow", "graph", "supervisor", "saga")


def native_runtime_entrypoint(spec: Any) -> tuple[str, Any] | None:
    """Return the first configured native entrypoint on *spec*, if any."""
    for key in _NATIVE_RUNTIME_ENTRYPOINTS:
        value = getattr(spec, key, None)
        if value is not None:
            return key, value
    return None
