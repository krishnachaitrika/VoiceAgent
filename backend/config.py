import os
from dotenv import load_dotenv

load_dotenv()

OPENAI_API_KEY: str = os.getenv("OPENAI_API_KEY", "")
SARVAM_API_KEY: str = os.getenv("SARVAM_API_KEY", "")
ELEVENLABS_API_KEY: str = os.getenv("ELEVENLABS_API_KEY", "")
ELEVENLABS_VOICE_ID: str = os.getenv("ELEVENLABS_VOICE_ID", "")

TWILIO_ACCOUNT_SID: str = os.getenv("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN: str = os.getenv("TWILIO_AUTH_TOKEN", "")
TWILIO_PHONE_NUMBER: str = os.getenv("TWILIO_PHONE_NUMBER", "")

# STREAM_TOKEN_TTL_SECONDS — how long the signed token minted by
# api/twilio_webhook.py for the Media Stream URL stays valid (see
# twilio_auth.py). Twilio opens the WebSocket within milliseconds of
# receiving the TwiML response, so this only needs enough headroom for
# normal network latency, not a real user-facing wait.
STREAM_TOKEN_TTL_SECONDS: int = int(os.getenv("STREAM_TOKEN_TTL_SECONDS", 30))

# ── Dashboard API auth (VA-B1 fix) ──────────────────────────────────────────
# Every /api/dashboard, /api/settings, /api/documents and /api/analytics
# route used to have zero authentication — see auth.py. Two shared-secret
# tiers: DASHBOARD_API_KEY grants read access, DASHBOARD_ADMIN_KEY is
# additionally required for settings/knowledge-base writes. Left blank by
# default so a deployment that forgets to set these fails CLOSED (locks the
# dashboard out) rather than open.
DASHBOARD_API_KEY: str = os.getenv("DASHBOARD_API_KEY", "")
DASHBOARD_ADMIN_KEY: str = os.getenv("DASHBOARD_ADMIN_KEY", "")

# DATABASE_URL is the only thing that actually connects to Postgres (see
# database/base.py) — whether that Postgres is Supabase, RDS, or local
# doesn't matter to this codebase; there is no separate Supabase client.
DATABASE_URL: str = os.getenv("DATABASE_URL", "").strip().strip('"').strip("'")
# Supabase's "Connect" dialog hands out plain postgresql:// (or postgres://)
# URLs, but this app talks to Postgres through the async asyncpg driver, which
# SQLAlchemy only selects for postgresql+asyncpg://. Pasting the URL unchanged
# used to fail with a confusing "No module named 'psycopg2'", so the driver
# prefix is added here instead of relying on everyone remembering to edit it.
for _plain_prefix in ("postgresql://", "postgres://"):
    if DATABASE_URL.startswith(_plain_prefix):
        DATABASE_URL = "postgresql+asyncpg://" + DATABASE_URL[len(_plain_prefix):]
        break

NGROK_URL: str = os.getenv("NGROK_URL", "")

AGENT_NAME: str = os.getenv("AGENT_NAME", "Velu")
COMPANY_NAME: str = os.getenv("COMPANY_NAME", "Technozis")
COMPANY_DESCRIPTION: str = os.getenv(
    "COMPANY_DESCRIPTION", "an enterprise AI services company based in the United Kingdom"
)

# The very first thing the caller hears, before any GPT-4o call happens —
# read once at import time from .env, never hardcoded in voice/stream.py.
# {agent_name} and {company_name} are filled in from the two vars above, so
# changing AGENT_NAME/COMPANY_NAME in .env automatically updates the spoken
# greeting too, with zero code changes. Override GREETING_TEMPLATE directly
# in .env for full control over the wording.
GREETING_TEMPLATE: str = os.getenv(
    "GREETING_TEMPLATE",
    "Hello! Thank you for calling {company_name}. "
    "I'm {agent_name}, your AI assistant. "
    "How can I help you today? ",
)


async def get_greeting() -> str:
    """
    Render the greeting template with the LIVE agent/company name.

    Previously read AGENT_NAME/COMPANY_NAME straight from .env (static for
    the whole process lifetime), completely bypassing the live Settings
    system — so changing Agent Name in the dashboard had no effect on the
    one thing every single caller hears first: the opening greeting.

    Now pulls from cache/settings_cache.py, same as the orchestrator
    prompts and the LLM cache fingerprint, so a Settings change takes
    effect on the greeting too, not just mid-conversation answers.
    """
    from cache.settings_cache import get_live_settings  # local import: avoids a
    # circular import, since cache/settings_cache.py itself imports this
    # config module for its .env fallback defaults.
    live = await get_live_settings()
    return GREETING_TEMPLATE.format(
        agent_name=live.get("agent_name") or AGENT_NAME,
        company_name=live.get("company_name") or COMPANY_NAME,
    )


async def get_stt_fallback_message() -> str:
    """
    Render STT_FALLBACK_MESSAGE with the LIVE company/agent name, same
    reasoning as get_greeting() above — a Settings-page rebrand should
    reach every spoken line, including this rarely-hit failure path, not
    just the greeting and normal replies.
    """
    from cache.settings_cache import get_live_settings  # local import: same
    # circular-import reason as get_greeting() above.
    live = await get_live_settings()
    return STT_FALLBACK_MESSAGE.format(
        agent_name=live.get("agent_name") or AGENT_NAME,
        company_name=live.get("company_name") or COMPANY_NAME,
    )

GOOGLE_CALENDAR_ID: str = os.getenv("GOOGLE_CALENDAR_ID", "primary")
GOOGLE_CREDENTIALS_JSON: str = os.getenv("GOOGLE_CREDENTIALS_JSON", "credentials.json")
GOOGLE_CALENDAR_TIMEZONE: str = os.getenv("GOOGLE_CALENDAR_TIMEZONE", "Asia/Kolkata")

# ─── Meeting booking validation (VA-C7 fix) ────────────────────────────────
# book_meeting (brain/tools.py) used to trust whatever preferred_date/
# preferred_time the model produced with zero validation — nothing rejected
# a booking in the past, outside business hours, or years away, and a
# datetime that failed to even parse silently booked "now" instead of
# failing. The only defence was the model reading the current date
# correctly from its prompt, which has already failed once in a real call
# (a booking landed in 2023). These bound what "a valid meeting time" means
# in code, independent of what the model produces.
MEETING_MAX_DAYS_AHEAD: int = int(os.getenv("MEETING_MAX_DAYS_AHEAD", 60))
MEETING_BUSINESS_HOUR_START: int = int(os.getenv("MEETING_BUSINESS_HOUR_START", 9))   # 24h, inclusive
MEETING_BUSINESS_HOUR_END: int = int(os.getenv("MEETING_BUSINESS_HOUR_END", 18))      # 24h, exclusive

APP_VERSION: str = os.getenv("APP_VERSION", "3.0.0")

# ─── CORS ───────────────────────────────────────────────────────────────────
# REAL-BUG FIX: main.py previously used allow_origins=["*"] together with
# allow_credentials=True — this combination is invalid per the Fetch/CORS
# spec (a wildcard origin cannot be paired with credentialed requests), so
# browsers reject the actual credentialed request even though the server
# sends a 200. Enterprise dashboards frequently end up needing cookies/
# credentialed requests (session cookies once auth is added, SSE with
# credentials, etc.), so this is worth having a real allow-list for now
# rather than carrying a wildcard forward.
#
# CORS_ALLOWED_ORIGINS — comma-separated list of exact origins (scheme +
# host + port, no path, no trailing slash) allowed to call this API with
# credentials. Defaults to the local Next.js dev server and the Docker
# Compose frontend's published port. Add your real dashboard domain(s) here
# in production, e.g.:
#   CORS_ALLOWED_ORIGINS=https://dashboard.yourcompany.com,https://yourcompany.com
CORS_ALLOWED_ORIGINS: list[str] = [
    origin.strip().rstrip("/")
    for origin in os.getenv(
        "CORS_ALLOWED_ORIGINS", "http://localhost:3000,http://127.0.0.1:3000"
    ).split(",")
    if origin.strip()
]

# Model config
OPENAI_MODEL: str = "gpt-4o"
EMBEDDING_MODEL: str = "text-embedding-3-small"
EMBEDDING_DIM: int = 1536

# pgvector's `<=>` operator (used in rag/search.py) is cosine DISTANCE:
# 0.0 = identical, 1.0 = orthogonal/unrelated, up to 2.0 = opposite.
# A search whose best match's distance is above this threshold is treated
# as "nothing relevant found" rather than being handed to GPT-4o as
# context — otherwise a nonsense/garbled query still returns *some*
# nearest chunk (there's always a nearest one), and GPT-4o will confidently
# synthesize an answer from content that isn't actually related to what
# was asked, which reads to the caller as a made-up/wrong answer.
#
# CALIBRATION HISTORY (adjust this value, don't just trust either number):
#   - 0.45 (first guess, no real data) rejected a genuine, answerable
#     question — "what are the projects done in ServiceNow field" scored
#     0.583 and got wrongly treated as "not found" in production testing
#     on 2026-07-09.
#   - Raised to 0.7 as a safer default: false-rejecting a real question
#     (caller gets "I don't have that information" on something you DO
#     cover) is a worse failure than occasionally answering from a
#     loosely-related chunk, especially since the knowledge agent's prompt
#     already instructs it to only use what's actually in the provided
#     context.
# rag/search.py logs the actual top-match distance on every single
# search — keep watching `[search_kb] Top match distance: ...` in your
# logs. If an obviously-irrelevant/garbled query (like the earlier
# "Okay body now" case) starts getting confident answers again, note its
# logged distance and lower this value to just above genuine questions'
# distances but below that one.
# NOTE: the value and the full rationale now live in the "Retrieval confidence
# band" section at the end of this file — 0.70 rejected every legitimate
# paraphrase the QA round tested. Defined once, there.

# ─── RAG chunking — rag/ingest.py ──────────────────────────────────────────
# Previously hardcoded as CHUNK_SIZE/CHUNK_OVERLAP module constants in
# ingest.py. Moved here for the same reason as everything else above: lets
# you retune chunk size per knowledge-base content without a code change.
#
# RAG_CHUNK_SIZE — target chunk length in characters. Chunking is now
# structure-aware (splits on markdown headings/paragraphs first, see
# ingest.py chunk_text()), so this is a target ceiling per chunk, not a
# hard mid-sentence cut point like before.
#   - Smaller (e.g. 300): more precise retrieval, but a fact spanning
#     multiple sentences may get split across chunks
#   - Larger (e.g. 800): keeps more context together, but a chunk can drift
#     to cover more than one topic, diluting relevance scoring
RAG_CHUNK_SIZE: int = int(os.getenv("RAG_CHUNK_SIZE", 500))

# RAG_CHUNK_OVERLAP — characters of overlap between consecutive chunks, so
# a fact sitting right at a chunk boundary still appears whole in at least
# one chunk instead of being split with no shared context.
RAG_CHUNK_OVERLAP: int = int(os.getenv("RAG_CHUNK_OVERLAP", 50))

# ─── Voice Activity Detection (VAD) — voice/vad.py ─────────────────────────
# Previously hardcoded directly in vad.py, which meant tuning this required
# a code change + redeploy. Moved here so dev/staging/prod can each run a
# different value via .env with zero code changes — same pattern as
# RAG_RELEVANCE_THRESHOLD above.
#
# VAD_SILENCE_THRESHOLD_MS — how long the caller must be silent before the
# system decides they've finished speaking and sends audio to STT.
#   - 800ms:  fastest turnaround, but risks cutting off speakers mid-sentence
#             during natural thinking pauses
#   - 1000ms: tried this as the default for speed — real call evidence
#             showed it was too aggressive: a caller pausing briefly
#             mid-thought ("Yeah, can you... [pause]... explore the ITSM
#             services") got split into two separate turns, producing a
#             nonsense fragment as its own question, a confusing agent
#             reply, and MORE total latency overall (two full round trips
#             instead of one) — the opposite of the intended goal
#   - 1300ms: (current default) middle ground — faster than the original
#             1500ms, with enough headroom to not clip a normal
#             mid-sentence pause. Chosen after the 1000ms regression above.
#   - 1500ms: safest for Indian English pacing and network jitter, but
#             caller feels a longer pause after they stop talking
# Watch actual call transcripts after changing this, not just the latency
# numbers — a "faster" turn that clipped a sentence isn't actually better
# for the caller.
VAD_SILENCE_THRESHOLD_MS: int = int(os.getenv("VAD_SILENCE_THRESHOLD_MS", 1300))

# VAD_SILENCE_RMS_THRESHOLD — energy level (RMS) above which audio counts as
# speech rather than line noise. Phone line noise/static typically sits
# ~150-400, quiet room background ~300-600, normal speech ~800-4000.
#   - Lower this (e.g. 650) if quiet callers are getting dropped/missed
#   - Raise this (e.g. 1000) if background noise is triggering false turns
VAD_SILENCE_RMS_THRESHOLD: int = int(os.getenv("VAD_SILENCE_RMS_THRESHOLD", 800))

# ─── HTTP connection pool sizing — voice/tts.py, voice/stt.py ──────────────
# Shared connection pool limits for calls to Sarvam/ElevenLabs. See the
# pooling explanation in voice/tts.py and voice/stt.py for what these do.
#   - HTTP_POOL_KEEPALIVE_CONNECTIONS: connections kept warm/open even when
#     idle, ready for instant reuse — this is the "always open" pool size
#   - HTTP_POOL_MAX_CONNECTIONS: hard ceiling including temporary overflow
#     connections opened during a burst of simultaneous calls
# 20/50 is generous headroom for current call volume. Only raise these if
# logs show requests actually queuing under real simultaneous-call load —
# AND check Sarvam's/ElevenLabs' own account-level concurrency limits first,
# since raising these numbers does nothing if their API rejects the extra
# concurrent requests on their end.
HTTP_POOL_KEEPALIVE_CONNECTIONS: int = int(os.getenv("HTTP_POOL_KEEPALIVE_CONNECTIONS", 20))
HTTP_POOL_MAX_CONNECTIONS: int = int(os.getenv("HTTP_POOL_MAX_CONNECTIONS", 50))

# ─── Concurrency and rate limiting (VA-B3 fix) ─────────────────────────────
# Nothing used to bound concurrent calls or requests per client — with
# database/base.py's pool_size=5/max_overflow=10, roughly fifteen
# simultaneous calls exhausted the DB pool and every turn after that started
# failing on database access. MAX_CONCURRENT_CALLS caps the /stream
# WebSocket explicitly (api/websocket.py) at connection time — anything over
# the cap is rejected before a Call row or any provider cost is incurred.
# The DB pool below is sized to comfortably outlive that cap (each call
# holds at most one session at a time), and the HTTP rate limits bound the
# dashboard/API surface, which has no natural cap of its own the way calls
# do.
MAX_CONCURRENT_CALLS: int = int(os.getenv("MAX_CONCURRENT_CALLS", 20))
RATE_LIMIT_DEFAULT: str = os.getenv("RATE_LIMIT_DEFAULT", "60/minute")
RATE_LIMIT_TWILIO_WEBHOOK: str = os.getenv("RATE_LIMIT_TWILIO_WEBHOOK", "30/minute")

# Graceful shutdown (VA-C6 fix) — how long main.py's lifespan shutdown
# waits for in-flight calls (api/websocket.py's wait_for_drain) to finish
# naturally before giving up and letting the process exit anyway. Should
# stay comfortably BELOW k8s/04-backend-deployment.yaml's
# terminationGracePeriodSeconds, which is the hard kill deadline — this
# number is the soft "try to wait" budget, that one is "give up entirely".
GRACEFUL_SHUTDOWN_DRAIN_SEC: float = float(os.getenv("GRACEFUL_SHUTDOWN_DRAIN_SEC", 30.0))

# ─── Live settings cache — cache/settings_cache.py ─────────────────────────
# The Settings dashboard writes agent_name/company_name/voice_provider/
# elevenlabs_voice_id/system_prompt into the `settings` DB table. Reading
# that table on every single message would mean an extra DB round trip per
# turn — so live settings are cached in memory for this many seconds, then
# re-read. The Settings page also explicitly invalidates this cache the
# moment you hit Save (see api/settings.py), so changes take effect on the
# very next call regardless of this TTL — this number only bounds the
# worst case for a call that started just before you saved.
SETTINGS_CACHE_TTL_SECONDS: int = int(os.getenv("SETTINGS_CACHE_TTL_SECONDS", 15))

# ─── Data retention (VA-B5 fix) ─────────────────────────────────────────────
# Calls, transcripts and leads used to be kept forever with no deletion
# path at all. This is the retention window scripts/purge_old_data.py
# enforces — run it on a schedule (e.g. a daily k8s CronJob) to actually
# apply it; setting this alone does nothing on its own. The right value is
# a business/legal decision (the company is UK-based, so UK GDPR applies)
# — 365 is a placeholder, not a recommendation.
DATA_RETENTION_DAYS: int = int(os.getenv("DATA_RETENTION_DAYS", 365))

# ─── Speech provider cost metering (VA-C2 fix) ─────────────────────────────
# calls.sarvam_cost (see database/models.py) was declared but never
# incremented anywhere — every call's dashboard cost figure reflected
# OpenAI tokens only, silently omitting speech entirely (likely the larger
# share of the actual bill). These per-unit rates are what
# voice/tts.py / api/websocket.py multiply by characters synthesized /
# call duration to populate that column for real. They default to 0.0
# because this codebase can't know your actual negotiated rate — fill
# these in from your ElevenLabs/Sarvam billing dashboard, or the cost
# figure is back to being silently wrong, just now for a different reason.
ELEVENLABS_TTS_COST_PER_1K_CHARS: float = float(os.getenv("ELEVENLABS_TTS_COST_PER_1K_CHARS", 0.0))
SARVAM_TTS_COST_PER_1K_CHARS: float = float(os.getenv("SARVAM_TTS_COST_PER_1K_CHARS", 0.0))
ELEVENLABS_STT_COST_PER_MINUTE: float = float(os.getenv("ELEVENLABS_STT_COST_PER_MINUTE", 0.0))

# ─── Twilio voice minutes ──────────────────────────────────────────────────
# The last untracked provider. Twilio bills inbound voice PER MINUTE and
# ROUNDS UP, so a 57-second call costs a full minute — modelled that way in
# api/websocket.py rather than pro-rated, otherwise short calls look about
# half price against the actual invoice.
#
# Default 0.0 for the same reason as the speech rates: a wrong number is worse
# than an obvious zero. Set it from your Twilio invoice — the rate varies by
# the country of the number, not by the caller.
TWILIO_INBOUND_COST_PER_MINUTE: float = float(os.getenv("TWILIO_INBOUND_COST_PER_MINUTE", 0.0))

# Outbound is priced COMPLETELY differently and the gap is large: inbound to a
# US number is ~$0.0085/min, outbound to an Indian mobile is ~$0.10-0.15/min.
# Ten to fifteen times more.
#
# Applying the inbound rate to an outbound call would under-report by ~93% —
# worse than not tracking at all, because the figure still looks credible.
# api/websocket.py picks the rate from the call's recorded direction.
#
# Outbound rates vary by DESTINATION country, so if you ever dial more than one
# country this single value stops being sufficient and needs a per-prefix rate
# table. Fine for a single market; noted so nobody is surprised later.
TWILIO_OUTBOUND_COST_PER_MINUTE: float = float(os.getenv("TWILIO_OUTBOUND_COST_PER_MINUTE", 0.0))

# ─── Twilio price reconciliation (billing/twilio_reconcile.py) ─────────────
# The two rates above are ESTIMATES — only as accurate as whoever typed them,
# valid for one destination country, and silently stale the day Twilio changes
# pricing. Twilio's Call resource exposes the amount it actually charged, so
# after each call the estimate is replaced with the real figure and the row is
# marked twilio_cost_source='actual'.
#
# The rates stay as the immediate placeholder (the price is not rated the
# instant a call ends) and as the fallback when reconciliation cannot complete.
TWILIO_RECONCILE_PRICE: bool = os.getenv("TWILIO_RECONCILE_PRICE", "true").lower() == "true"
# Twilio usually rates a call within a few seconds of it ending.
TWILIO_RECONCILE_DELAY_SEC: float = float(os.getenv("TWILIO_RECONCILE_DELAY_SEC", 20.0))
# Total wait is DELAY x MAX_ATTEMPTS. Past that the estimate stands and the row
# stays marked 'estimated', so an unreconciled call is visible rather than
# silently passing as measured.
TWILIO_RECONCILE_MAX_ATTEMPTS: int = int(os.getenv("TWILIO_RECONCILE_MAX_ATTEMPTS", 6))

# Cost per 1K tokens (USD) — GPT-4o
COST_INPUT_PER_1K: float = 0.005
COST_OUTPUT_PER_1K: float = 0.015

# ─── Redis (v3+ enterprise layer) ──────────────────────────────────────────
# Session memory, LLM/RAG cache, and the Redis Streams event bus all share
# this one connection string. Defaults to the docker-compose service name
# so `docker compose up` works with zero config; override for a managed
# Redis (Azure Cache / AWS ElastiCache / Upstash) in production.
REDIS_URL: str = os.getenv("REDIS_URL", "redis://localhost:6379/0")
REDIS_ENABLED: bool = os.getenv("REDIS_ENABLED", "true").lower() == "true"

# TTLs (seconds)
REDIS_SESSION_TTL: int = int(os.getenv("REDIS_SESSION_TTL", 3600))       # 1 hour — last N turns per call
REDIS_LLM_CACHE_TTL: int = int(os.getenv("REDIS_LLM_CACHE_TTL", 3600))   # 1 hour — identical Q&A cache
REDIS_RAG_CACHE_TTL: int = int(os.getenv("REDIS_RAG_CACHE_TTL", 86400))  # 24 hours — pgvector result cache

# ─── Feature flags — enterprise layer ──────────────────────────────────────
# NOTE: there is no ENABLE_LANGGRAPH_ORCHESTRATOR flag — the LangGraph
# orchestrator (orchestrator/) is the only agent brain in this codebase.
# The earlier single-call GPT-4o path was removed entirely rather than kept
# as a toggle, so there's exactly one place tool-calling logic lives.
ENABLE_GUARDRAILS: bool = os.getenv("ENABLE_GUARDRAILS", "true").lower() == "true"
ENABLE_ROLLING_MEMORY: bool = os.getenv("ENABLE_ROLLING_MEMORY", "true").lower() == "true"
ENABLE_SENTIMENT_ANALYTICS: bool = os.getenv("ENABLE_SENTIMENT_ANALYTICS", "true").lower() == "true"
ENABLE_EVENT_BUS: bool = os.getenv("ENABLE_EVENT_BUS", "true").lower() == "true"

# Rolling summary memory (v2): once history exceeds this many turns, older
# turns are collapsed into one GPT-4o-generated summary message instead of
# being dropped, so long calls don't lose earlier context.
ROLLING_MEMORY_MAX_TURNS: int = int(os.getenv("ROLLING_MEMORY_MAX_TURNS", 10))
ROLLING_MEMORY_SUMMARY_MODEL: str = os.getenv("ROLLING_MEMORY_SUMMARY_MODEL", "gpt-4o-mini")

# Sentiment / post-call analytics model (cheaper model is fine — runs once
# per call, after the call ends, off the critical latency path)
SENTIMENT_MODEL: str = os.getenv("SENTIMENT_MODEL", "gpt-4o-mini")

# ─── LangGraph orchestrator speed tuning (v3+) ─────────────────────────────
# The Receptionist only classifies intent — a cheap/fast model is plenty
# accurate for that and cuts router latency ~3-5x vs full GPT-4o.
ORCHESTRATOR_ROUTER_MODEL: str = os.getenv("ORCHESTRATOR_ROUTER_MODEL", "gpt-4o-mini")

# Sticky routing: once a call is mid-flow with one specialist agent (e.g.
# mid-booking with Action), short follow-up replies skip the Receptionist
# call entirely and go straight back to that same agent. TTL is short so a
# caller who changes topic after finishing a flow still gets re-routed.
STICKY_ROUTE_TTL: int = int(os.getenv("STICKY_ROUTE_TTL", 120))

# ─── v4 — Streaming voice pipeline (Sarvam WebSocket STT/TTS) ─────────────
# Phase 1 of the latency rework: replace the batch/REST Sarvam calls in
# voice/stt.py and voice/tts.py (buffer the whole utterance client-side,
# then one POST after a fixed silence window / one POST per full sentence)
# with Sarvam's WebSocket streaming APIs — continuous partial transcripts +
# server-side VAD on the way in, progressive audio-as-it's-synthesized on
# the way out.
#
# Both flags default to ON now that the streaming path has been the
# production default for a while — override to "false" per-environment
# only if you need to roll back to the old batch/REST path.
#   ENABLE_STREAMING_STT=true   → voice/stream.py uses StreamingSTTSession
#   ENABLE_STREAMING_TTS=true   → voice/stream.py uses StreamingTTSSession
# See voice/stt.py / voice/tts.py for the session classes themselves, and
# voice/stream.py for how handle_twilio_stream branches on these flags.
ENABLE_STREAMING_STT: bool = os.getenv("ENABLE_STREAMING_STT", "true").lower() == "true"
ENABLE_STREAMING_TTS: bool = os.getenv("ENABLE_STREAMING_TTS", "true").lower() == "true"

# ── Streaming + batch STT — ElevenLabs Scribe v2 (voice/stt.py) ────────────
# STT was fully migrated off Sarvam to ElevenLabs Scribe v2 / Scribe v2
# Realtime — Sarvam is no longer used anywhere in voice/stt.py. Sarvam TTS
# remains as the default/fallback voice on the TTS side only (see the
# Streaming TTS section below and voice/tts.py) — this is an input-side-only
# swap, nothing else in the pipeline changed.
#
# ELEVENLABS_STT_MODEL — batch endpoint model_id. "scribe_v2" is ElevenLabs'
# current general-purpose STT model as of this writing; kept as a .env
# setting (not hardcoded) so it can be bumped the moment a newer Scribe
# version ships, with zero code change.
ELEVENLABS_STT_MODEL: str = os.getenv("ELEVENLABS_STT_MODEL", "scribe_v2")
# ELEVENLABS_STT_REALTIME_MODEL — streaming/WebSocket model_id, kept separate
# from the batch model above since ElevenLabs versions batch vs realtime
# Scribe independently.
ELEVENLABS_STT_REALTIME_MODEL: str = os.getenv("ELEVENLABS_STT_REALTIME_MODEL", "scribe_v2_realtime")
# ELEVENLABS_STT_AUDIO_FORMAT — REAL-BUG FIX: this used to be two separate
# settings, ELEVENLABS_STT_ENCODING ("pcm_s16le") and
# ELEVENLABS_STT_SAMPLE_RATE (8000), sent as two separate query params
# (encoding=... & sample_rate=...). Verified against ElevenLabs' actual
# published API reference (elevenlabs.io/docs/api-reference/speech-to-text/
# v-1-speech-to-text-realtime): there is no "encoding" or "sample_rate"
# query param on this endpoint at all — the real, only parameter is
# "audio_format", one combined string, with a fixed set of accepted values:
# pcm_8000, pcm_16000, pcm_22050, pcm_24000, pcm_44100, pcm_48000,
# ulaw_8000. Sending the wrong/unrecognized param names meant ElevenLabs
# silently ignored them and used its own default sample rate — a real
# sample-rate mismatch against the 8kHz audio actually being sent, which is
# what caused both the garbled-language transcripts (corrupted-sounding
# audio) and, after the separate mulaw_to_pcm decode bug was fixed, the
# complete silence/empty-transcript bug (correctly-decoded audio, still
# being interpreted at the wrong rate).
#
# "ulaw_8000" is now the default — this accepts Twilio's native 8kHz mulaw
# bytes directly with ZERO conversion needed on our side (see
# voice/stream.py's streaming STT send call), which is both simpler and one
# fewer place for a hand-rolled/library codec bug to hide. The batch STT
# path (transcribe_audio) is unaffected by this — it POSTs a WAV file to a
# different endpoint that has never had this query-param issue.
ELEVENLABS_STT_AUDIO_FORMAT: str = os.getenv("ELEVENLABS_STT_AUDIO_FORMAT", "ulaw_8000")
# Kept for the client-side send-buffering byte-size math in voice/stt.py
# (StreamingSTTSession._send_chunk_bytes) — NOT sent to ElevenLabs as a
# query param any more (see ELEVENLABS_STT_AUDIO_FORMAT above).
ELEVENLABS_STT_SAMPLE_RATE: int = int(os.getenv("ELEVENLABS_STT_SAMPLE_RATE", 8000))
# ELEVENLABS_STT_COMMIT_STRATEGY — "vad" lets ElevenLabs' own server-side
# voice-activity detection decide when an utterance is finished and emit a
# committed_transcript (mirrors Sarvam's old vad_signals="true" behaviour).
# The alternative, "manual", requires this codebase to send an explicit
# commit:true flag on an audio chunk instead — only switch to that if VAD
# auto-commit proves too aggressive/lax in real call testing, same
# "watch the transcripts" discipline as VAD_SILENCE_THRESHOLD_MS above.
ELEVENLABS_STT_COMMIT_STRATEGY: str = os.getenv("ELEVENLABS_STT_COMMIT_STRATEGY", "vad")
# ── VAD fine-tuning — REAL ENTERPRISE GAP FIX (2026-08-01) ─────────────────
# Real call testing found phone numbers getting captured with a digit
# missing/split across multiple attempts (confirm_booking_details rejecting
# 9-digit captures 2-3 times before succeeding). Root cause: callers
# naturally pause between digit groups when reading out a phone number
# ("636... 996... 595"), and with zero VAD tuning exposed here, ElevenLabs'
# own default silence threshold was the only thing deciding when an
# utterance is "done" — a mid-number pause was enough to trigger an early
# commit, splitting the number.
#
# These four map directly to ElevenLabs' documented realtime STT query
# params (elevenlabs.io/docs/api-reference/speech-to-text/
# v-1-speech-to-text-realtime). Same discipline as the old
# SARVAM_STT_POSITIVE_SPEECH_THRESHOLD-style overrides: all optional/blank
# by default so ElevenLabs' own default applies, and only worth setting
# once real call data says which specific boundary needs adjusting — do NOT
# guess these upfront. vad_silence_threshold_secs is the one most directly
# relevant to the phone-number-splitting symptom above; a slightly higher
# value (e.g. "1.0"–"1.5") gives callers more room to pause between digit
# groups before the segment auto-commits. Kept as strings (not floats) so
# "blank" is a clean, distinguishable default rather than 0.0 meaning
# something.
ELEVENLABS_STT_VAD_THRESHOLD: str = os.getenv("ELEVENLABS_STT_VAD_THRESHOLD", "")
ELEVENLABS_STT_VAD_SILENCE_THRESHOLD_SECS: str = os.getenv("ELEVENLABS_STT_VAD_SILENCE_THRESHOLD_SECS", "1.0")
ELEVENLABS_STT_MIN_SPEECH_DURATION_MS: str = os.getenv("ELEVENLABS_STT_MIN_SPEECH_DURATION_MS", "")
ELEVENLABS_STT_MIN_SILENCE_DURATION_MS: str = os.getenv("ELEVENLABS_STT_MIN_SILENCE_DURATION_MS", "")
# English-only build: language is forced to "en" directly in voice/stt.py
# (both the batch transcribe_audio() call and the streaming
# StreamingSTTSession.connect() query params) rather than being read from
# a .env-configurable setting — there's no other language to fall back to
# or narrow detection across any more, so no setting is exposed here.

# VERIFIED (2026-08-01, against ElevenLabs' live published API reference at
# elevenlabs.io/docs/api-reference/speech-to-text/v-1-speech-to-text-realtime
# — this replaces an earlier unverified CONFIDENCE NOTE that turned out to
# have the wrong query param names, see ELEVENLABS_STT_AUDIO_FORMAT above
# for the real bug that caused): the realtime WebSocket URL/query-param
# shape is wss://api.elevenlabs.io/v1/speech-to-text/realtime?model_id=...
# &audio_format=...&commit_strategy=...&language_code=... — auth via
# xi-api-key header (or a token query param for client-side use, not used
# here). The input_audio_chunk / partial_transcript / final_transcript /
# committed_transcript / session_started / rate_limited message_type values
# are all correct per the reference. ElevenLabs' realtime protocol does not
# emit separate START_SPEECH/END_SPEECH VAD signal events the way Sarvam
# did — voice/stt.py approximates on_speech_start on the first
# partial_transcript of an utterance and on_speech_end on each
# committed_transcript; this part is still an approximation, not confirmed
# against the reference (the reference doesn't document an equivalent
# explicit event), so keep watching real call logs for it.

# ELEVENLABS_STT_SEND_CHUNK_MS — REAL-BUG FIX (confirmed via a real call):
# 200ms batching (5 input_audio_chunk messages/sec, sustained for the
# duration of a live call) was enough to eventually trigger ElevenLabs'
# own server-side rate limiter — a real call got this exact error and the
# session was forcibly closed:
#   {"message_type": "queue_overflow", "error": "Session terminated: audio
#   data is being sent too frequently. Please reduce the rate at which you
#   send audio chunks."}
# This isn't a burst/spike issue — it built up gradually over roughly 40
# seconds of otherwise normal, steady real-time audio, meaning ElevenLabs'
# ingestion queue was draining slightly slower than our steady send rate
# and eventually overflowed. The reconnect logic (STT_RECONNECT_*, see
# above) correctly recovered the session when this happened, but the real
# fix is not sending at a rate that risks tripping this in the first
# place. Raised from 200ms to 500ms — cuts the message rate from 5/sec to
# 2/sec (same total audio throughput, just fewer/larger messages), which
# gives ElevenLabs' queue meaningfully more headroom, at the cost of
# ~300ms more client-side buffering latency before each chunk is sent.
# If queue_overflow recurs even at this rate, raise further from here
# using real call evidence, not guesswork.
ELEVENLABS_STT_SEND_CHUNK_MS: int = int(os.getenv("ELEVENLABS_STT_SEND_CHUNK_MS", 500))

# ── Streaming STT resilience (voice/stt.py + voice/stream.py) ──────────────
# Kept provider-agnostic (renamed off "Sarvam" in the comments only — the
# setting names below were already generic) since any third-party STT
# WebSocket can drop mid-call. Goal is the same: survive a transient
# provider-side failure by reopening the session a bounded number of times
# with backoff, and only fall back to a spoken apology + escalation once
# genuinely out of options — never leave the caller listening to silence.
#
# STT_RECONNECT_MAX_ATTEMPTS — how many times to try reopening the streaming
# STT socket after an unexpected mid-call close, before giving up on this
# call and falling back to the spoken-apology + escalate path.
STT_RECONNECT_MAX_ATTEMPTS: int = int(os.getenv("STT_RECONNECT_MAX_ATTEMPTS", 3))
# STT_RECONNECT_BACKOFF_BASE_MS / _MAX_MS — exponential backoff between
# reconnect attempts (base * 2^attempt, capped at max), so a brief provider
# blip gets a quick retry while a real outage doesn't hammer their API.
STT_RECONNECT_BACKOFF_BASE_MS: int = int(os.getenv("STT_RECONNECT_BACKOFF_BASE_MS", 500))
STT_RECONNECT_BACKOFF_MAX_MS: int = int(os.getenv("STT_RECONNECT_BACKOFF_MAX_MS", 4000))
# STT_FALLBACK_MESSAGE — spoken to the caller if every reconnect attempt
# fails and the call can no longer hear them. {agent_name} / {company_name}
# are filled in from live settings at speak-time, same as GREETING_TEMPLATE
# above, so this never needs a code change if either is rebranded.
STT_FALLBACK_MESSAGE: str = os.getenv(
    "STT_FALLBACK_MESSAGE",
    "I'm sorry, I'm having trouble hearing you right now due to a technical issue. "
    "Someone from the {company_name} team will call you back shortly. Thank you for your patience.",
)

# ── LLM call resilience (VA-C1 fix) ─────────────────────────────────────────
# Every other external dependency (STT above, TTS's
# ELEVENLABS_TTS_AUDIO_IDLE_TIMEOUT_SEC) already has a bounded wait — the
# GPT-4o completion/embedding calls on orchestrator/agents.py,
# rag/embedder.py and analytics/sentiment.py did not, so a slow or hanging
# OpenAI response left a live caller in silence with no fallback and no
# escalation, until Twilio eventually tore the call down on its own.
#
# Set at the client level (services/providers.py's get_openai_client)
# rather than per call site, so every current AND future call through that
# one client is covered automatically — OPENAI_REQUEST_TIMEOUT_SEC bounds
# each individual attempt, OPENAI_MAX_RETRIES is the SDK's own bounded
# retry on a retryable failure (timeout/connection/5xx) before it gives up
# and raises, at which point brain/agent.py.process_turn's fallback below
# takes over — mirroring the STT path's reconnect-then-fallback shape.
OPENAI_REQUEST_TIMEOUT_SEC: float = float(os.getenv("OPENAI_REQUEST_TIMEOUT_SEC", 8.0))
OPENAI_MAX_RETRIES: int = int(os.getenv("OPENAI_MAX_RETRIES", 1))
# LLM_FALLBACK_MESSAGE — spoken to the caller (and used as the escalation
# reason) when the LLM call still fails after every retry above.
LLM_FALLBACK_MESSAGE: str = os.getenv(
    "LLM_FALLBACK_MESSAGE",
    "I'm having a technical issue right now. Let me get someone from our team to call you back.",
)

# English-only build: the LANGUAGE_SWITCH_* hysteresis settings (confidence
# threshold, minimum word count, confirm-streak count) that used to gate
# whether a mid-call language detection was trusted enough to flip the
# call's reply/TTS language have been removed along with the switching
# logic itself (see voice/stream.py) — there's only one language now, so
# nothing to switch between or guard against misdetecting.

# ── Smart hearing (config.ENABLE_SMART_HEARING) ─────────────────────────────
# Sarvam's VAD only knows about acoustic silence — it has no idea whether a
# sentence sounds grammatically finished. Real call testing repeatedly
# showed one continuous thought getting chopped into 2-3 separate
# END_SPEECH-triggered transcripts on natural pauses (e.g. "Do you have any
# specific projects in" / "Service No" / "What are the projects done in?"
# from one uninterrupted question) — each treated as its own turn, each
# running a full wasted RAG/GPT cycle before being superseded.
#
# Smart hearing adds a completeness check on top of Sarvam's VAD: before
# treating a finalized transcript as a real turn, check whether it *sounds*
# unfinished (trails on a conjunction/preposition, no terminal punctuation,
# or is very short and grammatically dangling). If it does, hold it and
# merge it with whatever Sarvam sends next — which arrives naturally the
# moment the caller keeps talking — instead of firing RAG/GPT on a half
# sentence. A grace-period timer (not "wait forever") finalizes the held
# fragment on its own if the caller doesn't continue.
ENABLE_SMART_HEARING: bool = os.getenv("ENABLE_SMART_HEARING", "true").lower() == "true"

# SMART_HEARING_GRACE_MS — how long to hold an incomplete-sounding fragment
# before finalizing it anyway (as-is) if no continuation arrives. This is
# the direct implementation of the original "+700ms grace period instead of
# cutting immediately" idea — it only ever adds delay on fragments that
# already looked unfinished, never on a complete quick answer.
SMART_HEARING_GRACE_MS: int = int(os.getenv("SMART_HEARING_GRACE_MS", 700))

# SMART_HEARING_MAX_HOLD_MS — hard ceiling on total hold time across
# multiple merges, so a caller who keeps trailing off (or goes quiet
# mid-thought for good) can't hold up the turn indefinitely. Once this
# elapses, whatever's been accumulated is sent through as-is even if it
# still looks incomplete — same "safety net over perfection" principle as
# the other timeouts in this file.
SMART_HEARING_MAX_HOLD_MS: int = int(os.getenv("SMART_HEARING_MAX_HOLD_MS", 3000))

# ── Multi-part turn merging (adaptive) ──────────────────────────────────────
# Smart hearing (above) only holds a fragment when it *sounds* grammatically
# unfinished. It does nothing for a caller who says two or more COMPLETE
# thoughts back-to-back ("What are your working hours?" <pause> "Also, do
# you have parking?") — each is valid on its own, so each used to fire its
# own full RAG/GPT/TTS turn immediately, giving two separate replies instead
# of one natural, combined answer.
#
# Rather than always holding every finalized fragment "just in case" (which
# would add latency to every single normal turn, even callers who only ever
# say one thing at a time), this is adaptive: we watch for real evidence,
# per call, that THIS caller talks in quick multi-part bursts, and only then
# start giving future fragments a brief grace window before answering.
# Evidence is either signal below (whichever happens first):
#   1. The caller starts a new utterance suspiciously soon after the
#      previous turn was dispatched (MULTI_PART_QUICK_SUCCESSION_MS) —
#      i.e. they were already talking again before the agent even started
#      replying to the first thing.
#   2. The caller barges in on the agent's reply at all. Interrupting to add
#      something is itself direct proof of multi-part intent, since we're
#      already forced to stop and listen anyway.
# Once MULTI_PART_TRIGGER_COUNT such signals are observed, adaptive mode
# turns on for the REST of that call (sticky) — it does not reset per turn.
ENABLE_MULTI_PART_MERGE: bool = os.getenv("ENABLE_MULTI_PART_MERGE", "true").lower() == "true"

# MULTI_PART_QUICK_SUCCESSION_MS — max gap, in ms, between dispatching one
# turn and the caller starting to speak again, for that gap to count as
# "quick succession" evidence of multi-part speech (signal 1 above).
MULTI_PART_QUICK_SUCCESSION_MS: int = int(os.getenv("MULTI_PART_QUICK_SUCCESSION_MS", 1500))

# MULTI_PART_TRIGGER_COUNT — number of quick-succession/barge-in signals
# required before adaptive holding switches on for the rest of the call.
# Default 1: the very first sign of multi-part behaviour is enough to start
# protecting the rest of that call's turns.
MULTI_PART_TRIGGER_COUNT: int = int(os.getenv("MULTI_PART_TRIGGER_COUNT", 1))

# MULTI_PART_HOLD_GRACE_MS — once adaptive mode is on, how long to hold a
# fragment that already sounds grammatically COMPLETE, in case the caller is
# mid-way through listing several points. Slightly longer than
# SMART_HEARING_GRACE_MS because we're being more speculative here (the
# fragment isn't dangling — we're purely betting on the caller's observed
# habit of multi-part bursts).
MULTI_PART_HOLD_GRACE_MS: int = int(os.getenv("MULTI_PART_HOLD_GRACE_MS", 900))

# TURN_PREEMPT_GRACE_MS — REAL-BUG FIX (confirmed via real call logs): this
# used to only protect a turn that had ALREADY finished generating its
# answer. A second question arriving while the first was still inside
# process_turn() (GPT/RAG still running) got zero grace at all — its task
# was cancelled immediately, silently discarding the first question with no
# answer ever generated. voice/stream.py now applies this same grace window
# to ANY in-flight turn, whether it already has a ready answer or is still
# being generated, so a caller asking a second thing a moment after the
# first is never met with the first one just vanishing.
#
# This is the MAXIMUM time (ms) an in-flight turn gets before being treated
# as genuinely superseded/barged-in-on. Real call latencies for a full
# GPT/RAG turn run 2-8 seconds (see brain.agent "latency=" log lines) — the
# old 1500ms default was tuned only for the "already-speaking" case and was
# usually too short to let a still-generating turn actually finish. Raised
# to 3000ms as a better balance: still short enough that a real barge-in
# (caller talking over active TTS) gets cut off promptly, but long enough to
# catch most "asked a second question moments after the first, before any
# reply" cases seen in testing. Tune from here based on real call latency
# data, not by guessing — if GPT/RAG latency trends up, this may need to
# rise with it.
TURN_PREEMPT_GRACE_MS: int = int(os.getenv("TURN_PREEMPT_GRACE_MS", 3000))

# SMART_HEARING_SHORT_FRAGMENT_WORDS / _GRACE_MS — a fragment this short
# with no terminal punctuation ("Could you", "Can you also") is almost
# never a genuine complete utterance on its own — real one-off
# acknowledgements ("Yes", "No", "Okay"...) are already dropped entirely by
# is_filler_transcript() before reaching this check. What's left in this
# bucket is overwhelmingly a clause-starter caller paused mid-thought on,
# so it gets a longer grace window than an ordinary incomplete-sounding
# fragment — real call evidence: a caller pausing ~1.5s between "Could
# you" and finishing "...explore about the generative AI field also?" got
# answered with a nonsense filler reply because the standard 700ms grace
# expired first.
SMART_HEARING_SHORT_FRAGMENT_WORDS: int = int(os.getenv("SMART_HEARING_SHORT_FRAGMENT_WORDS", 3))
SMART_HEARING_SHORT_FRAGMENT_GRACE_MS: int = int(os.getenv("SMART_HEARING_SHORT_FRAGMENT_GRACE_MS", 2200))

# ── ElevenLabs TTS — batch + streaming (voice/tts.py) ───────────────────────
# Only used when the Settings dashboard's "Voice Provider" is set to
# "elevenlabs" AND a real elevenlabs_voice_id is configured (see
# synthesize_speech() / StreamingTTSSession in voice/tts.py) — Sarvam Bulbul
# stays the default/fallback voice otherwise, exactly as before. These were
# previously hardcoded literals inside _elevenlabs_tts(); moved here so the
# model/voice tuning is a .env change, not a code change, same as every
# other provider setting in this file.
#
# ELEVENLABS_TTS_MODEL — batch (REST) TTS model_id.
ELEVENLABS_TTS_MODEL: str = os.getenv("ELEVENLABS_TTS_MODEL", "eleven_turbo_v2")
# ELEVENLABS_TTS_STREAMING_MODEL — WebSocket streaming TTS model_id, kept
# separate since not every model supports the streaming endpoint (e.g.
# eleven_v3 explicitly does not, per ElevenLabs' own docs). Flash v2.5 is
# ElevenLabs' current low-latency real-time model.
ELEVENLABS_TTS_STREAMING_MODEL: str = os.getenv("ELEVENLABS_TTS_STREAMING_MODEL", "eleven_flash_v2_5")
ELEVENLABS_TTS_STABILITY: float = float(os.getenv("ELEVENLABS_TTS_STABILITY", 0.5))
ELEVENLABS_TTS_SIMILARITY_BOOST: float = float(os.getenv("ELEVENLABS_TTS_SIMILARITY_BOOST", 0.75))
# ELEVENLABS_TTS_OUTPUT_FORMAT — "ulaw_8000" asks ElevenLabs to hand back
# audio already in Twilio's native 8kHz mu-law format, same reasoning as
# SARVAM_TTS_OUTPUT_CODEC below: removes our own PCM→mulaw conversion step
# from the streaming path when ElevenLabs is the active provider.
ELEVENLABS_TTS_OUTPUT_FORMAT: str = os.getenv("ELEVENLABS_TTS_OUTPUT_FORMAT", "ulaw_8000")
# ELEVENLABS_TTS_AUDIO_IDLE_TIMEOUT_SEC — mirrors
# SARVAM_TTS_AUDIO_IDLE_TIMEOUT_SEC's reasoning below: how long
# ElevenLabsStreamingTTSSession.audio_chunks() waits for the next message
# before concluding the turn's audio is done, independent of whether
# ElevenLabs sends an explicit final/close signal.
ELEVENLABS_TTS_AUDIO_IDLE_TIMEOUT_SEC: float = float(os.getenv("ELEVENLABS_TTS_AUDIO_IDLE_TIMEOUT_SEC", 4.0))

# CONFIDENCE NOTE (verify against live ElevenLabs docs before relying on this
# in production): the streaming endpoint
# (wss://api.elevenlabs.io/v1/text-to-speech/{voice_id}/stream-input),
# xi-api-key auth, the {"text": " ", "voice_settings": {...}} handshake
# message, per-chunk {"text": "..."} sends, the {"text": ""} end-of-stream
# message, and the {"audio": base64, "isFinal": bool} response shape are
# taken from ElevenLabs' published WebSocket TTS API reference as of this
# writing. Re-verify against the current API reference and a real test call
# after any ElevenLabs API version bump.

# ── Streaming TTS (voice/tts.py: StreamingTTSSession) ───────────────────────
# SARVAM_TTS_MODEL — kept separate from the batch path's TTS model setting
# for the same independent-rollback reason as SARVAM_STT_MODEL above.
SARVAM_TTS_MODEL: str = os.getenv("SARVAM_TTS_MODEL", "bulbul:v3")
SARVAM_TTS_SPEAKER: str = os.getenv("SARVAM_TTS_SPEAKER", "shubh")
SARVAM_TTS_PACE: float = float(os.getenv("SARVAM_TTS_PACE", 1.0))
# SARVAM_TTS_MIN_BUFFER_SIZE — how many characters of text must accumulate
# before Sarvam starts synthesizing. Lower = first audio starts sooner but
# on tinier, choppier fragments; higher = smoother phrasing but more delay
# before the caller hears anything. 30 chars is roughly "a few words" —
# enough for natural prosody without waiting for a whole sentence.
SARVAM_TTS_MIN_BUFFER_SIZE: int = int(os.getenv("SARVAM_TTS_MIN_BUFFER_SIZE", 30))
# SARVAM_TTS_MAX_CHUNK_LENGTH — the ceiling Sarvam splits on internally,
# independent of our own split_into_sentences() chunking in voice/stream.py.
SARVAM_TTS_MAX_CHUNK_LENGTH: int = int(os.getenv("SARVAM_TTS_MAX_CHUNK_LENGTH", 120))
# SARVAM_TTS_OUTPUT_CODEC — "mulaw" asks Sarvam to hand back audio already
# in Twilio's native 8kHz mu-law format, which removes our own PCM→mulaw
# conversion step (voice/stream.py: pcm_to_mulaw) from the streaming path
# entirely. Only change this if you have a reason to post-process the
# audio yourself before it reaches Twilio.
SARVAM_TTS_OUTPUT_CODEC: str = os.getenv("SARVAM_TTS_OUTPUT_CODEC", "mulaw")
SARVAM_TTS_OUTPUT_SAMPLE_RATE: int = int(os.getenv("SARVAM_TTS_OUTPUT_SAMPLE_RATE", 8000))

# STREAMING_TTS_TURN_TIMEOUT_SEC — hard ceiling on one turn's entire
# streaming-TTS lifecycle (connect + configure + feed all sentences +
# receive all audio + close). If Sarvam's socket ever goes quiet — no audio,
# no error, no close — this is what unblocks the call instead of hanging
# it forever. Generous on purpose (a real multi-sentence reply legitimately
# takes several seconds); this is a safety net, not a latency target.
STREAMING_TTS_TURN_TIMEOUT_SEC: int = int(os.getenv("STREAMING_TTS_TURN_TIMEOUT_SEC", 20))

# SARVAM_TTS_AUDIO_IDLE_TIMEOUT_SEC — how long audio_chunks() waits for the
# *next* message before concluding "no more audio is coming for this turn".
# Sarvam's own "final" completion event (send_completion_event=true) is the
# primary end-of-turn signal, but real testing showed the socket can stay
# open and simply go quiet after the last real audio chunk, with no event
# and no close — this idle timeout is what lets a turn end promptly and
# gracefully in that case, well inside the STREAMING_TTS_TURN_TIMEOUT_SEC
# hard cap above (which stays as a backstop for a fully unresponsive
# connect/configure, not normal end-of-turn).
SARVAM_TTS_AUDIO_IDLE_TIMEOUT_SEC: float = float(os.getenv("SARVAM_TTS_AUDIO_IDLE_TIMEOUT_SEC", 4.0))

# ─── Phone number validation — utils/phone.py ──────────────────────────────
# PHONE_NUMBER_DIGIT_LENGTH — the exact number of digits a valid phone
# number must have AFTER country code / trunk-prefix stripping. Used by
# brain/tools.py to hard-reject an incomplete or over-long number before it
# is ever saved as a lead or read back for a booking, instead of trusting
# whatever digit count GPT-4o decided was "enough". Default 10 = Indian
# mobile numbers.
PHONE_NUMBER_DIGIT_LENGTH: int = int(os.getenv("PHONE_NUMBER_DIGIT_LENGTH", 10))
# PHONE_NUMBER_COUNTRY_CODE — stripped from the front of the number when
# present (e.g. "+91 98765 43210" → "9876543210"). Leave blank to disable.
PHONE_NUMBER_COUNTRY_CODE: str = os.getenv("PHONE_NUMBER_COUNTRY_CODE", "91")
# PHONE_NUMBER_ALLOWED_FIRST_DIGITS — after normalization, the number must
# start with one of these digits (Indian mobile numbers always start 6-9).
# Leave blank ("") to skip this check entirely for other regions/deployments.
PHONE_NUMBER_ALLOWED_FIRST_DIGITS: str = os.getenv("PHONE_NUMBER_ALLOWED_FIRST_DIGITS", "6789")
# PHONE_FIDELITY_MIN_CHUNK_DIGITS — utils/phone.py's phone_supported_by_history
# accepts a number that's split across two turns as long as each piece is
# at least this many digits long (prevents trivially short 1-2 digit
# coincidences from falsely validating a number).
PHONE_FIDELITY_MIN_CHUNK_DIGITS: int = int(os.getenv("PHONE_FIDELITY_MIN_CHUNK_DIGITS", 4))

# Bounds the Google Calendar insert, which now runs in a worker thread via
# asyncio.to_thread (brain/tools.py). Without the thread hop it blocked the
# event loop and froze audio for every concurrent call.
GOOGLE_CALENDAR_TIMEOUT_SEC: float = float(os.getenv("GOOGLE_CALENDAR_TIMEOUT_SEC", 5.0))

# ─── Deployment environment ───────────────────────────────────────────────
# Drives fail-fast behaviour in config_validation.py. In "production" a
# configuration error refuses to boot; anywhere else it is logged and boot
# continues, so local work with a sparse .env stays frictionless while a bad
# production deploy is impossible to miss.
ENVIRONMENT: str = os.getenv("ENVIRONMENT", "development").lower()
IS_PRODUCTION: bool = ENVIRONMENT == "production"


# ═══════════════════════════════════════════════════════════════════════════
# QA REMEDIATION — round six (test-plan findings VA-T-001 … VA-T-016)
# ═══════════════════════════════════════════════════════════════════════════
# Everything below follows the same rule as the rest of this file: a working
# default lives HERE, and .env carries only what is genuinely deployment
# specific — secrets, addresses, and anything that differs between one
# install and the next. A fresh clone with an empty .env must still boot and
# behave sensibly; .env is for what a deployment knows that the code cannot.
# ═══════════════════════════════════════════════════════════════════════════

# ─── Rate limiting behind a proxy (VA-T-006, rate_limit.py) ────────────────
# The limiter keyed on the socket peer, which in every real deployment is an
# intermediary rather than the caller: dashboard traffic arrives from the
# Next.js server so all users shared one bucket, and /incoming-call arrives
# from the ingress so a per-client guard became a GLOBAL ceiling on inbound
# call volume.
#
# X-Forwarded-For is client-supplied and trivially forged, so it is honoured
# ONLY for requests that actually came from an address named here. Empty is
# the safe default and matches a direct local run exactly.
#
# Set this per deployment — it is infrastructure topology, which is precisely
# the kind of thing code cannot know:
#   docker-compose : the bridge network, e.g. 172.16.0.0/12
#   Kubernetes     : the pod CIDR, e.g. 10.0.0.0/8
#   single host    : leave blank if nothing sits in front of uvicorn
# Accepts a comma-separated list of IPs and/or CIDR blocks.
RATE_LIMIT_TRUSTED_PROXIES: str = os.getenv("RATE_LIMIT_TRUSTED_PROXIES", "")

# ─── Knowledge-base version (VA-T-003 / VA-T-016, cache/kb_version.py) ─────
# How long a worker may reuse its cached view of the knowledge base's version
# before re-querying. Both the LLM cache and the RAG cache are keyed on this
# value, so it bounds how long a stale entry can survive a change nobody
# signalled explicitly.
#
# Upload and delete call invalidate_kb_version() directly, so in practice a
# change takes effect on the next turn; this TTL is the backstop for a change
# that arrives some other way — ingest_documents.py run from a shell, or a row
# removed by hand in SQL.
KB_VERSION_TTL_SECONDS: int = int(os.getenv("KB_VERSION_TTL_SECONDS", 60))

# ─── Shutdown budget (VA-T-011, main.py) ──────────────────────────────────
# The call drain and the Twilio price reconciliation drain now run
# CONCURRENTLY rather than back to back, so the shutdown cost is the longer of
# the two instead of their sum. This is the hard ceiling on the pair.
#
# It MUST stay below the platform's kill deadline — terminationGracePeriodSeconds
# in k8s/04-backend-deployment.yaml, or stop_grace_period in docker-compose.
# Previously 30 + 30 ran against a 45s deadline, so the process was SIGKILLed
# mid-reconciliation and the Redis/HTTP teardown after it never ran at all,
# leaking connections on every rolling restart. config_validation checks this
# relationship at startup so the mismatch is caught on boot, not during an
# incident.
SHUTDOWN_TOTAL_BUDGET_SEC: float = float(os.getenv("SHUTDOWN_TOTAL_BUDGET_SEC", 35.0))

# The platform deadline, mirrored here ONLY so the startup check can compare
# against it. Nothing reads it at runtime. Keep it in step with the manifest;
# if they diverge the check warns rather than silently trusting this value.
PLATFORM_TERMINATION_GRACE_SEC: float = float(
    os.getenv("PLATFORM_TERMINATION_GRACE_SEC", 60.0)
)

# ─── Dashboard session auth (VA-T-002, frontend middleware) ───────────────
# The dashboard had no login at all: the proxy attached the admin key to every
# request, so anyone who could reach the host had full access to transcripts,
# caller PII and the agent's settings.
#
# A single shared password is the deliberate first step — adequate while this
# is an internal console, and explicitly NOT adequate once clients log in,
# because one password cannot separate one client's calls from another's.
# The frontend reads these; they are listed here so the startup check can warn
# when the dashboard is unprotected.
# Logins are per-user accounts in the `users` table (migration 0005), with
# Argon2id password hashes — NOT one shared password. A shared secret cannot
# say WHO changed a setting, cannot revoke one person without locking out
# everybody, and for a multi-client product cannot separate one client's call
# transcripts from another's.
#
# This secret signs the session token. It is NOT a password and nobody types
# it: it is the key the backend signs with and the frontend verifies with, so
# middleware can check a session without a database round trip on every
# request. It must be IDENTICAL in backend .env and frontend/.env.
#
# Rotating it invalidates every live session immediately, which is the lever
# to pull if one must be killed before it expires.
# Generate with: openssl rand -hex 32
DASHBOARD_SESSION_SECRET: str = os.getenv("DASHBOARD_SESSION_SECRET", "")

# How long a login lasts before re-authentication. A signed token cannot be
# revoked early, so this bounds how long a deactivated account keeps working.
DASHBOARD_SESSION_TTL_HOURS: int = int(os.getenv("DASHBOARD_SESSION_TTL_HOURS", 12))

# Per-account lockout. Password hashing protects a leaked database; it does
# nothing against someone guessing at the login endpoint, where the attacker
# never sees a hash. Without a lockout an internet-reachable dashboard can be
# attacked at HTTP speed indefinitely.
#
# The lock expires on its own so a colleague who mistyped is not permanently
# shut out and nobody has to run a manual unlock at 2am.
LOGIN_MAX_FAILED_ATTEMPTS: int = int(os.getenv("LOGIN_MAX_FAILED_ATTEMPTS", 5))
LOGIN_LOCKOUT_MINUTES: int = int(os.getenv("LOGIN_LOCKOUT_MINUTES", 15))

# Tighter than RATE_LIMIT_DEFAULT: lockout stops one account being hammered,
# this stops one client spraying a common password across many usernames,
# which per-account lockout would never notice.
RATE_LIMIT_LOGIN: str = os.getenv("RATE_LIMIT_LOGIN", "10/minute")


# ─── Retrieval confidence band (VA-T-012 / VA-T-013, rag/search.py) ───────
# A single pass/fail threshold produced BOTH measured failures from one
# number: 80% retrieval failure (legitimate paraphrases landing just over the
# line) and 13.3% hallucination (the specialist answering from model training
# when nothing was retrieved — once with a factually CORRECT answer and zero
# retrieval, which is the dangerous case because nothing flags it).
#
#   distance <= STRONG              confident; answer normally
#   STRONG < distance <= RELEVANCE  usable but uncertain; the passage is used
#                                   under strict answer-only-from-this rules
#   distance > RELEVANCE            nothing useful; refuse, offer a callback
#
# THESE NUMBERS ARE A STARTING POINT, NOT A MEASUREMENT. They are read from
# the QA round's recorded distances — misses at 0.7461/0.7562/0.7861/0.838
# against hits at 0.3727-0.6246. Re-run the AI Evals Dataset B after changing
# them and move them based on what it reports, not on judgement.
RAG_STRONG_MATCH_THRESHOLD: float = float(os.getenv("RAG_STRONG_MATCH_THRESHOLD", 0.55))

# Raised from 0.70. At 0.70 every one of the QA round's retrieval failures was
# a real question about content that exists in the knowledge base.
RAG_RELEVANCE_THRESHOLD: float = float(os.getenv("RAG_RELEVANCE_THRESHOLD", 0.80))


# ─── Guardrail deflection (VA-T-014, orchestrator/graph.py) ───────────────
# Spoken instead of whatever the Receptionist produced when the input
# guardrail flagged the turn AND the route was "direct".
#
# A prompt-injection attempt names no topic, so it classifies as pure social
# exchange and was answered "Yes, go ahead!". Nothing leaked — but a
# transcript showing the agent apparently agreeing to "ignore all previous
# instructions" is its own problem, for a client reviewing calls and for the
# caller deciding whether to try again.
#
# Deliberately bland: it declines without confirming that anything was
# detected, which would tell a prober exactly which phrasings trip the filter.
GUARDRAIL_DEFLECTION_MESSAGE: str = os.getenv(
    "GUARDRAIL_DEFLECTION_MESSAGE",
    "I can only help with questions about {company_name} — what would you like to know?",
)


# ─── Escalation notifications (notifications/notifier.py) ─────────────────
# `escalate` used to write a database row, flip the call status, publish an
# event — and tell no human being, while telling the CALLER "our team will
# follow up with you shortly". A promise with no mechanism behind it.
#
# ALL OF THIS IS OFF UNTIL A CHANNEL IS CONFIGURED. With none set,
# dispatch_escalation returns immediately, so an existing deployment that
# pulls this code and changes nothing behaves exactly as it does today.
#
# Delivery never touches the call path: it runs in a task the caller's turn
# does not await, so a slow or broken Slack cannot slow a reply.
ENABLE_ESCALATION_NOTIFICATIONS: bool = os.getenv("ENABLE_ESCALATION_NOTIFICATIONS", "true").lower() == "true"

# Slack incoming webhook — richest output, best for a team channel.
ESCALATION_SLACK_WEBHOOK_URL: str = os.getenv("ESCALATION_SLACK_WEBHOOK_URL", "")
# A number to text, using the Twilio credentials already configured. Best for
# genuinely out-of-hours paging.
ESCALATION_SMS_TO: str = os.getenv("ESCALATION_SMS_TO", "")
# Generic JSON POST — PagerDuty, Opsgenie, n8n, or your own endpoint.
ESCALATION_WEBHOOK_URL: str = os.getenv("ESCALATION_WEBHOOK_URL", "")
ESCALATION_WEBHOOK_TOKEN: str = os.getenv("ESCALATION_WEBHOOK_TOKEN", "")

# Transcript excerpts travel into Slack and webhooks, which usually have a
# wider audience and longer retention than the database. Keep the excerpt
# short and let the dashboard link carry the detail.
ESCALATION_SNIPPET_MAX_CHARS: int = int(os.getenv("ESCALATION_SNIPPET_MAX_CHARS", 400))
NOTIFICATION_TIMEOUT_SEC: float = float(os.getenv("NOTIFICATION_TIMEOUT_SEC", 6.0))

# Used only to build the "Open dashboard" deep link in a notification.
# Cosmetic — blank simply omits the button.
DASHBOARD_BASE_URL: str = os.getenv("DASHBOARD_BASE_URL", "").rstrip("/")


# ─── Error tracking (observability/error_tracking.py) ─────────────────────
# There was none. An agent failing at 3am was discovered by complaint, and
# several handlers log `except Exception as e` without a traceback, so even
# the log did not say where a failure came from.
#
# OFF BY DEFAULT. With SENTRY_DSN unset the SDK is never imported and every
# function is a no-op, so this cannot affect a working deployment.
#
# Call transcripts contain caller PII, so send_default_pii is False AND a
# before_send hook strips anything phone-number-shaped from the message — the
# first stops the SDK volunteering data, the second catches what our own log
# lines put in an exception string.
SENTRY_DSN: str = os.getenv("SENTRY_DSN", "")
# Sampled: performance traces on a voice workload are high-volume and mostly
# identical. 5% spots a regression without meaningful overhead.
SENTRY_TRACES_SAMPLE_RATE: float = float(os.getenv("SENTRY_TRACES_SAMPLE_RATE", 0.05))


# ─── Database connection ceiling (VA-T-005) ───────────────────────────────
# database/base.py sizes the pool at MAX_CONCURRENT_CALLS + the same again in
# overflow, so ONE pod can hold 2 x MAX_CONCURRENT_CALLS connections. At the
# HPA's maxReplicas that multiplies: 20 calls x 2 x 10 pods = 400, against a
# Supabase pooler that typically caps around 200. The pods would win the race
# and the excess would fail to connect, mid-call.
#
# The right fix depends on a decision this code cannot make — one shared
# database, or one per client. So instead of guessing, the ceiling is declared
# here and config_validation compares the arithmetic at startup and says so.
# Set it to your provider's actual limit.
DB_CONNECTION_CEILING: int = int(os.getenv("DB_CONNECTION_CEILING", 200))
# Mirrors spec.maxReplicas in k8s/04-backend-deployment.yaml. Nothing reads it
# at runtime; it exists so the startup check can do the multiplication.
MAX_REPLICAS: int = int(os.getenv("MAX_REPLICAS", 10))

# ─── Mock interview (browser, /interview dashboard page) ───────────────────
# A spoken practice interview in the dashboard: the backend generates
# questions for a role, evaluates each answer, asks at most
# INTERVIEW_MAX_FOLLOWUPS_PER_QUESTION follow-ups, and writes a scored report
# at the end. Lives in mock_interview/ and api/interview.py — it shares the
# OpenAI client, guardrails, Redis and TTS with the phone agent but touches
# none of the call pipeline.
#
# Question writing and the final report are one call each per interview, so
# they get the stronger model. Per-answer evaluation happens on every turn
# while the candidate waits, so it defaults to the fast router model.
INTERVIEW_QUESTION_MODEL: str = os.getenv("INTERVIEW_QUESTION_MODEL", OPENAI_MODEL)
INTERVIEW_TURN_MODEL: str = os.getenv("INTERVIEW_TURN_MODEL", ORCHESTRATOR_ROUTER_MODEL)
INTERVIEW_REPORT_MODEL: str = os.getenv("INTERVIEW_REPORT_MODEL", OPENAI_MODEL)
# The shared client's OPENAI_REQUEST_TIMEOUT_SEC (8s) is tuned for a live
# phone turn. Writing ten questions or a full report routinely takes longer,
# and nobody is on a phone line waiting, so these calls get their own bound.
INTERVIEW_LLM_TIMEOUT_SEC: float = float(os.getenv("INTERVIEW_LLM_TIMEOUT_SEC", 45.0))
# How long an interview session is kept (Redis, or in-process if Redis is off).
INTERVIEW_SESSION_TTL: int = int(os.getenv("INTERVIEW_SESSION_TTL", 7200))
INTERVIEW_MAX_QUESTIONS: int = int(os.getenv("INTERVIEW_MAX_QUESTIONS", 10))
INTERVIEW_MAX_FOLLOWUPS_PER_QUESTION: int = int(os.getenv("INTERVIEW_MAX_FOLLOWUPS_PER_QUESTION", 1))
# Longer answers are truncated before evaluation (about 5-6 minutes of speech).
INTERVIEW_MAX_ANSWER_CHARS: int = int(os.getenv("INTERVIEW_MAX_ANSWER_CHARS", 4000))
# The phone path synthesizes at 8kHz because that is all a phone line carries.
# A browser can play full-band audio, so the interview asks Sarvam for more.
INTERVIEW_TTS_SAMPLE_RATE: int = int(os.getenv("INTERVIEW_TTS_SAMPLE_RATE", 22050))
INTERVIEW_MAX_SPEAK_CHARS: int = int(os.getenv("INTERVIEW_MAX_SPEAK_CHARS", 1200))
