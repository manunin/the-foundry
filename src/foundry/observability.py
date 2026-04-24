from __future__ import annotations

import os

import structlog

log = structlog.get_logger()

_enabled = False


def init_langfuse() -> bool:
    """Initialize Langfuse from env. No-op if keys missing.

    Safe to call multiple times; first call with valid keys wins.
    Returns True when tracing is active, False otherwise.
    """
    global _enabled
    if _enabled:
        return True
    if not os.getenv("LANGFUSE_SECRET_KEY") or not os.getenv("LANGFUSE_PUBLIC_KEY"):
        log.info("langfuse.disabled", reason="missing keys")
        return False
    try:
        from langfuse import Langfuse

        Langfuse()
    except Exception as e:
        log.warning("langfuse.init_failed", error=str(e))
        return False
    _enabled = True
    log.info("langfuse.enabled", host=os.getenv("LANGFUSE_HOST", "default"))
    return True


def flush() -> None:
    """Flush pending traces. Call at end of `run_once` to avoid losing data."""
    if not _enabled:
        return
    try:
        from langfuse import get_client

        get_client().flush()
    except Exception as e:
        log.warning("langfuse.flush_failed", error=str(e))
