"""
config_validation.py — catch deployment mistakes at startup, not on a live call.

WHY A WHOLE MODULE FOR THIS

Several of the defects in the QA round were not code bugs at all. They were
configuration that looked fine and was wrong, and nothing anywhere said so:

  VA-T-015  GREETING_TEMPLATE in .env promised "Tamil, Hindi, or English" on a
            build that only ever answers in English. Wrong on 100% of calls,
            the first sentence every caller hears, and a one-line fix — but
            nothing flagged it, so it survived into a QA round.

  VA-T-011  Two 30s shutdown drains against terminationGracePeriodSeconds: 45.
            Arithmetic anyone could have checked, discovered by reading the
            manifest against the code.

  VA-T-007  All five provider cost rates defaulting to 0.0, so a deployment
            silently recorded every call at $0.00 and looked cheap to run.

  VA-T-002  No dashboard password, so the console was open to anyone who could
            reach the host.

The pattern is the same each time: a value that is individually plausible,
wrong in context, and silent. Code cannot know the right value — only the
deployment does — but it CAN know when two values contradict each other, or
when something security-relevant was left blank.

So this runs once at startup and says so, loudly, naming the setting and the
consequence. It never guesses a value on the deployment's behalf: a wrong
number that looks credible is worse than an obvious blank, which is the exact
lesson from an outbound Twilio rate estimated at $0.12 against a real $0.0699.

STRICTNESS
  ENVIRONMENT=production   errors refuse to boot
  anything else            everything logged, boot continues
That asymmetry is the point. Local work with a sparse .env must stay
frictionless; a bad production deploy must be impossible to miss.
"""
import logging
from dataclasses import dataclass, field
from typing import List

import config

logger = logging.getLogger(__name__)

# Languages the build can actually answer in. The orchestrator's
# LANGUAGE_INSTRUCTION is the fixed string "Respond entirely in English.", and
# migration 0002 dropped calls.language_detected and leads.language precisely
# because the build went English-only. Anything else named in the greeting is a
# promise the agent cannot keep.
SUPPORTED_LANGUAGES = {"english"}

# Checked against the greeting. Not an exhaustive list of world languages —
# just the ones plausibly offered by a deployment of this product, which is
# what makes the check cheap and free of false positives.
_KNOWN_LANGUAGE_WORDS = {
    "tamil", "hindi", "telugu", "kannada", "malayalam", "marathi", "bengali",
    "gujarati", "punjabi", "urdu", "spanish", "french", "german", "arabic",
    "mandarin", "chinese", "japanese", "portuguese", "russian", "english",
}


@dataclass
class ValidationReport:
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def error(self, msg: str) -> None:
        self.errors.append(msg)

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)


def _check_greeting_languages(r: ValidationReport) -> None:
    """VA-T-015 — the greeting must not offer a language the build cannot honour."""
    greeting = (config.GREETING_TEMPLATE or "").lower()
    offered = {w for w in _KNOWN_LANGUAGE_WORDS if w in greeting}
    unsupported = sorted(offered - SUPPORTED_LANGUAGES)
    if unsupported:
        r.error(
            f"GREETING_TEMPLATE offers {', '.join(unsupported)}, but this build "
            f"answers only in {'/'.join(sorted(SUPPORTED_LANGUAGES))}. A caller who "
            f"accepts the offer is answered in English anyway. This is the first "
            f"sentence of EVERY call — fix the wording in .env."
        )


def _check_shutdown_budget(r: ValidationReport) -> None:
    """VA-T-011 — the drains must finish inside the platform's kill deadline."""
    budget = config.SHUTDOWN_TOTAL_BUDGET_SEC
    deadline = config.PLATFORM_TERMINATION_GRACE_SEC

    if budget >= deadline:
        r.error(
            f"SHUTDOWN_TOTAL_BUDGET_SEC ({budget}s) is not below the platform kill "
            f"deadline ({deadline}s). The process will be SIGKILLed mid-shutdown, so "
            f"Redis and HTTP clients are never closed — connections leak on every "
            f"rolling restart. Lower the budget, or raise "
            f"terminationGracePeriodSeconds in k8s/04-backend-deployment.yaml."
        )
    elif deadline - budget < 5:
        r.warn(
            f"Only {deadline - budget:.0f}s of slack between SHUTDOWN_TOTAL_BUDGET_SEC "
            f"and the platform deadline. Client teardown needs a moment after the "
            f"drains finish."
        )

    if config.GRACEFUL_SHUTDOWN_DRAIN_SEC > budget:
        r.warn(
            f"GRACEFUL_SHUTDOWN_DRAIN_SEC ({config.GRACEFUL_SHUTDOWN_DRAIN_SEC}s) "
            f"exceeds SHUTDOWN_TOTAL_BUDGET_SEC ({budget}s), so the call drain can "
            f"never use its full window."
        )


def _check_dashboard_auth(r: ValidationReport) -> None:
    """VA-T-002 — the console must not be open to anyone who can reach it."""
    if not config.DASHBOARD_SESSION_SECRET:
        msg = (
            "DASHBOARD_SESSION_SECRET is not set, so dashboard logins cannot be "
            "issued or verified and the console is unreachable. Generate one with "
            "`openssl rand -hex 32` and use the SAME value in frontend/.env."
        )
        r.error(msg) if config.IS_PRODUCTION else r.warn(msg)
    elif len(config.DASHBOARD_SESSION_SECRET) < 32:
        r.warn(
            "DASHBOARD_SESSION_SECRET is shorter than 32 characters. It is the key "
            "that signs every dashboard session — a guessable value lets anyone mint "
            "a valid session without a password."
        )

    if not (config.DASHBOARD_API_KEY and config.DASHBOARD_ADMIN_KEY):
        r.error(
            "DASHBOARD_API_KEY / DASHBOARD_ADMIN_KEY must both be set — the backend "
            "refuses every API call without them, so the dashboard will not load."
        )


