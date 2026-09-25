"""
brain/memory.py — v2/v3 session memory: Redis-backed with rolling summary.

EVOLUTION FROM v1:
  v1:   plain Python dict, sliding window of last 10 turns, lost on process
        restart, not shared across multiple backend replicas.
  v2:   + rolling summary — once history exceeds ROLLING_MEMORY_MAX_TURNS,
        the oldest turns are collapsed into a single GPT-4o-mini generated
        summary message instead of being silently dropped, so long calls
        don't lose earlier context (caller's name, stated interest, etc).
  v3+:  + Redis-backed storage (Redis — Session Memory in the architecture
        diagram) so session state survives a backend restart and is shared
        across multiple FastAPI replicas behind a load balancer. Falls back
        to the original in-process dict automatically if Redis is
        unreachable — a call must never fail because the cache is down.
  v3.1: + background rolling summary. The summary GPT call used to be
        awaited inline inside add_turn(), which is itself awaited inline
        inside brain/agent.py's process_turn() — so the caller sat in dead
        silence for the summary call's full latency (observed: 20+ seconds)
        AFTER their real answer was already generated, purely for memory
        housekeeping that only matters for the *next* turn. It now runs as
        a background task; add_turn() returns as soon as the raw turn is
        persisted, and the collapsed/summarized history is swapped in
        whenever the background task finishes, before it's next read.

Redis key shape:  session:{call_id}  →  JSON list of {role, content} dicts
TTL: config.REDIS_SESSION_TTL (default 1h) — auto-expires abandoned sessions.
"""
import asyncio
import json
import logging
from typing import Dict, List

import config
from cache.redis_client import get_redis

logger = logging.getLogger(__name__)

# ── In-process fallback store (used when Redis is disabled/unreachable) ────
_sessions: Dict[str, List[Dict]] = {}

# ── Append-only raw digit ledger ───────────────────────────────────────────
# REAL BUG (2026-09-16 live call): book_meeting rejected a phone number the
# caller HAD said, logging stream='100'.
#
# brain/tools.py rebuilds the caller's "ground truth" digit stream by filtering
# get_history() for role == "user". That works until the rolling summary fires:
# _apply_rolling_summary REPLACES the old turns with a single role == "system"
# message, so the turn containing "my mobile number is 6369996595" stops
# existing as a user turn. The only user turns left were "Yeah.",
# "Uh, today, uh, 1:00 p.m." and "Yeah." — digits "100" — and the check
# correctly concluded the number was unsupported, because by then it genuinely
# was not in the history any more.
#
# Reading digits out of the summary instead is NOT a fix: the summary is
# LLM-generated, and the whole purpose of this check is comparing against raw
# transcript text the model cannot influence.
#
# So the digits get their own append-only ledger, written once per user turn
# and never summarized, truncated or rewritten. Same TTL and same
# Redis/in-process fallback as the session itself.
_digit_streams: Dict[str, str] = {}

MAX_TURNS = config.ROLLING_MEMORY_MAX_TURNS
_SESSION_KEY_PREFIX = "session:"
_DIGITS_KEY_PREFIX = "digits:"

# Hard cap so a very long call cannot grow the ledger without bound. 200 digits
# is ~20 phone numbers — far beyond any legitimate call.
_MAX_DIGIT_STREAM_LEN = 200

# ── Background rolling-summary bookkeeping ──────────────────────────────────
# Keyed per call_id. A per-process asyncio.Lock (not a distributed Redis
# lock) is sufficient here: every add_turn() for a given call_id is always
# invoked from the same backend process — the one holding that call's
# Twilio WebSocket connection for its entire lifetime — so there is never
# cross-replica contention on a single call's memory, even though the
# session data itself lives in shared Redis.
_summary_locks: Dict[str, asyncio.Lock] = {}
_summary_tasks: Dict[str, asyncio.Task] = {}


def _key(call_id: str) -> str:
    return f"{_SESSION_KEY_PREFIX}{call_id}"


def _digits_key(call_id: str) -> str:
    return f"{_DIGITS_KEY_PREFIX}{call_id}"


