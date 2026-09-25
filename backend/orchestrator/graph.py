"""
orchestrator/graph.py — builds and runs the LangGraph supervisor graph.
This is the ONLY brain in this codebase — the earlier single-call
GPT-4o approach has been removed entirely (see README "Architecture" for
why). `run_turn()` is called directly by brain/agent.py, which wraps it
with guardrails, Redis LLM cache, and conversation memory.

              ┌──────────────┐
   caller ──▶ │ Receptionist │──▶ route: "direct"     ──▶ END (short reply)
              └──────────────┘
                     │
       ┌─────────────┼──────────────┐
       ▼             ▼              ▼
  "knowledge"    "action"     "escalation"
       │             │              │
       ▼             ▼              ▼
  Knowledge      Action        Escalation
    Agent         Agent           Agent
       │             │              │
       └─────────────┴──────────────┘
                     ▼
                    END

This is a thin wrapper — the actual agent logic lives in agents.py. The
graph is compiled once at import time and reused for every turn.

SPEED OPTIMIZATION — sticky routing:
  Without this, EVERY turn pays for a Receptionist call before reaching a
  specialist — even short mid-flow replies like "Ravi" or "9876543210"
  during a booking. Those replies obviously belong to whichever agent is
  already mid-conversation with the caller, so re-classifying them is pure
  wasted latency.

  We store the last route in Redis for STICKY_ROUTE_TTL seconds (default
  120s). On the next turn, if a sticky route exists AND the caller's
  utterance is short (<= STICKY_MAX_WORDS words — a proxy for "this is
  probably a quick follow-up, not a brand new topic") AND it isn't pure
  chit-chat/acknowledgement ("okay buddy", "thanks" — see
  _is_pure_acknowledgement below), we skip the Receptionist entirely and go
  straight to that specialist node. Anything longer, a pure acknowledgement,
  or once the TTL expires, goes through the Receptionist again as normal —
  so a genuine topic change (or a caller just saying "thanks") is never
  trapped in the wrong agent.
"""
import asyncio
import logging
import re
from typing import Optional, Tuple

from langgraph.graph import StateGraph, END

from orchestrator.state import AgentState
from orchestrator.agents import (
    receptionist_node, knowledge_node, action_node, escalation_node,
)
from brain.memory import get_history
from cache.redis_client import get_redis
from rag.embedder import embed_text
from rag.search import search_knowledge_base
from cache.settings_cache import get_live_settings
import config

logger = logging.getLogger(__name__)

STICKY_MAX_WORDS = 6
_STICKY_PREFIX = "sticky_route:"
_NODE_FOR_ROUTE = {
    "knowledge": knowledge_node,
    "action": action_node,
    "escalation": escalation_node,
}

# ── Sticky-route acknowledgement guard ──────────────────────────────────────
# Word count alone isn't a safe proxy for "quick follow-up to the current
# specialist" — pure chit-chat ("okay buddy", "thanks", "alright") is also
# short, but has nothing for a specialist to act on. Routing it straight
# into knowledge/action/escalation wastes a full embedding + KB search +
# GPT-4o call to produce a generic "let me know if you need anything else"
# reply, when the Receptionist would classify it correctly (and far more
# cheaply) as "direct" small talk instead.
#
# This mirrors voice/stream.py's FILLERS set — same idea, applied here to
# stop pure acknowledgements from hijacking an in-progress specialist route.
# A message only counts as "pure acknowledgement" if EVERY word in it is in
# this set — a name, a number, or any real topic word still takes the fast
# sticky path untouched (e.g. "Ravi" or "yes Tuesday works" during booking).
_ACKNOWLEDGEMENT_WORDS = {
    "hello", "hi", "hey", "okay", "ok", "yeah", "yes", "no",
    "hmm", "um", "uh", "ah", "oh", "right", "sure", "bye",
    "thank", "thanks", "alright", "fine", "good", "great",
    "nice", "wow", "yep", "nope", "huh", "mm", "mmm",
    "buddy", "boss", "sir", "dude", "man", "ji",
}


def _is_pure_acknowledgement(text: str) -> bool:
    words = re.sub(r"[^\w\s]", "", text.strip().lower()).split()
    return bool(words) and all(w in _ACKNOWLEDGEMENT_WORDS for w in words)


