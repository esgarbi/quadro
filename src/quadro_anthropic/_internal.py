"""
Internal helpers for :mod:`quadro_anthropic.reasoner`.

The public surface is re-exported from :mod:`quadro_anthropic`; nothing here is
part of the documented API.
"""

from __future__ import annotations

import logging
from typing import Any

try:
    from anthropic import Anthropic
except ImportError:  # pragma: no cover - exercised only without the extra
    Anthropic = None  # type: ignore[assignment,misc]

logger = logging.getLogger(__name__)

_ANTHROPIC_IMPORT_ERROR: str = (
    "Anthropic SDK is required for this module.  "
    "Install it with:  pip install 'quadro[anthropic]'"
)


def _ensure_anthropic() -> None:
    """Raise a friendly error if the optional Anthropic SDK is unavailable."""
    if Anthropic is None:
        raise ImportError(_ANTHROPIC_IMPORT_ERROR)


def _extract_tokens(response: Any) -> int:
    """Return total Anthropic input + output tokens, or 0 if unavailable."""
    try:
        usage = getattr(response, "usage", None)
        if usage is None:
            return 0
        input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
        output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
        return input_tokens + output_tokens
    except Exception as exc:  # noqa: BLE001
        logger.debug("token extraction failed; returning 0: %s", exc)
        return 0
