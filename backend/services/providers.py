"""
services/providers.py — lazily-constructed external clients (VA-A2 fix).

orchestrator/agents.py, rag/embedder.py and analytics/sentiment.py used to
each build their own `AsyncOpenAI(api_key=config.OPENAI_API_KEY)` at module
scope, which meant importing any of them (and therefore importing main.py)
required a valid API key to even exist as a process. A test can't substitute
a fake provider without patching module globals, and a key rotation needs a
restart. Routing every call site through get_openai_client() instead means
the client is built on first real use, not at import time, and tests can
monkeypatch this one function instead of three separate module globals.
"""
from functools import lru_cache

from openai import AsyncOpenAI

import config


@lru_cache
def get_openai_client() -> AsyncOpenAI:
    # timeout/max_retries bound every completion and embedding call made
    # through this client (VA-C1 fix) — previously unset, so a slow/hanging
    # OpenAI response could leave a call waiting indefinitely with no
    # fallback. Set once here rather than per call site so nothing new
    # added later can silently reintroduce an unbounded call.
    return AsyncOpenAI(
        api_key=config.OPENAI_API_KEY,
        timeout=config.OPENAI_REQUEST_TIMEOUT_SEC,
        max_retries=config.OPENAI_MAX_RETRIES,
    )