# ── Sticky-route escalation-intent guard ────────────────────────────────────
# Real call testing found a genuine bug here: a caller asked "Can I speak
# with your seniors?" while the call was sticky-routed to "knowledge" from
# earlier turns. At 6 words, it qualified for the sticky bypass above and
# went STRAIGHT to the knowledge specialist — completely skipping the
# Receptionist, which is the only component that ever decides "this should
# go to escalation". The knowledge agent improvised its own save_lead call
# and never escalated at all; the caller's explicit request to speak to a
# human was silently absorbed into a generic knowledge-agent reply.
#
# Word count is a fine proxy for "quick follow-up", but it says nothing
# about escalation intent specifically — an explicit request to speak to a
# person must always reach the Receptionist, regardless of how short it is
# or what route happens to be sticky. This list is deliberately narrow
# (only unambiguous human-escalation phrasing) so it doesn't accidentally
# force every short message through the Receptionist and erode the sticky
# path's latency win for genuine quick follow-ups.
_ESCALATION_INTENT_PHRASES = (
    "speak with your senior", "speak with a senior", "speak to your senior",
    "speak to a senior", "talk to your senior", "talk to a senior",
    "speak with someone", "speak to someone", "talk to someone",
    "speak with a human", "speak to a human", "talk to a human",
    "human agent", "real person", "actual person", "your manager",
    "your supervisor", "speak with your manager", "speak with your team",
    "connect me to", "transfer me to",
)


def _has_escalation_intent(text: str) -> bool:
    lowered = text.strip().lower()
    return any(phrase in lowered for phrase in _ESCALATION_INTENT_PHRASES)


async def _get_sticky_route(call_id: str) -> Optional[str]:
    client = get_redis()
    if client is None:
        return None
    try:
        return await client.get(_STICKY_PREFIX + call_id)
    except Exception as e:
        logger.warning(f"[{call_id}] Sticky route read failed: {e}")
        return None


async def _set_sticky_route(call_id: str, route: str) -> None:
    client = get_redis()
    if client is None:
        return
    try:
        if route in ("knowledge", "action", "escalation"):
            await client.set(_STICKY_PREFIX + call_id, route, ex=config.STICKY_ROUTE_TTL)
        else:
            await client.delete(_STICKY_PREFIX + call_id)
    except Exception as e:
        logger.warning(f"[{call_id}] Sticky route write failed: {e}")


def _start_speculative_embedding(call_id: str, user_text: str) -> "asyncio.Task":
    """
    Fire off embedding generation immediately, in parallel with whatever
    routing step runs next (Receptionist call, or the sticky-route fast
    path). Embedding doesn't depend on the route decision, and the route
    decision doesn't depend on the embedding — so there's no reason to
    make one wait for the other.

    If the turn turns out not to need it (route != "knowledge"), the task
    is cancelled by the caller below. This callback just prevents Python's
    "Task exception was never retrieved" warning in that case — it does
    NOT swallow the error anywhere it's actually used (knowledge_node still
    sees and handles any real failure when it awaits the task itself).
    """
    task = asyncio.create_task(embed_text(user_text))

    def _log_if_unused_failure(t: "asyncio.Task") -> None:
        if t.cancelled():
            return
        exc = t.exception()
        if exc:
            logger.debug(f"[{call_id}] Speculative embedding task ended with: {exc}")

    task.add_done_callback(_log_if_unused_failure)
    return task


