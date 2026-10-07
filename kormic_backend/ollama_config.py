"""Shared request settings for the local Qwen model."""

import os

# Allow slow CPU inference without expiring its shared capacity reservation.
QWEN_REQUEST_TIMEOUT_SECONDS = 900
QWEN_SLOT_TTL_SECONDS = 1860


def qwen_keep_alive():
    """Return the Ollama idle lifetime; a request overrides the server default."""
    return os.getenv("KORMIC_QWEN_KEEP_ALIVE", "3m").strip() or "3m"
