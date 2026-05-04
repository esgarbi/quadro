"""Saga definitions for the newsroom pipeline.

Each saga lives in its own module; this package re-exports the saga
objects and shared constants so existing imports remain stable.
"""

from ._common import ARTICLES_DIR
from .ideation import ideation_saga
from .research import research_saga
from .review import review_saga
from .writing import writing_saga

__all__ = [
    "ARTICLES_DIR",
    "ideation_saga",
    "research_saga",
    "review_saga",
    "writing_saga",
]
