"""
brain/agent.py — the single call-handling entry point used by
voice/stream.py. All actual "thinking" now happens in the LangGraph
orchestrator (orchestrator/graph.py) — this file is a thin, stable wrapper
around it that handles the things every turn needs regardless of which
agent inside the graph answers: guardrails, the Redis LLM cache, and
conversation memory.

WHY THE OLD SINGLE-CALL PATH WAS REMOVED:
  Earlier versions of this file also contained a standalone "ask GPT-4o
  once, call a tool if needed" implementation, toggled by a config flag
  alongside the LangGraph path. That flag and the duplicate code path have
  been removed — the LangGraph orchestrator is now the only agent brain in
  this codebase, so there is exactly one place tool-calling logic lives
  (orchestrator/agents.py), not two copies to keep in sync.

WHAT THIS FILE ACTUALLY DOES, PER TURN:
  1. Input guardrail — jailbreak detection, PII masking for storage
  2. Redis LLM cache check — skip the whole orchestrator call on a repeat
     factual question (>= MIN_CACHEABLE_WORDS words)
  3. Delegates to orchestrator.graph.run_turn() for the real work
  4. Cleans GPT-4o markdown artefacts out of the response for voice
  5. Output guardrail — blocks any system-prompt leak, masks PII in storage
  6. Populates the LLM cache — ONLY for turns the orchestrator itself
     marked safe (no side-effect tool ran — see orchestrator/graph.py)
  7. Saves the turn to memory (Redis-backed rolling-summary memory)
"""
import logging
import time
from typing import Tuple

from brain.memory import get_history, add_turn
from cache.llm_cache import get_cached, set_cached
from database.base import AsyncSessionLocal
from database import crud
from guardrails.input_guardrail import check_input, jailbreak_reminder
from guardrails.output_guardrail import check_output
from orchestrator.graph import run_turn as orchestrator_run_turn
import config

logger = logging.getLogger(__name__)

# Cache lookups are only attempted for utterances this long or longer, to
# avoid accidentally serving a cached answer for a short context-dependent
# reply like "yes" / "book it" / "9876543210" where the correct response
# depends entirely on conversation history, not the words themselves.
MIN_CACHEABLE_WORDS = 4


def _clean_response_text(text: str) -> str:
    """
    Clean the orchestrator's response for voice output.

    Removes markdown that sounds terrible when read aloud:
      - **bold** → plain text
      - *italic* → plain text
      - Numbered list items (1. 2. 3.) → natural sentence flow
      - Bullet points (- • *) → comma-separated or new sentence
      - Excessive newlines → single space

    Also strips any accidental re-greeting that slips through a prompt.
    """
    import re

    text = re.sub(r'\*{1,3}([^*]+)\*{1,3}', r'\1', text)
    text = re.sub(r'^\s*\d+\.\s+', '', text, flags=re.MULTILINE)
    text = re.sub(r'^\s*[-•*]\s+', '', text, flags=re.MULTILINE)
    text = re.sub(r'\n+', ' ', text)
    text = re.sub(r'  +', ' ', text)

    greet_patterns = [
        r'^Hello[,!]?\s+',
        r'^Hi[,!]?\s+',
        r'^Good (morning|afternoon|evening)[,!]?\s+',
        r'^Thank you for calling[^.!?]*[.!?]\s*',
    ]
    for pattern in greet_patterns:
        text = re.sub(pattern, '', text, flags=re.IGNORECASE)

    return text.strip()