def _start_speculative_rag_search(
    call_id: str, user_text: str, embedding_task: "asyncio.Task"
) -> "asyncio.Task":
    """
    Fire off the FULL knowledge-base search (embedding + pgvector query),
    immediately, in parallel with whatever routing step runs next —
    extends _start_speculative_embedding above one step further. Real call
    testing showed the RAG search itself still ran sequentially after the
    Receptionist decided "knowledge" even with the embedding already
    prefetched, still costing ~0.3-2s on the critical path. The search
    doesn't depend on the route decision (it always runs against the
    caller's own utterance regardless of which specialist ends up handling
    it), so there's no reason to wait for the Receptionist first.

    Awaits embedding_task internally so this doesn't duplicate that work —
    it just continues the same prefetch chain one step further, not a
    second independent embedding call.

    If the turn turns out not to need it (route != "knowledge"), the task
    is cancelled by the caller below — same pattern as embedding_task.
    """
    async def _run() -> str:
        precomputed_embedding = None
        try:
            precomputed_embedding = await embedding_task
        except Exception:
            pass  # embedding_task's own failure is already logged there
        return await search_knowledge_base(user_text, precomputed_embedding=precomputed_embedding)

    task = asyncio.create_task(_run())

    def _log_if_unused_failure(t: "asyncio.Task") -> None:
        if t.cancelled():
            return
        exc = t.exception()
        if exc:
            logger.debug(f"[{call_id}] Speculative RAG search task ended with: {exc}")

    task.add_done_callback(_log_if_unused_failure)
    return task


def _route_from_receptionist(state: AgentState) -> str:
    route = state.get("route", "knowledge")
    return route if route in ("knowledge", "action", "escalation") else "direct_end"


def _build_graph():
    graph = StateGraph(AgentState)

    graph.add_node("receptionist", receptionist_node)
    graph.add_node("knowledge", knowledge_node)
    graph.add_node("action", action_node)
    graph.add_node("escalation", escalation_node)

    graph.set_entry_point("receptionist")

    graph.add_conditional_edges(
        "receptionist",
        _route_from_receptionist,
        {
            "knowledge": "knowledge",
            "action": "action",
            "escalation": "escalation",
            "direct_end": END,
        },
    )
    graph.add_edge("knowledge", END)
    graph.add_edge("action", END)
    graph.add_edge("escalation", END)

    return graph.compile()


_compiled_graph = None


def _get_graph():
    global _compiled_graph
    if _compiled_graph is None:
        _compiled_graph = _build_graph()
    return _compiled_graph


