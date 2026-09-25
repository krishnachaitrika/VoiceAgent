"""
twilio_auth.py — Twilio webhook signature validation + Media Stream URL
signing (VA-B2 fix).

Both telephony entry points had zero authentication: api/twilio_webhook.py
never checked X-Twilio-Signature, and api/websocket.py accepted any
WebSocket connection with no verification it came from Twilio at all. A
stranger who learned either URL could forge calls that spend OpenAI/
ElevenLabs/Sarvam credit, write fake call/lead rows, and reach book_meeting
to create real calendar events — with no cost ceiling and no attribution.

Twilio's Media Streams WebSocket has no signature mechanism of its own (only
the initial webhook POST is signed), so the fix here is exactly the report's
suggested approach: "sign the stream URL". Once /incoming-call validates a
real Twilio signature, it mints a short-lived HMAC token tied to that call's
CallSid and puts it in the wss:// URL handed back in the TwiML; the
WebSocket handler verifies that token before accepting the connection.

Kept in its own module (not duplicated in twilio_webhook.py and
websocket.py) so the two sides of the HMAC can't drift apart, and so it can
be unit-tested directly without a live Twilio account or a real WebSocket.
"""
import hashlib
import hmac
import time
from typing import Optional

from twilio.request_validator import RequestValidator

import config


def validate_twilio_signature(url: str, params: dict, signature: Optional[str]) -> bool:
    """Validate an inbound Twilio webhook request. Fails closed: no
    TWILIO_AUTH_TOKEN configured, or no signature header at all, is always
    a rejection — never treated as "skip validation"."""
    if not config.TWILIO_AUTH_TOKEN or not signature:
        return False
    validator = RequestValidator(config.TWILIO_AUTH_TOKEN)
    return validator.validate(url, params, signature)


def _stream_token_payload(call_sid: str, expiry: int) -> bytes:
    return f"{call_sid}:{expiry}".encode("utf-8")


def issue_stream_token(call_sid: str, ttl_seconds: Optional[int] = None) -> tuple[str, int]:
    """Mint a (token, expiry_unix_ts) pair for a CallSid that has just been
    validated by a real Twilio webhook request. Signed with
    TWILIO_AUTH_TOKEN — a secret only this backend and Twilio know — rather
    than introducing a separate signing secret to manage."""
    ttl = ttl_seconds if ttl_seconds is not None else config.STREAM_TOKEN_TTL_SECONDS
    expiry = int(time.time()) + ttl
    token = hmac.new(
        config.TWILIO_AUTH_TOKEN.encode("utf-8"),
        _stream_token_payload(call_sid, expiry),
        hashlib.sha256,
    ).hexdigest()
    return token, expiry


def verify_stream_token(call_sid: str, expiry: str, token: str) -> bool:
    """Verify a token minted by issue_stream_token. Fails closed on any
    malformed input (missing/non-numeric expiry, empty token, no auth token
    configured) rather than raising — the WebSocket handler treats any
    False here as "reject the connection"."""
    if not config.TWILIO_AUTH_TOKEN or not call_sid or not token:
        return False
    try:
        expiry_int = int(expiry)
    except (TypeError, ValueError):
        return False
    if expiry_int < int(time.time()):
        return False
    expected = hmac.new(
        config.TWILIO_AUTH_TOKEN.encode("utf-8"),
        _stream_token_payload(call_sid, expiry_int),
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, token)
