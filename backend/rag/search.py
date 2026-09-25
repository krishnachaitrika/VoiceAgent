"""
rag/search.py — pgvector semantic search with a two-tier cache.

KEY IMPROVEMENT (v1):
  Previously every search call re-embedded the query from scratch (~1.4s avg).
  Many callers ask similar questions in one call ("ServiceNow", "services",
  "generative AI"). With caching, the second identical/near-identical query
  costs 0ms instead of 1.4s.

KEY IMPROVEMENT (v3+ — "Redis — RAG Cache" in the architecture diagram):
  The cache below was per-process and per-call — it reset every time the
  backend restarted and wasn't shared across replicas. This adds a
  Redis-backed tier IN FRONT of it: the top-K chunks for a normalized query
  are cached in Redis for REDIS_RAG_CACHE_TTL (default 24h), shared across
  every backend instance, so a question one caller asked this morning is
  instant for a different caller this afternoon — pgvector is skipped
  entirely on a hit.

  Cache strategy (checked in this order):
    1. Redis result cache      (shared across replicas, 24h TTL)
    2. In-process result cache (this replica only, no TTL — process lifetime)
    3. In-process embedding cache (skip re-embedding even on a full miss)
    4. Full embed + pgvector search
"""
import logging
from typing import Optional
from sqlalchemy import text
from database.base import AsyncSessionLocal
from rag.embedder import embed_text
import config
from cache.redis_client import get_redis

logger = logging.getLogger(__name__)

_RAG_CACHE_PREFIX = "rag:"


async def _rag_cache_key(query: str) -> str:
    """Namespace the RAG cache by the knowledge base's current version.

    VA-T-016: the key used to be rag:<question> alone, so deleting a document
    left its text reachable under that key for the full REDIS_RAG_CACHE_TTL
    (24h) and callers kept being told content that had been removed — while
    the API, the documents page and the database all reported it gone.

    Including the version means an upload or delete makes every prior entry
    unreachable at once, with no keyspace scan and no reverse index to drift.
    Orphans expire on their existing TTL.
    """
    from cache.kb_version import get_kb_version

    return f"{_RAG_CACHE_PREFIX}{await get_kb_version()}:{_normalize_query(query)}"

# Named so callers (orchestrator/agents.py) can check "did we actually find
# anything" without comparing against a duplicated literal string.
KB_NOT_FOUND_MESSAGE = "No relevant information found."

# VA-T-012 / VA-T-013 — RETRIEVAL IS A BAND, NOT A CLIFF.
#
# A single pass/fail threshold produced both failure modes the QA round
# measured, from the same number:
#
#   retrieval failure 80%  Legitimate paraphrases of answerable questions
#                          landed just OVER the line and were rejected
#                          outright — 0.7461, 0.7562, 0.7861. "What's your
#                          starting price?" is not an off-topic question.
#
#   hallucination 13.3%    With nothing retrieved, the specialist answered
#                          from the model's own training anyway. One reply
#                          was FACTUALLY CORRECT with zero retrieval — right
#                          answer, wrong source. That is the dangerous case,
#                          because nothing flags it until the model's
#                          training and the client's real pricing disagree.
#
# Three bands instead:
#
#   distance <= STRONG    confident match; answer normally
#   STRONG..RELEVANCE     usable but uncertain; the passage is passed through
#                         with instructions to answer ONLY from it and to say
#                         so plainly if the answer is not in there
#   > RELEVANCE           nothing useful; refuse and offer a callback
#
# The middle band is what recovers the paraphrases without letting genuinely
# unrelated content through. The prefix below marks it so the caller of this
# function can tell the two apart — the text is stripped before it ever
# reaches a prompt.
KB_WEAK_MATCH_PREFIX = "[[WEAK_MATCH]]"

# ── In-memory cache ────────────────────────────────────────────────────────────
# Stores: { normalized_query: (embedding_list, result_string) }
_embedding_cache: dict[str, tuple[list, str]] = {}
_result_cache:    dict[str, str]               = {}
MAX_CACHE_SIZE = 50


