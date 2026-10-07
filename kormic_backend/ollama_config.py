"""Shared request settings for the local Qwen model."""

import os


def qwen_keep_alive():
    """Return the Ollama idle lifetime; a request overrides the server default."""
    return os.getenv("KORMIC_QWEN_KEEP_ALIVE", "3m").strip() or "3m"