async def get_digit_stream(call_id: str) -> str:
    """Every digit the caller has actually uttered this call, in order.
    Survives rolling-summary compaction — see the comment on _digit_streams."""
    client = get_redis()
    if client is not None:
        try:
            raw = await client.get(_digits_key(call_id))
            return raw or ""
        except Exception as e:
            logger.warning(f"[{call_id}] Redis get_digit_stream failed, falling back to memory: {e}")
    return _digit_streams.get(call_id, "")


async def _append_digits(call_id: str, text: str) -> None:
    """Extract and append the digits from one raw user turn.

    Imported lazily: utils.phone is a leaf module today, but this keeps
    brain.memory free of any import-time dependency on it so neither can ever
    become a cycle."""
    from utils.phone import build_user_digit_stream

    digits = build_user_digit_stream([text])
    if not digits:
        return

    current = await get_digit_stream(call_id)
    combined = (current + digits)[-_MAX_DIGIT_STREAM_LEN:]

    client = get_redis()
    if client is not None:
        try:
            await client.set(_digits_key(call_id), combined, ex=config.REDIS_SESSION_TTL)
            return
        except Exception as e:
            logger.warning(f"[{call_id}] Redis append digits failed, falling back to memory: {e}")
    _digit_streams[call_id] = combined


def _get_summary_lock(call_id: str) -> asyncio.Lock:
    lock = _summary_locks.get(call_id)
    if lock is None:
        lock = asyncio.Lock()
        _summary_locks[call_id] = lock
    return lock


async def get_history(call_id: str) -> List[Dict]:
    """Return the conversation history for a call session."""
    client = get_redis()
    if client is not None:
        try:
            raw = await client.get(_key(call_id))
            if raw:
                return json.loads(raw)
            return []
        except Exception as e:
            logger.warning(f"[{call_id}] Redis get_history failed, falling back to memory: {e}")
    return _sessions.get(call_id, []).copy()


async def _save_history(call_id: str, history: List[Dict]) -> None:
    client = get_redis()
    if client is not None:
        try:
            await client.set(_key(call_id), json.dumps(history), ex=config.REDIS_SESSION_TTL)
            return
        except Exception as e:
            logger.warning(f"[{call_id}] Redis save_history failed, falling back to memory: {e}")
    _sessions[call_id] = history


async def add_turn(call_id: str, role: str, content: str) -> None:
    """
    Append a single turn to the session history and persist it immediately.

    This function must stay fast — it sits directly on the voice-reply
    critical path (brain/agent.py awaits it before the caller hears
    anything). Collapsing old turns into a rolling summary is real LLM
    latency that has nothing to do with the turn currently being spoken,
    so it is kicked off in the background instead of awaited here.
    """
    history = await get_history(call_id)
    history.append({"role": role, "content": content})
    await _save_history(call_id, history)
    logger.debug(f"[{call_id}] Memory: {len(history)} entries stored")

    # Record the caller's raw digits in the ledger the rolling summary never
    # touches. Cheap (a regex plus one Redis SET), and it must happen here, on
    # the same path that stores the turn, so the two cannot diverge.
    if role == "user":
        try:
            await _append_digits(call_id, content)
        except Exception as e:
            # Never let ledger bookkeeping break a live turn. A missing ledger
            # degrades to the old behaviour; it does not drop the call.
            logger.warning(f"[{call_id}] Could not append to digit ledger: {e}")

    if len(history) > MAX_TURNS:
        if config.ENABLE_ROLLING_MEMORY:
            _schedule_rolling_summary(call_id)
        else:
            # v1 behaviour: simple sliding window, oldest turns dropped.
            # Cheap (no LLM call), so this stays inline.
            await _save_history(call_id, history[-MAX_TURNS:])


def _schedule_rolling_summary(call_id: str) -> None:
    """
    Fire-and-forget the rolling summary. At most one summary run is ever
    in flight per call_id — if one is already running, a newly-crossed
    threshold just waits for the next turn to try again rather than
    stacking redundant GPT calls.
    """
    existing = _summary_tasks.get(call_id)
    if existing and not existing.done():
        return
    task = asyncio.create_task(_run_rolling_summary_background(call_id))
    _summary_tasks[call_id] = task