async def process_turn(call_id: str, user_text: str, language: str) -> Tuple[str, float]:
    """
    Process one conversational turn end-to-end.

    Args:
        call_id:   Unique call session identifier
        user_text: Clean transcribed speech from caller
        language:  Caller's speech may be in any language ElevenLabs Scribe
                   recognizes (STT is not restricted to one language) — this
                   is passed through as "en-IN" everywhere in this codebase
                   since replies are always generated in English regardless
                   of what language the caller spoke.

    Returns:
        (response_text, turn_cost_usd)
    """
    start = time.perf_counter()

    # ── Guardrails: input ───────────────────────────────────────────────────
    guard_in = check_input(call_id, user_text) if config.ENABLE_GUARDRAILS else None
    storage_user_text = guard_in.storage_text if guard_in else user_text

    # ── Save the caller's message to history IMMEDIATELY, before any GPT
    # call — not after the turn completes. This used to happen only at the
    # very end (see the old comment this replaced), which meant a turn
    # cancelled mid-flight by barge-in or smart-hearing (voice/stream.py:
    # run_turn awaits process_turn(), and that await raises CancelledError
    # the instant a newer utterance supersedes it) silently lost the
    # caller's actual words — including real content like a name or partial
    # phone number — because they were never written to history at all.
    # The very next turn's GPT call then had zero memory the caller had
    # already said it, and re-asked for information it was already given.
    # This is a REAL, confirmed bug from live call testing, not a
    # theoretical one — see the 2026-07-17 call where "my name is Velu and
    # my mobile number is 63, 63" was cancelled by the next fragment
    # arriving, and the agent asked for both again three turns later.
    #
    # Saving here instead means the caller's words are durably recorded the
    # moment they're transcribed, regardless of whether this turn's own
    # reply ever gets spoken — cancellation now only ever discards a reply
    # that hadn't been generated yet, never information the caller already
    # provided.
    await add_turn(call_id, "user", storage_user_text)

    # ── LLM cache — skip the orchestrator entirely on a cache hit ──────────
    word_count = len(user_text.split())
    cache_eligible = word_count >= MIN_CACHEABLE_WORDS
    if cache_eligible:
        cached = await get_cached(user_text, language)
        if cached:
            logger.info(f"[{call_id}] LLM cache hit — skipping the orchestrator entirely")
            await add_turn(call_id, "assistant", cached)
            return cached, 0.0

    # REAL-BUG FIX: guard_in.jailbreak_detected was already being computed
    # above and jailbreak_reminder() already existed for exactly this case
    # — but nothing ever passed it into the orchestrator, so a detected
    # attempt was only ever logged, never actually reinforced to the LLM.
    # None on every normal turn (the overwhelming majority), so this adds
    # no extra cost/latency except on the rare turn where a jailbreak
    # pattern is actually flagged.
    security_note = jailbreak_reminder() if (guard_in and guard_in.jailbreak_detected) else None

    try:
        final_text, total_cost, cacheable = await orchestrator_run_turn(
            call_id, user_text, language, security_note=security_note
        )
    except Exception as e:
        # VA-C1 fix: this used to just speak an apology with nothing behind
        # it — no escalation was ever actually recorded, so "someone will
        # call you back" was a promise the system never kept. Now that
        # every OpenAI call has a bounded timeout+retry (services/providers.py),
        # this except block is reachable in bounded time instead of hanging
        # forever, and actually creates the escalation it promises.
        logger.error(f"[{call_id}] Orchestrator error: {e}")
        final_text, total_cost, cacheable = (config.LLM_FALLBACK_MESSAGE, 0.0, False)
        try:
            async with AsyncSessionLocal() as db:
                await crud.upsert_escalation(
                    db, call_id=call_id, reason=f"LLM call failed: {e}", transcript_snippet=user_text,
                )
        except Exception as escalation_error:
            logger.error(f"[{call_id}] Failed to record escalation for LLM failure: {escalation_error}")

    final_text = _clean_response_text(final_text)

    # ── Guardrails: output ───────────────────────────────────────────────────
    storage_response = final_text
    if config.ENABLE_GUARDRAILS:
        guard_out = check_output(call_id, final_text)
        final_text = guard_out.speak_text
        storage_response = guard_out.storage_text

    # ── Populate LLM cache — only for turns the orchestrator marked safe ───
    if cache_eligible and cacheable:
        await set_cached(user_text, language, final_text)

    # ── Save assistant reply to memory (PII-masked storage text where
    # guardrails ran). The caller's own message was already saved at the
    # top of this function, immediately on receipt — see the comment there
    # for why that matters.
    await add_turn(call_id, "assistant", storage_response)

    elapsed = time.perf_counter() - start
    logger.info(
        f"[{call_id}] Turn complete: '{final_text[:60]}' | "
        f"cost=${total_cost:.5f} | latency={elapsed:.3f}s"
    )
    return final_text, total_cost