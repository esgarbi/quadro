"""Shared constants for the newsroom saga modules."""

from pathlib import Path

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"
ARTICLES_DIR = Path(__file__).resolve().parent.parent / "output_2"
_TOKENS_KEY_PREFIX = "_tokens:"
