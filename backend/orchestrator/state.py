"""
orchestrator/state.py — shared state passed between LangGraph nodes.

Each node reads/writes this single TypedDict as it flows through the graph:
  Receptionist → (routes to one of) → Knowledge / Action / Escalation → END
"""
import asyncio
from typing import List, Dict, Literal, Optional, TypedDict


class AgentState(TypedDict, total=False):
    call_id: str
    language: str
    user_text: str
    history: List[Dict[str, str]]          # prior conversation turns

    route: Optional[Literal["knowledge", "action", "escalation", "direct"]]
    response_text: str                       # final text to speak to the caller
    cost_usd: float                          # accumulated OpenAI cost for this turn
    tool_called: bool                        # True if any tool ran a side effect this turn
                                              # (save_lead/book_meeting/escalate) — used to
                                              # decide if this turn is safe to LLM-cache

    # REAL-BUG FIX: brain/agent.py's input guardrail (guardrails/
    # input_guardrail.py) detects possible jailbreak/prompt-injection
    # attempts and builds a short corrective reminder for exactly this
    # case (guardrails.input_guardrail.jailbreak_reminder()) — but nothing
    # ever threaded that reminder into the actual messages sent to the
    # model, so a detected attempt was only ever logged, never actually
    # reinforced to the LLM. Set by brain/agent.py only on the turns where
    # guard_in.jailbreak_detected is True (the common case leaves this
    # None); orchestrator/agents.py's _build_messages() inserts it as an
    # extra system-role message, right after the specialist's own system
    # prompt, for whichever node ends up handling the turn.
    security_note: Optional[str]

    # Speculative embedding of user_text, kicked off in graph.run_turn() at
    # the same moment as the Receptionist call (they don't depend on each
    # other — the Receptionist doesn't need the embedding, and the embedding
    # doesn't need a route decision first). knowledge_node awaits this
    # instead of asking rag/search.py to compute it from scratch, saving
    # the ~0.7-1.4s embed_text() round trip on every knowledge turn.
    # Left uncomputed (None) is always a safe fallback — search.py embeds
    # normally if this is missing or fails.
    embedding_task: Optional["asyncio.Task"]

    # Speculative FULL knowledge-base search (embedding + pgvector query),
    # kicked off in graph.run_turn() at the same moment as the Receptionist
    # call — extends the embedding-prefetch idea above one step further.
    # Real call testing showed the RAG vector search itself (not just the
    # embedding) still ran sequentially after the Receptionist decided
    # "knowledge", adding another ~0.3-2s to the critical path even with the
    # embedding already prefetched. Since knowledge_node's search always
    # runs against user_text with no dependency on the route decision
    # either, there's no reason to wait for that decision first. If the
    # turn turns out not to need it, graph.run_turn() cancels this the same
    # way it already cancels embedding_task. Left uncomputed (None) is
    # always a safe fallback — knowledge_node searches normally if this is
    # missing, cancelled, or fails.
    rag_task: Optional["asyncio.Task"]