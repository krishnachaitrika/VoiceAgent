import asyncio
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI, Response
from fastapi.middleware.cors import CORSMiddleware
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from sqlalchemy import text
from api import twilio_webhook, websocket, dashboard, settings, documents, analytics, auth_session, interview
from api.websocket import begin_shutdown, wait_for_drain
from cache.redis_client import ping as redis_ping, close as redis_close
from database.base import get_engine
from voice.tts import close_tts_client
from voice.stt import close_stt_client
from logging_utils import CallCorrelationFilter, JSONFormatter
from rate_limit import limiter
import config
from config_validation import validate_and_report
from observability.error_tracking import init_error_tracking

# Structured (JSON) logging with call_id/turn_id fields — see
# logging_utils.py. Replaces the previous plain-text basicConfig format,
# which had no way to correlate a log line back to a specific call except
# grepping for a hand-written "[call_id]" prefix in the message text.
_handler = logging.StreamHandler()
_handler.setFormatter(JSONFormatter())
_handler.addFilter(CallCorrelationFilter())
logging.basicConfig(level=logging.INFO, handlers=[_handler])
logger = logging.getLogger(__name__)


def _warn_unset_cost_rates() -> None:
    """Report any provider cost rate left at its 0.0 default.

    Every rate defaults to 0.0 rather than to a plausible figure, deliberately:
    a wrong-but-credible number is far more dangerous than an obvious zero,
    because nobody questions it. A real example from this project — an outbound
    Twilio rate guessed at $0.12/min when the account was actually billed
    $0.0699 — overstated every outbound call by 42% and looked entirely
    reasonable in the dashboard.

    But a silent zero has its own failure mode: a deployment that never set the
    rates records every call at $0.00 and the team concludes the product is
    cheap to run. So the zeros are announced loudly, once, at startup. Warn,
    never guess.
    """
    rates = [
        ("ELEVENLABS_STT_COST_PER_MINUTE", config.ELEVENLABS_STT_COST_PER_MINUTE,
         "speech-to-text (billed per minute of audio, on every call)"),
        ("SARVAM_TTS_COST_PER_1K_CHARS", config.SARVAM_TTS_COST_PER_1K_CHARS,
         "Sarvam text-to-speech (per 1K characters)"),
        ("ELEVENLABS_TTS_COST_PER_1K_CHARS", config.ELEVENLABS_TTS_COST_PER_1K_CHARS,
         "ElevenLabs text-to-speech (only used if voice_provider is elevenlabs)"),
        ("TWILIO_INBOUND_COST_PER_MINUTE", config.TWILIO_INBOUND_COST_PER_MINUTE,
         "inbound voice minutes, rounded up"),
        ("TWILIO_OUTBOUND_COST_PER_MINUTE", config.TWILIO_OUTBOUND_COST_PER_MINUTE,
         "outbound voice minutes — typically 10-15x the inbound rate"),
    ]
    unset = [(name, why) for name, value, why in rates if value <= 0.0]

    if not unset:
        logger.info(f"Cost tracking: all {len(rates)} provider rates configured")
        return

    logger.warning(
        f"COST CONFIG: {len(unset)} of {len(rates)} provider rate(s) are not set. "
        f"Those costs will record as $0.00 and the dashboard will understate "
        f"what each call actually costs."
    )
    for name, why in unset:
        logger.warning(f"COST CONFIG:   {name} — {why}")
    logger.warning(
        "COST CONFIG: set these in .env from your provider invoices. They are "
        "left at 0.0 rather than defaulted to a plausible figure on purpose — a "
        "wrong rate that looks credible is worse than an obvious zero."
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup — verify Redis connectivity but never block boot on it. Redis
    # is a performance layer (session/LLM/RAG cache, event bus) — if it's
    # down, the app falls back to safe in-process behaviour automatically
    # rather than crashing (see cache/redis_client.py).
    if config.REDIS_ENABLED:
        connected = await redis_ping()
        logger.info(f"Redis connectivity at startup: {'OK' if connected else 'UNAVAILABLE (falling back to in-process cache/memory)'}")
    logger.info(f"Agent: {config.AGENT_NAME} | Company: {config.COMPANY_NAME} | Orchestrator: LangGraph")

    # Round six: several QA findings were configuration that looked fine and
    # was wrong, with nothing anywhere saying so — a greeting promising Tamil
    # and Hindi on an English-only build (VA-T-015), a shutdown budget longer
    # than the platform's kill deadline (VA-T-011), an unprotected dashboard
    # (VA-T-002). config_validation checks the relationships code CAN know and
    # refuses to boot in production when one is broken.
    #
    # Cost rates are reported by the same module now; the standalone warning
    # below is kept because it enumerates every rate individually, which is
    # more useful than one summary line when you are setting them up.
    # Initialised BEFORE validation so a configuration error that refuses to
    # boot is itself reported. A no-op when SENTRY_DSN is unset.
    init_error_tracking()

    validate_and_report()
    _warn_unset_cost_rates()

    from notifications.notifier import configured_channels

    channels = configured_channels()
    logger.info(
        f"Escalation notifications: {', '.join(channels)}"
        if channels
        else "Escalation notifications: NONE configured — escalations are recorded "
             "but nobody is alerted"
    )
    # STT is now fully on ElevenLabs Scribe v2 (voice/stt.py) with no Sarvam
    # fallback path — unlike TTS, which still falls back to Sarvam Bulbul.
    # Fail loudly and early at boot if the key is missing, rather than
    # letting every single call silently fail to hear the caller.
    if not config.ELEVENLABS_API_KEY:
        logger.error(
            "ELEVENLABS_API_KEY is not set — STT (voice/stt.py) has no fallback "
            "provider and every call will fail to transcribe speech. Set "
            "ELEVENLABS_API_KEY in .env before accepting calls."
        )
    yield
    # Shutdown (VA-C6 fix) — this used to close Redis/HTTP clients only,
    # with no idea whether a call was still in progress. Stop accepting
    # new /stream connections first, then give in-flight ones a bounded
    # window to finish naturally (each writes its own transcript/cost in
    # handle_twilio_stream's finally block on completion) before tearing
    # down the clients they depend on.
    begin_shutdown()

    # VA-T-011 FIX — THE TWO DRAINS RAN BACK TO BACK AND BLEW THE DEADLINE.
    #
    # wait_for_drain (30s) followed by drain_twilio_prices (30s) is up to 60s
    # of shutdown against terminationGracePeriodSeconds: 45. Kubernetes SIGKILLs
    # at the deadline, so the reconciliation drain was cut off mid-flight AND
    # the client teardown below never ran at all — leaking Redis and HTTP
    # connections on every single rolling restart.
    #
    # They are independent: live calls and price reconciliations wait on
    # different things and neither blocks the other. Running them concurrently
    # makes the total the LONGER of the two rather than their sum.
    #
    # The whole thing is then bounded by SHUTDOWN_TOTAL_BUDGET_SEC, which
    # config validates against the platform deadline at startup — so a
    # misconfigured grace period is caught on boot rather than discovered
    # during an incident.
    from billing.twilio_reconcile import drain_pending as drain_twilio_prices
    from notifications.notifier import close_notifier, drain_pending as drain_notifications

    try:
        await asyncio.wait_for(
            asyncio.gather(
                wait_for_drain(config.GRACEFUL_SHUTDOWN_DRAIN_SEC),
                drain_twilio_prices(),
                # Added to the SAME concurrent gather, not as a fourth
                # sequential step — VA-T-011 was caused by stacking drains
                # until they exceeded the platform's kill deadline.
                drain_notifications(),
                return_exceptions=True,
            ),
            timeout=config.SHUTDOWN_TOTAL_BUDGET_SEC,
        )
    except asyncio.TimeoutError:
        logger.warning(
            f"Shutdown budget ({config.SHUTDOWN_TOTAL_BUDGET_SEC}s) reached — "
            f"proceeding to client teardown so connections are still released."
        )
    await redis_close()
    await close_tts_client()
    await close_notifier()
    await close_stt_client()


# Every value below comes from config.py (which reads .env) — nothing here
# is hardcoded, so AGENT_NAME/COMPANY_NAME/APP_VERSION changes in .env show
# up automatically on restart with zero code edits.
app = FastAPI(
    title=f"{config.COMPANY_NAME} Voice Agent",
    description=f"AI voice agent — {config.AGENT_NAME} — for {config.COMPANY_NAME} inbound calls",
    version=config.APP_VERSION,
    lifespan=lifespan,
)

# REAL-BUG FIX: allow_origins=["*"] + allow_credentials=True is an invalid
# combination per the CORS spec — browsers refuse to honor a wildcard
# Access-Control-Allow-Origin on a credentialed request, so this silently
# broke the moment the dashboard needed to send cookies/credentials. Now
# reads an explicit allow-list from config.CORS_ALLOWED_ORIGINS (.env:
# CORS_ALLOWED_ORIGINS) instead of a wildcard — see config.py for defaults
# and how to add your production dashboard domain.
app.add_middleware(
    CORSMiddleware,
    allow_origins=config.CORS_ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Rate limiting (VA-B3 fix) — nothing previously bounded requests per
# client on the HTTP surface at all. Keyed by remote address; every route
# gets config.RATE_LIMIT_DEFAULT unless overridden with its own @limiter.limit(...)
# (see api/twilio_webhook.py's stricter limit on /incoming-call). The
# Limiter instance itself lives in rate_limit.py so route modules can
# import it too without a circular import on main.py.
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(SlowAPIMiddleware)

# Routers
app.include_router(twilio_webhook.router)
app.include_router(websocket.router)
# Mounted WITHOUT require_dashboard_auth: this is how a session is obtained
# in the first place, so gating it would be circular. Protected by its own
# tighter rate limit (RATE_LIMIT_LOGIN) plus per-account lockout instead.
app.include_router(auth_session.router, prefix="/api")

app.include_router(dashboard.router, prefix="/api")
app.include_router(settings.router, prefix="/api")
app.include_router(documents.router, prefix="/api")     # v2 — document upload
app.include_router(analytics.router, prefix="/api")     # v3+ — sentiment + event feed
app.include_router(interview.router, prefix="/api")     # browser mock interview (mock_interview/)


@app.get("/health")
async def health():
    return {"status": "ok", "agent": config.AGENT_NAME, "company": config.COMPANY_NAME, "version": config.APP_VERSION}


@app.get("/ready")
async def ready(response: Response):
    """
    Readiness probe (used by the Kubernetes deployment's readinessProbe —
    see k8s/04-backend-deployment.yaml). Reports on the enterprise layer
    without ever failing the probe just because Redis is briefly down —
    Redis is a performance layer, not a hard dependency.

    VA-C4 fix: this used to check Redis (optional) but never Postgres
    (mandatory — every turn hits the DB) — a backend whose database was
    unreachable still passed readiness and kept receiving calls. A cheap
    SELECT 1 now fails the probe (503) if the database isn't reachable;
    Redis stays reported but non-fatal, unchanged.
    """
    redis_ok = await redis_ping() if config.REDIS_ENABLED else None

    db_ok = True
    try:
        async with get_engine().connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception as e:
        db_ok = False
        logger.error(f"Readiness probe: database unreachable: {e}")
        response.status_code = 503

    return {
        "status": "ready" if db_ok else "not_ready",
        "agent": config.AGENT_NAME,
        "company": config.COMPANY_NAME,
        "version": config.APP_VERSION,
        "database_connected": db_ok,
        "redis_connected": redis_ok,
        "orchestrator": "langgraph",
        "guardrails": config.ENABLE_GUARDRAILS,
        "rolling_memory": config.ENABLE_ROLLING_MEMORY,
        "sentiment_analytics": config.ENABLE_SENTIMENT_ANALYTICS,
        "event_bus": config.ENABLE_EVENT_BUS,
    }


@app.get("/")
async def root():
    return {"message": f"{config.COMPANY_NAME} Voice Agent API is running. Visit /docs for API docs."}