def _normalize_query(query: str) -> str:
    """Normalize query for cache key — lowercase, strip whitespace."""
    return query.lower().strip()


def _cache_get(query: str) -> Optional[str]:
    """Return cached result string if available, else None."""
    return _result_cache.get(_normalize_query(query))


def _cache_get_embedding(query: str) -> Optional[list]:
    """Return cached embedding vector if available, else None."""
    key = _normalize_query(query)
    entry = _embedding_cache.get(key)
    return entry[0] if entry else None


def _cache_set(query: str, embedding: list, result: str) -> None:
    """Store embedding and result in cache. Evict oldest if over limit."""
    key = _normalize_query(query)
    if len(_result_cache) >= MAX_CACHE_SIZE:
        oldest = next(iter(_result_cache))
        _result_cache.pop(oldest, None)
        _embedding_cache.pop(oldest, None)
    _embedding_cache[key] = (embedding, result)
    _result_cache[key] = result


async def search_knowledge_base(
    query: str,
    limit: int = 3,
    precomputed_embedding: Optional[list] = None,
) -> str:
    """
    Semantic search on Supabase pgvector documents table.

    Flow:
      1. Check result cache — if hit, return immediately (0ms)
      2. Check embedding cache — if hit, skip embed_text call (~1.4s saved)
      3. If both miss — use precomputed_embedding if the caller supplied one
         (e.g. orchestrator/graph.py fires this off in parallel with the
         Receptionist LLM call), otherwise embed query fresh
      4. Search pgvector
      5. Reject the result if even the nearest chunk is below
         config.RAG_RELEVANCE_THRESHOLD relevance (garbled/off-topic query)
      6. Otherwise cache and return the top-N chunks joined as a string

    Args:
        query: The caller's question / search intent
        limit: Number of chunks to return (default 3)
        precomputed_embedding: Optional embedding already computed by the
            caller (e.g. fired concurrently with routing). Used only on a
            full cache miss — if the result or embedding cache already has
            this query, precomputed_embedding is ignored entirely, so
            passing it is always safe and never changes cache behaviour.

    Returns:
        Relevant document chunks joined by separator, or fallback message.
    """
    # ── Level 0: Redis result cache hit (shared across all replicas) ──────────
    redis_client = get_redis()
    if redis_client is not None:
        try:
            redis_hit = await redis_client.get(await _rag_cache_key(query))
            if redis_hit:
                logger.info(f"[search_kb] Redis cache hit: '{query[:50]}' → {len(redis_hit)} chars")
                _cache_set(query, [], redis_hit)  # warm the in-process tier too
                return redis_hit
        except Exception as e:
            logger.warning(f"[search_kb] Redis RAG cache read failed: {e}")

    # ── Level 1: Full result cache hit ────────────────────────────────────────
    cached_result = _cache_get(query)
    if cached_result:
        logger.info(f"[search_kb] Cache hit (result): '{query[:50]}' → {len(cached_result)} chars")
        return cached_result

    try:
        # ── Level 2: Embedding cache hit ──────────────────────────────────────
        cached_embedding = _cache_get_embedding(query)
        if cached_embedding:
            logger.info(f"[search_kb] Cache hit (embedding): '{query[:50]}'")
            embedding = cached_embedding
        elif precomputed_embedding is not None:
            # ── Level 3a: Caller already embedded this in parallel ─────────────
            logger.info(f"[search_kb] Using precomputed embedding (parallel prefetch): '{query[:50]}'")
            embedding = precomputed_embedding
        else:
            # ── Level 3b: Full embed + search ──────────────────────────────────
            embedding = await embed_text(query)

        embedding_str = "[" + ",".join(str(x) for x in embedding) + "]"

        sql = text("""
            SELECT content, (embedding <=> :embedding) AS distance
            FROM documents
            ORDER BY embedding <=> :embedding
            LIMIT :limit
        """)

        async with AsyncSessionLocal() as db:
            result = await db.execute(sql, {"embedding": embedding_str, "limit": limit})
            rows = result.fetchall()

        if not rows:
            return KB_NOT_FOUND_MESSAGE

        # rows are ordered nearest-first, so rows[0] is the best possible
        # match for this query — if even that isn't relevant, the other
        # two are guaranteed to be worse. Reject the whole result rather
        # than handing GPT-4o unrelated content to answer confidently from.
        top_distance = float(rows[0][1])
        logger.info(
            f"[search_kb] Top match distance: {top_distance:.4f} "
            f"(threshold={config.RAG_RELEVANCE_THRESHOLD}) for: '{query[:50]}'"
        )

        if top_distance > config.RAG_RELEVANCE_THRESHOLD:
            logger.info(
                f"[search_kb] REJECTED (distance {top_distance:.4f} > "
                f"{config.RAG_RELEVANCE_THRESHOLD}) for: '{query[:50]}' — the "
                f"specialist will refuse rather than answer unsourced"
            )
            _cache_set(query, embedding, KB_NOT_FOUND_MESSAGE)
            if redis_client is not None:
                try:
                    await redis_client.set(
                        await _rag_cache_key(query),
                        KB_NOT_FOUND_MESSAGE,
                        ex=config.REDIS_RAG_CACHE_TTL,
                    )
                except Exception as e:
                    logger.warning(f"[search_kb] Redis RAG cache write failed: {e}")
            return KB_NOT_FOUND_MESSAGE

        chunks = [row[0] for row in rows]

        # Previously: once the TOP chunk passed the threshold, ALL `limit`
        # chunks were included regardless of their own individual distance
        # — meaning chunk 2 or 3 could be a weak/unrelated match sitting
        # right next to the real answer in GPT-4o's prompt. That kind of
        # irrelevant-but-present context is exactly what nudges a model
        # toward blending facts from unrelated chunks (hallucination) or
        # diluting a clear answer with unrelated tangents.
        #
        # Now each chunk is checked individually — only chunks that
        # themselves pass the relevance threshold are handed to GPT-4o.
        # The top chunk (already validated above) is always kept even if
        # it's the only one that passes, so a single strong match still
        # produces a full answer.
        relevant_chunks = [
            row[0] for row in rows
            if float(row[1]) <= config.RAG_RELEVANCE_THRESHOLD
        ]
        if not relevant_chunks:
            relevant_chunks = [chunks[0]]  # top chunk already passed above

        result_str = "\n\n---\n\n".join(relevant_chunks)

        # Tag the middle band so the specialist knows to answer ONLY from this
        # passage. Without the distinction it treats a 0.74 match exactly like
        # a 0.42 one — which is how a paraphrase that retrieved marginally
        # related text became a confident, unsourced answer.
        #
        # The marker is stripped in orchestrator/agents.py before the text
        # reaches any prompt, so the model never sees it.
        if top_distance > config.RAG_STRONG_MATCH_THRESHOLD:
            logger.info(
                f"[search_kb] WEAK match (distance {top_distance:.4f} in band "
                f"{config.RAG_STRONG_MATCH_THRESHOLD}-{config.RAG_RELEVANCE_THRESHOLD}) "
                f"— passage passed through under strict answer-only-from-this rules"
            )
            result_str = KB_WEAK_MATCH_PREFIX + result_str
        else:
            logger.info(f"[search_kb] STRONG match (distance {top_distance:.4f})")

        # Store in cache for next time (in-process tier always; Redis tier
        # too if available, so other replicas benefit immediately)
        _cache_set(query, embedding, result_str)
        if redis_client is not None:
            try:
                await redis_client.set(
                    await _rag_cache_key(query),
                    result_str,
                    ex=config.REDIS_RAG_CACHE_TTL,
                )
            except Exception as e:
                logger.warning(f"[search_kb] Redis RAG cache write failed: {e}")
        logger.info(f"[search_kb] Result length: {len(result_str)} chars")
        return result_str

    except Exception as e:
        logger.error(f"Knowledge base search failed: {e}")
        return KB_NOT_FOUND_MESSAGE