async def _run_rolling_summary_background(call_id: str) -> None:
    lock = _get_summary_lock(call_id)
    async with lock:
        try:
            # Re-fetch fresh — turns may have been added since this task
            # was scheduled, and another run may have already collapsed
            # the history down below the threshold in the meantime.
            history = await get_history(call_id)
            if len(history) <= MAX_TURNS:
                return
            new_history = await _apply_rolling_summary(call_id, history)
            await _save_history(call_id, new_history)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"[{call_id}] Background rolling summary failed: {e}")


async def _apply_rolling_summary(call_id: str, history: List[Dict]) -> List[Dict]:
    """
    v2 rolling summary memory.

    Collapses everything except the most recent (MAX_TURNS - 2) turns into
    a single system-role summary message, generated by a cheap model. This
    keeps the caller's name, stated interest, and any confirmed booking
    details in context even on long calls, instead of them scrolling out
    of a fixed-size window.

    Layout after collapsing:
      [ {role: system, content: "Earlier in this call: ..."}, <recent turns> ]
    """
    keep_recent = max(MAX_TURNS - 2, 2)
    already_summarized = bool(history) and history[0].get("role") == "system" and \
        history[0].get("content", "").startswith("Earlier in this call:")

    old_summary = history[0]["content"] if already_summarized else None
    to_summarize = history[1:-keep_recent] if already_summarized else history[:-keep_recent]
    recent = history[-keep_recent:]

    if not to_summarize:
        return history

    try:
        from services.providers import get_openai_client
        client = get_openai_client()

        transcript_snippet = "\n".join(
            f"{t['role']}: {t['content']}" for t in to_summarize if t.get("role") in ("user", "assistant")
        )
        prompt = (
            "Summarize the key facts from this phone call so far in 2-3 short "
            "sentences: caller's name, phone, what they're interested in, and "
            "any decisions or bookings made. Do not include pleasantries.\n\n"
        )
        if old_summary:
            prompt += f"Existing summary: {old_summary}\n\n"
        prompt += f"New turns to fold in:\n{transcript_snippet}"

        response = await client.chat.completions.create(
            model=config.ROLLING_MEMORY_SUMMARY_MODEL,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=120,
            temperature=0.2,
        )
        summary_text = response.choices[0].message.content.strip()
    except Exception as e:
        logger.warning(f"[{call_id}] Rolling summary generation failed, keeping window as-is: {e}")
        return history[-MAX_TURNS:]

    logger.info(f"[{call_id}] Rolling summary updated: {summary_text[:80]}")
    return [{"role": "system", "content": f"Earlier in this call: {summary_text}"}] + recent


async def clear_session(call_id: str) -> None:
    """Remove a session from memory when the call ends."""
    task = _summary_tasks.pop(call_id, None)
    if task and not task.done():
        # The call is over — no reply will ever read this summary again,
        # so don't waste an in-flight OpenAI call finishing it.
        task.cancel()
    _summary_locks.pop(call_id, None)

    client = get_redis()
    if client is not None:
        try:
            await client.delete(_key(call_id), _digits_key(call_id))
        except Exception as e:
            logger.warning(f"[{call_id}] Redis clear_session failed: {e}")
    if call_id in _sessions:
        del _sessions[call_id]
    _digit_streams.pop(call_id, None)
    logger.info(f"[{call_id}] Session cleared from memory")


async def get_active_sessions() -> List[str]:
    """Return list of currently active call IDs (in-process fallback only)."""
    client = get_redis()
    if client is not None:
        try:
            keys = await client.keys(f"{_SESSION_KEY_PREFIX}*")
            return [k[len(_SESSION_KEY_PREFIX):] for k in keys]
        except Exception as e:
            logger.warning(f"Redis get_active_sessions failed: {e}")
    return list(_sessions.keys())