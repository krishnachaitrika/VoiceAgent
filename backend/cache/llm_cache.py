"""
cache/llm_cache.py — "Redis — LLM Cache" from the v3+ architecture diagram.

Caches the FINAL spoken response for a turn, keyed by the normalized caller
utterance, so an identical question asked by a different caller (or the
same caller twice) skips the GPT-4o call entirely.

SETTINGS FINGERPRINT — why this exists:
  The cached VALUE is a fully rendered response string. If it was generated
  while Agent Name was "Velu", that exact text stays cached and gets
  replayed verbatim to future callers — even after Agent Name is changed
  to "Rahul" in the Settings dashboard — until the cache entry's TTL
  happens to expire on its own. That's a real bug: a Settings change is
  supposed to take effect on the next call, but a cache hit was silently
  serving pre-change text instead of ever reaching the (now-updated)
  orchestrator prompts.

  Fixed by folding a short fingerprint of the current agent_name/
  company_name/system_prompt into the cache key itself. The moment any of
  those change, the fingerprint changes, so old cache entries simply stop
  matching (not deleted, just no longer reachable) and the very next
  matching question is treated as a fresh cache miss — genuinely dynamic,
  no manual cache-clearing step required.

  REAL-BUG FIX, PART 2: the fingerprint above only covered DB-level
  Settings-dashboard overrides. It did NOT cover the static prompt template
  baked into brain/prompts.py (DEFAULT_SYSTEM_PROMPT_GUIDANCE) — so a code
  fix to that file (e.g. the "greeting + real question wrongly answered as
  a connectivity check" fix) had zero effect on anything already cached
  under the old prompt, confirmed by a real call log showing the exact
  pre-fix wrong answer being served via cache hit AFTER the fix was
  deployed. _PROMPT_TEMPLATE_HASH below closes that gap the same way —
  hash the template content, fold it into the fingerprint, so any prompt
  edit in code invalidates old cache entries automatically too.

SAFETY — only cache turns with no side effects:
  We only cache a turn's response when GPT-4o made ZERO tool calls (a plain
  informational answer, e.g. "what services do you offer"). Turns that
  called save_lead / book_meeting / escalate are NEVER cached, because
  those respond with call-specific confirmations (a name, a phone number,
  a booking time) that must not be replayed to a different caller.
  brain/agent.py enforces this by only calling `set_cached` on the no-tool-
  call path.
"""
import hashlib
import logging
from typing import Optional

import config
from cache.redis_client import get_redis
from cache.kb_version import get_kb_version
from cache.settings_cache import get_live_settings
from brain.prompts import DEFAULT_SYSTEM_PROMPT_GUIDANCE

logger = logging.getLogger(__name__)

_LLM_CACHE_PREFIX = "llmcache:"

# REAL-BUG FIX (confirmed via real call log): the settings fingerprint below
# used to hash ONLY the DB-level Settings-dashboard overrides
# (agent_name/company_name/system_prompt). It did NOT account for the
# static prompt template baked into this codebase
# (brain/prompts.py:DEFAULT_SYSTEM_PROMPT_GUIDANCE) — so editing that file
# in code (e.g. fixing the "Hi hello, tell me about your company" ->
# wrongly answered "Yes, I can hear you" bug) had ZERO effect on any
# response already cached under the old prompt. A real call confirmed this:
# "LLM cache hit — skipping the orchestrator entirely" served the exact
# pre-fix wrong answer, even after the prompt fix was deployed, because the
# fingerprint hadn't changed. Hashing the template content here means ANY
# prompt-file edit changes the fingerprint automatically, which makes every
# previously-cached response unreachable (not deleted — just no longer
# matched), and the very next matching question is treated as a fresh
# cache miss against the current, correct prompt. No manual cache-clearing
# step required, same "genuinely dynamic" property the DB-level fingerprint
# already had for Settings-dashboard changes.
_PROMPT_TEMPLATE_HASH = hashlib.sha256(DEFAULT_SYSTEM_PROMPT_GUIDANCE.encode("utf-8")).hexdigest()[:12]


async def _settings_fingerprint() -> str:
    """
    Short hash of everything that can change what a cached response should
    say — the DB-level Settings-dashboard overrides (agent name, company
    name, custom system prompt guidance) AND the code-level prompt template
    (_PROMPT_TEMPLATE_HASH, see module docstring above for the real bug this
    closes). Deliberately does NOT include voice_provider/elevenlabs_voice_id
    — those affect audio synthesis, not the text being cached here, so
    changing them shouldn't needlessly invalidate perfectly good cached text.
    """
    live = await get_live_settings()
    # system_prompt is no longer part of this: it is code now, and any change
    # to it arrives as a new _PROMPT_TEMPLATE_HASH via a deploy — which already
    # invalidates every cached answer. Keeping the old key would have hashed a
    # value that is now always absent, adding nothing.
    #
    # VA-T-003: the knowledge-base version IS included, because it was the
    # missing piece. The fingerprint covered identity and prompts but nothing
    # about the documents, so correcting a fact and re-uploading left every
    # already-cached answer serving the OLD fact for up to REDIS_LLM_CACHE_TTL.
    kb_version = await get_kb_version()
    raw = (
        f"{live.get('agent_name', '')}|{live.get('company_name', '')}"
        f"|{_PROMPT_TEMPLATE_HASH}|{kb_version}"
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


def _cache_key(user_text: str, language: str, fingerprint: str) -> str:
    normalized = f"{fingerprint}:{language}:{user_text.strip().lower()}"
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    return f"{_LLM_CACHE_PREFIX}{digest}"


async def get_cached(user_text: str, language: str) -> Optional[str]:
    client = get_redis()
    if client is None:
        return None
    try:
        fingerprint = await _settings_fingerprint()
        return await client.get(_cache_key(user_text, language, fingerprint))
    except Exception as e:
        logger.warning(f"LLM cache read failed: {e}")
        return None


async def set_cached(user_text: str, language: str, response_text: str) -> None:
    client = get_redis()
    if client is None:
        return
    try:
        fingerprint = await _settings_fingerprint()
        await client.set(
            _cache_key(user_text, language, fingerprint), response_text, ex=config.REDIS_LLM_CACHE_TTL
        )
    except Exception as e:
        logger.warning(f"LLM cache write failed: {e}")