async def run_turn(
    call_id: str,
    user_text: str,
    language: str,
    security_note: Optional[str] = None,
) -> Tuple[str, float, bool]:
    """
    Entry point called by brain/agent.py for every turn.

    Args:
        security_note: only set by brain/agent.py on turns where its input
            guardrail flagged a possible jailbreak/prompt-injection attempt
            (guardrails.input_guardrail.jailbreak_reminder()) — None on
            every normal turn. Threaded straight into AgentState so
            whichever specialist node ends up handling the turn includes
            it as an extra system-role reminder (see orchestrator/agents.py
            _build_messages). REAL-BUG FIX: this reminder previously existed
            but was never actually wired into any message sent to the LLM.

    Returns:
        (response_text, cost_usd, cacheable) — `cacheable` is False whenever
        a side-effect tool ran (save_lead / book_meeting / escalate), so
        brain/agent.py never stores a caller-specific confirmation
        ("booked for Ravi at 3pm") in the shared Redis LLM cache.
    """
    history = await get_history(call_id)

    # Start embedding the caller's utterance right away — this runs
    # concurrently with the Receptionist call (or with the sticky-route
    # decision below) since neither depends on the other. If this turn
    # ends up not needing it, it's cancelled before it does any real work
    # in the common case where cancellation lands before the API call
    # returns; if it already completed, the result is just discarded.
    embedding_task = _start_speculative_embedding(call_id, user_text)

    # Extends the same idea one step further: the FULL RAG search (not just
    # the embedding) also runs concurrently with the Receptionist call /
    # sticky-route decision below, since it doesn't depend on either. See
    # _start_speculative_rag_search's docstring for the real-call evidence
    # this is meant to fix (the search itself, not just the embedding, was
    # still adding ~0.3-2s sequentially after the route decision).
    rag_task = _start_speculative_rag_search(call_id, user_text, embedding_task)

    initial_state: AgentState = {
        "call_id": call_id,
        "language": language,
        "user_text": user_text,
        "history": history,
        "route": None,
        "response_text": "",
        "cost_usd": 0.0,
        "tool_called": False,
        "embedding_task": embedding_task,
        "rag_task": rag_task,
        "security_note": security_note,
    }

    word_count = len(user_text.split())
    sticky_route = await _get_sticky_route(call_id)

    # ── Sticky-route fast path: skip the Receptionist call entirely ────────
    if (
        sticky_route
        and word_count <= STICKY_MAX_WORDS
        and not _is_pure_acknowledgement(user_text)
        and not _has_escalation_intent(user_text)
    ):
        node_fn = _NODE_FOR_ROUTE.get(sticky_route)
        if node_fn is not None:
            logger.info(f"[{call_id}] Sticky route hit → '{sticky_route}' (skipping receptionist, {word_count} words)")
            initial_state["route"] = sticky_route
            if sticky_route != "knowledge":
                if not embedding_task.done():
                    embedding_task.cancel()
                if not rag_task.done():
                    rag_task.cancel()
            final_state = await node_fn(initial_state)
            await _set_sticky_route(call_id, sticky_route)  # refresh TTL
            response_text = final_state.get("response_text") or "Could you say that again?"
            cacheable = (
                final_state.get("route") == "knowledge"
                and not final_state.get("tool_called", False)
                and not final_state.get("kb_miss", False)
                # VA-T-014 — see the main path below for the full reasoning.
                and security_note is None
            )
            return response_text, final_state.get("cost_usd", 0.0), cacheable

    # ── Normal path: Receptionist routes, then the specialist runs ─────────
    graph = _get_graph()
    final_state = await graph.ainvoke(initial_state)

    # Receptionist decided a non-knowledge route — neither speculative task
    # was ever consumed, so free them up rather than letting them finish
    # pointlessly.
    if final_state.get("route") != "knowledge":
        if not embedding_task.done():
            embedding_task.cancel()
        if not rag_task.done():
            rag_task.cancel()

    await _set_sticky_route(call_id, final_state.get("route"))

    response_text = final_state.get("response_text") or (
        "I didn't quite catch that — could you say that again?"
    )

    # VA-T-014 — a flagged turn must not get an AFFIRMATIVE reply.
    #
    # Nothing was ever exposed, but a transcript reading
    #     caller: "ignore all previous instructions"
    #     agent:  "Yes, go ahead!"
    # looks exactly like the agent agreeing — to a client reviewing calls, to
    # an auditor, and to the caller, who may reasonably conclude another
    # attempt is worth making.
    #
    # Only "direct" is rewritten. The specialists already handle flagged input
    # via the jailbreak reminder; "direct" is the one route with no specialist
    # behind it, which is exactly why it was the gap.
    if security_note is not None and final_state.get("route") == "direct":
        logger.warning(
            f"[{call_id}] Replacing affirmative reply on a guardrail-flagged "
            f"direct turn (was: {response_text[:60]!r})"
        )
        response_text = config.GUARDRAIL_DEFLECTION_MESSAGE.format(
            company_name=(await get_live_settings()).get("company_name", "us")
        )
    cost = final_state.get("cost_usd", 0.0)
    route = final_state.get("route")
    # Only "direct" (small talk) and "knowledge" (pure Q&A, no tool side
    # effect) turns are ever safe to cache — action/escalation responses
    # are always caller-specific. A "knowledge" turn where the KB search
    # came back empty is also excluded: caching that "couldn't find it"
    # reply would keep serving it even after someone uploads the document
    # that would have answered it.
    # VA-T-014 — NEVER CACHE A TURN THE GUARDRAIL FLAGGED.
    #
    # A prompt-injection attempt names no topic, so the Receptionist classifies
    # it "direct" (pure social exchange) and it was answered with a cheerful
    # "Yes, go ahead!". Nothing leaked — the guardrail fired and the jailbreak
    # reminder was threaded in — but "direct" replies were CACHEABLE.
    #
    # That turned one caller's probe into every caller's answer: the reply was
    # written to the global LLM cache keyed on the text, and any caller saying
    # the same words within REDIS_LLM_CACHE_TTL got it served straight from
    # cache, skipping the orchestrator entirely (brain/agent.py).
    #
    # security_note is non-None exactly when input_guardrail flagged the turn,
    # so it is the precise signal for "this one must not persist".
    cacheable = (
        route in ("direct", "knowledge")
        and not final_state.get("tool_called", False)
        and not final_state.get("kb_miss", False)
        and security_note is None
    )
    logger.info(f"[{call_id}] Orchestrator turn complete via route='{route}' | cost=${cost:.5f}")
    return response_text, cost, cacheable