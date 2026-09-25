"""
cache/redis_client.py — one shared async Redis connection pool for the
whole app (session memory, LLM cache, RAG cache, event bus all use this).

DESIGN — graceful degradation:
  Redis is a performance/scale layer, not a correctness dependency. If Redis
  is down or REDIS_ENABLED=false, every caller in this codebase falls back
  to safe in-process behaviour (smaller cache, no cross-process sharing,
  slightly higher latency) instead of crashing the call. This matters
  because a voice call must never fail just because the cache layer is
  unavailable — see get_redis() usage in session_cache.py / llm_cache.py /
  rag_cache.py / events/bus.py, all wrapped in try/except.
"""
import logging
from typing import Optional

import redis.asyncio as aioredis
import config

logger = logging.getLogger(__name__)

_pool: Optional[aioredis.Redis] = None
_connection_failed: bool = False


def get_redis() -> Optional[aioredis.Redis]:
    """
    Return the shared Redis client, or None if Redis is disabled/unreachable.
    Lazily creates the connection pool on first call. Never raises — callers
    should treat a None return as "fall back to non-Redis behaviour".
    """
    global _pool, _connection_failed

    if not config.REDIS_ENABLED or _connection_failed:
        return None

    if _pool is None:
        try:
            _pool = aioredis.from_url(
                config.REDIS_URL,
                encoding="utf-8",
                decode_responses=True,
                max_connections=50,
                socket_connect_timeout=2,
                socket_timeout=2,
            )
        except Exception as e:
            logger.warning(f"Redis connection pool creation failed: {e}. Running without Redis.")
            _connection_failed = True
            return None

    return _pool


async def ping() -> bool:
    """Health check used by /health and /ready endpoints."""
    client = get_redis()
    if client is None:
        return False
    try:
        return bool(await client.ping())
    except Exception as e:
        logger.warning(f"Redis ping failed: {e}")
        return False


async def close() -> None:
    """Close the pool cleanly on app shutdown."""
    global _pool
    if _pool is not None:
        try:
            await _pool.aclose()
        except Exception:
            pass
        _pool = None
