"""
observability/error_tracking.py — optional crash reporting.

THE GAP

There is no error tracking at all. If the agent starts failing at 3am, the
first anyone knows is a complaint. Worse, several handlers in this codebase
swallow the detail:

    except Exception as e:
        logger.error(f"... {e}")

That records the message and discards the traceback, so "Tool 'book_meeting'
failed: 'No time zone found with key Asia/Kolkata'" arrives with no indication
of WHERE it came from. Sentry captures the stack regardless of how the handler
logs it.

DESIGN CONSTRAINT: CANNOT AFFECT A WORKING DEPLOYMENT

With SENTRY_DSN unset — the default — every function here returns immediately
and the SDK is never even imported. A deployment that pulls this code and
changes nothing behaves exactly as it does today. If sentry-sdk is not
installed, that is also fine: the import is guarded and produces one warning,
not a crash.

When it IS enabled, the overhead is a sampled background HTTP send on the
error path only. Nothing is added to a normal turn.

PII — THIS MATTERS MORE HERE THAN USUAL

Call transcripts contain names, phone numbers and whatever the caller chose to
say. Sending those to a third-party service would be a data-protection
problem, so:

  send_default_pii   is False, so the SDK never attaches request bodies,
                     headers or user identifiers on its own
  before_send        strips anything that looks like a phone number from the
                     message before it leaves the process

The second is the important one. The first stops the SDK volunteering data;
the second catches what our OWN log lines put in an exception message.
"""
import logging
import re
from typing import Any, Optional

import config

logger = logging.getLogger(__name__)

_enabled = False

# Matches a run of 10+ digits, optionally with +, spaces or hyphens — which is
# every way a caller's number appears in this codebase's log lines.
_PHONE_RE = re.compile(r"\+?\d[\d\s\-()]{8,}\d")


def _scrub(text: str) -> str:
    return _PHONE_RE.sub("[phone-redacted]", text)


def _before_send(event: dict, hint: dict) -> Optional[dict]:
    """Last chance to redact before an event leaves the process.

    Runs on every event. Deliberately conservative: it would rather mangle a
    harmless number in a message than let a caller's phone number reach a
    third party.
    """
    try:
        for entry in event.get("exception", {}).get("values", []):
            if entry.get("value"):
                entry["value"] = _scrub(entry["value"])

        if event.get("logentry", {}).get("message"):
            event["logentry"]["message"] = _scrub(event["logentry"]["message"])

        if isinstance(event.get("message"), str):
            event["message"] = _scrub(event["message"])
    except Exception:
        # A scrubbing bug must not become a crash in the error path — the one
        # place where a second failure is hardest to diagnose. Drop the event
        # rather than risk sending something unredacted.
        return None
    return event


def init_error_tracking() -> bool:
    """Initialise Sentry if configured. Returns True when enabled.

    Never raises. A missing DSN, a missing package or a bad DSN all degrade to
    "no error tracking", which is exactly where the project is today.
    """
    global _enabled

    if not config.SENTRY_DSN:
        return False

    try:
        import sentry_sdk
        from sentry_sdk.integrations.asyncio import AsyncioIntegration
        from sentry_sdk.integrations.fastapi import FastApiIntegration
        from sentry_sdk.integrations.logging import LoggingIntegration
    except ImportError:
        logger.warning(
            "SENTRY_DSN is set but sentry-sdk is not installed — error tracking is "
            "OFF. Add sentry-sdk to requirements.txt and reinstall."
        )
        return False

    try:
        sentry_sdk.init(
            dsn=config.SENTRY_DSN,
            environment=config.ENVIRONMENT,
            release=config.APP_VERSION,
            integrations=[
                AsyncioIntegration(),
                FastApiIntegration(),
                # Capture logger.error and above as events, but only breadcrumb
                # INFO. Capturing INFO would send every normal turn to Sentry
                # and bury the actual failures.
                LoggingIntegration(level=logging.INFO, event_level=logging.ERROR),
            ],
            # Sampled, not every transaction: performance traces on a voice
            # workload are high-volume and mostly identical. 5% is enough to
            # spot a regression without adding meaningful overhead.
            traces_sample_rate=config.SENTRY_TRACES_SAMPLE_RATE,
            send_default_pii=False,
            before_send=_before_send,
            max_breadcrumbs=50,
        )
        _enabled = True
        logger.info(
            f"Error tracking enabled (environment={config.ENVIRONMENT}, "
            f"release={config.APP_VERSION}, traces={config.SENTRY_TRACES_SAMPLE_RATE})"
        )
        return True
    except Exception as e:
        logger.warning(f"Could not initialise error tracking: {e}")
        return False


def is_enabled() -> bool:
    return _enabled


def set_call_context(call_id: Optional[str]) -> None:
    """Tag subsequent events with the call they belong to.

    A no-op when disabled. The call id is the CallSid, which is already in
    every log line — it is the key that lets you line up a Sentry event with
    the transcript and the cost record for the same call.
    """
    if not _enabled:
        return
    try:
        import sentry_sdk

        sentry_sdk.set_tag("call_id", call_id)
    except Exception:
        pass


def capture_exception(error: BaseException, **context: Any) -> None:
    """Report an exception a handler has already caught.

    For the places that deliberately swallow an error to keep the call alive —
    a failed calendar insert, a TTS provider erroring mid-turn. Those must not
    break the call, but they should not vanish either.
    """
    if not _enabled:
        return
    try:
        import sentry_sdk

        with sentry_sdk.push_scope() as scope:
            for key, value in context.items():
                scope.set_extra(key, value)
            sentry_sdk.capture_exception(error)
    except Exception:
        pass