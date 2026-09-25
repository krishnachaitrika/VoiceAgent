"""
cache/kb_version.py — a fingerprint of the knowledge base's current contents.

WHY THIS EXISTS

Two separate defects, one missing concept.

  VA-T-003  The LLM cache key covered agent name, company name and the prompt
            template — but nothing about the documents. Correct a fact,
            re-upload, and every already-cached answer kept serving the OLD
            fact for up to REDIS_LLM_CACHE_TTL.

  VA-T-016  The RAG cache is keyed on the question alone. Delete a document
            and its text stayed in Redis under rag:<question> for up to
            REDIS_RAG_CACHE_TTL — 24 hours by default. Confirmed live: a probe
            document was deleted, the API reported the chunks gone, the
            documents page showed the file removed, and the agent went on
            reading its content to callers with 86,006 seconds of TTL left.

VA-T-016 is the more serious of the two. A stale answer is wrong; deleted
content is material someone deliberately REMOVED — because it was incorrect,
confidential, or legally problematic — still being spoken aloud, with every
surface reporting success.

THE APPROACH: VERSION THE KEY, DON'T HUNT THE KEYS

The obvious fix is to delete the matching Redis keys when a document changes.
That is fragile: the RAG key is derived from the caller's question, so there is
no way to know which keys a given document contributed to without scanning the
whole keyspace (SCAN over a live Redis, per delete) or keeping a reverse index
that can itself drift.

Instead the version becomes part of both cache keys. Change the knowledge base
and every previously cached entry is simply unreachable — no scan, no delete,
no reverse index, nothing to drift. Orphaned entries age out on their existing
TTL.

WHAT THE VERSION IS DERIVED FROM

Row count and the most recent created_at of the documents table. Between them
they detect every mutation that matters:

    upload  -> count rises, max(created_at) moves
    delete  -> count falls
    replace -> both change

Derived rather than incremented, deliberately: a counter someone forgets to
bump is a silent correctness bug, and the failure mode is exactly the one being
fixed here. This cannot be forgotten because nothing has to remember it.

COST

One cheap aggregate query, cached in-process for KB_VERSION_TTL_SECONDS. At the
default that is at most one query per minute per worker, off the latency path —
against the alternative of serving deleted content for a day.
"""
import logging
import time
from typing import Optional

from sqlalchemy import func, select

import config
from database.base import AsyncSessionLocal
from database.models import Document

logger = logging.getLogger(__name__)

# Process-local cache. Deliberately not Redis: this is read on every cached
# turn, it is tiny, and a stale value costs at most KB_VERSION_TTL_SECONDS of
# continuing to serve the previous version — bounded and small, where the bug
# it replaces was unbounded up to the full cache TTL.
_cached_version: Optional[str] = None
_cached_at: float = 0.0

# Used when the database cannot be reached. A FIXED string, not a timestamp:
# a changing fallback would silently invalidate every cached answer on each
# turn during a database blip, turning a brief outage into a cost spike and a
# latency cliff. Holding the key steady means a blip degrades to "serves the
# last known-good cache", which is the safer failure.
_FALLBACK_VERSION = "kb-unavailable"


async def get_kb_version() -> str:
    """Return a short token identifying the knowledge base's current contents.

    Changes whenever a document is added or removed. Never raises — a failure
    here must not break a live call.
    """
    global _cached_version, _cached_at

    now = time.monotonic()
    if _cached_version is not None and (now - _cached_at) < config.KB_VERSION_TTL_SECONDS:
        return _cached_version

    try:
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(func.count(Document.id), func.max(Document.created_at))
            )
            count, latest = result.one()
    except Exception as e:
        logger.warning(f"Could not read knowledge-base version, using fallback: {e}")
        # Do NOT cache the fallback — the next call should retry rather than
        # hold a degraded key for the full TTL.
        return _FALLBACK_VERSION

    # int(timestamp) rather than the full datetime: second resolution is far
    # finer than any realistic upload cadence, and it keeps the cache key short.
    stamp = int(latest.timestamp()) if latest is not None else 0
    version = f"{count or 0}-{stamp}"

    if version != _cached_version and _cached_version is not None:
        logger.info(
            f"Knowledge base changed ({_cached_version} -> {version}) — "
            f"previously cached answers and retrievals are now unreachable."
        )

    _cached_version = version
    _cached_at = now
    return version


def invalidate_kb_version() -> None:
    """Force the next read to re-query.

    Called from the upload and delete paths so a change takes effect on the
    very next turn rather than after KB_VERSION_TTL_SECONDS. The TTL alone
    would eventually catch it; this makes it immediate, which matters when the
    reason for a deletion is that the content should not be spoken again.
    """
    global _cached_version, _cached_at
    _cached_version = None
    _cached_at = 0.0