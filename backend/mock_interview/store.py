"""
mock_interview/store.py — where interview sessions live between requests.

Same degradation rule as the rest of the codebase (see cache/redis_client.py):
Redis when it is available, so a session survives a backend restart and works
behind several replicas; an in-process dict when it is not, so a Redis outage
never breaks an interview that is in progress on this process.

Sessions expire after config.INTERVIEW_SESSION_TTL either way. Nothing here is
written to Postgres — an interview is practice data, not a business record.
"""
import json
import logging
import time
from typing import Dict, Optional, Tuple

import config
from cache.redis_client import get_redis

logger = logging.getLogger(__name__)

_KEY_PREFIX = "interview:"

# In-process fallback: session_id -> (expires_at_monotonic, json_blob).
_local: Dict[str, Tuple[float, str]] = {}
# A bound so a process that runs for weeks without Redis cannot grow forever.
_MAX_LOCAL_SESSIONS = 500


def _key(session_id: str) -> str:
    return f"{_KEY_PREFIX}{session_id}"


def _prune_local() -> None:
    now = time.monotonic()
    for sid in [s for s, (exp, _) in _local.items() if exp <= now]:
        _local.pop(sid, None)
    while len(_local) > _MAX_LOCAL_SESSIONS:
        oldest = min(_local.items(), key=lambda item: item[1][0])[0]
        _local.pop(oldest, None)


async def save_session(session: dict) -> None:
    blob = json.dumps(session)
    client = get_redis()
    if client is not None:
        try:
            await client.set(_key(session["id"]), blob, ex=config.INTERVIEW_SESSION_TTL)
            return
        except Exception as e:
            logger.warning(f"[interview {session['id']}] Redis save failed, keeping in memory: {e}")
    _local[session["id"]] = (time.monotonic() + config.INTERVIEW_SESSION_TTL, blob)
    _prune_local()


async def load_session(session_id: str) -> Optional[dict]:
    client = get_redis()
    if client is not None:
        try:
            raw = await client.get(_key(session_id))
            if raw:
                return json.loads(raw)
        except Exception as e:
            logger.warning(f"[interview {session_id}] Redis load failed, checking memory: {e}")
    entry = _local.get(session_id)
    if entry is None:
        return None
    expires_at, blob = entry
    if expires_at <= time.monotonic():
        _local.pop(session_id, None)
        return None
    return json.loads(blob)