def _check_proxy_trust(r: ValidationReport) -> None:
    """VA-T-006 — rate limits are per-bucket, and the bucket must be the client."""
    if config.IS_PRODUCTION and not config.RATE_LIMIT_TRUSTED_PROXIES:
        r.warn(
            "RATE_LIMIT_TRUSTED_PROXIES is empty in production. If anything sits in "
            "front of this service (ingress, load balancer, the Next.js proxy), every "
            "request appears to come from that one address: all dashboard users share "
            f"one {config.RATE_LIMIT_DEFAULT} bucket, and the webhook limit becomes a "
            "GLOBAL ceiling on inbound call volume rather than a per-client guard."
        )


def _check_cost_rates(r: ValidationReport) -> None:
    """VA-T-007 — unset rates record $0.00 and make the product look free."""
    rates = {
        "ELEVENLABS_STT_COST_PER_MINUTE": config.ELEVENLABS_STT_COST_PER_MINUTE,
        "SARVAM_TTS_COST_PER_1K_CHARS": config.SARVAM_TTS_COST_PER_1K_CHARS,
        "ELEVENLABS_TTS_COST_PER_1K_CHARS": config.ELEVENLABS_TTS_COST_PER_1K_CHARS,
        "TWILIO_INBOUND_COST_PER_MINUTE": config.TWILIO_INBOUND_COST_PER_MINUTE,
        "TWILIO_OUTBOUND_COST_PER_MINUTE": config.TWILIO_OUTBOUND_COST_PER_MINUTE,
    }
    unset = sorted(k for k, v in rates.items() if v <= 0.0)
    if unset:
        r.warn(
            f"{len(unset)} of {len(rates)} provider cost rate(s) unset "
            f"({', '.join(unset)}) — those costs record as $0.00 and the dashboard "
            f"understates what each call really costs. Set them from your invoices; "
            f"they default to 0.0 rather than a plausible figure on purpose, because "
            f"a credible-looking wrong rate is worse than an obvious zero."
        )


def _check_db_connection_ceiling(r: ValidationReport) -> None:
    """VA-T-005 — the pool multiplies by replica count.

    database/base.py sizes pool_size AND max_overflow at MAX_CONCURRENT_CALLS,
    so one pod can hold twice that many connections. At maxReplicas the total
    can exceed what the database provider allows, and the failure appears as
    calls unable to connect — mid-conversation, under exactly the load that
    caused it.

    This cannot be fixed without knowing whether clients share a database, so
    the arithmetic is reported instead of guessed at.
    """
    per_pod = config.MAX_CONCURRENT_CALLS * 2
    worst_case = per_pod * config.MAX_REPLICAS

    if worst_case > config.DB_CONNECTION_CEILING:
        r.warn(
            f"Database connections could reach {worst_case} at full scale "
            f"({config.MAX_CONCURRENT_CALLS} calls x 2 x {config.MAX_REPLICAS} replicas), "
            f"above DB_CONNECTION_CEILING={config.DB_CONNECTION_CEILING}. Past the limit "
            f"new connections fail mid-call. Options: put PgBouncer (or Supabase's "
            f"pooler in transaction mode) in front, lower MAX_CONCURRENT_CALLS or "
            f"MAX_REPLICAS, or give each client their own database."
        )


def _check_notification_channels(r: ValidationReport) -> None:
    """Report when escalations would reach nobody.

    Not an error — a deployment may genuinely not want alerts. But the agent
    tells callers "our team will follow up", so silence here is a promise
    nothing can keep, and it should be a deliberate choice rather than an
    oversight.
    """
    if not config.ENABLE_ESCALATION_NOTIFICATIONS:
        return
    from notifications.notifier import configured_channels

    if not configured_channels():
        r.warn(
            "No escalation notification channel is configured "
            "(ESCALATION_SLACK_WEBHOOK_URL / ESCALATION_SMS_TO / "
            "ESCALATION_WEBHOOK_URL). Escalations are recorded in the database, but "
            "nobody is alerted — while the agent tells the caller their team will "
            "follow up."
        )


def validate() -> ValidationReport:
    """Run every check. Pure — no logging, no raising — so it is unit-testable."""
    r = ValidationReport()
    _check_greeting_languages(r)
    _check_shutdown_budget(r)
    _check_dashboard_auth(r)
    _check_proxy_trust(r)
    _check_cost_rates(r)
    _check_db_connection_ceiling(r)
    _check_notification_channels(r)
    return r


def validate_and_report(strict: bool = None) -> ValidationReport:
    """Called from main.py's lifespan startup.

    strict=None means "decide from ENVIRONMENT". Pass an explicit bool to
    override, which is what the tests do.
    """
    if strict is None:
        strict = config.IS_PRODUCTION

    report = validate()
    for w in report.warnings:
        logger.warning(f"CONFIG: {w}")
    for e in report.errors:
        logger.error(f"CONFIG ERROR: {e}")

    if report.errors and strict:
        raise RuntimeError(
            f"Refusing to start: {len(report.errors)} configuration error(s). See the "
            f"CONFIG ERROR lines above. Set ENVIRONMENT=development to boot anyway "
            f"while debugging locally."
        )

    if report.ok and not report.warnings:
        logger.info("Configuration validated — no errors or warnings")
    else:
        logger.info(
            f"Configuration validated with {len(report.errors)} error(s) and "
            f"{len(report.warnings)} warning(s)"
        )
    return report