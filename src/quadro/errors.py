"""Quadro error hierarchy.

All domain errors inherit from QuadroError. Infrastructure errors
(transport failures, etc.) remain as RuntimeError.
"""

from __future__ import annotations


class QuadroError(Exception):
    """Base class for all Quadro domain errors."""


class TransitionError(QuadroError):
    """Raised when a task transition is invalid for its lifecycle profile."""


class NotFoundError(QuadroError):
    """Raised when a task, agent, or other entity is not found."""


class ConflictError(QuadroError):
    """Raised on idempotency key collision with a different payload."""


class ValidationError(QuadroError):
    """Raised when request data fails validation (missing fields, bad format)."""
