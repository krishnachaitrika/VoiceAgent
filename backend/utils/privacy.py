"""
utils/privacy.py — mask PII before it reaches a log line (VA-B5 fix).

Masking previously happened only at the dashboard API boundary
(api/dashboard.py's _mask_phone, applied on read) — the log stream itself
carried full phone numbers at info level from api/twilio_webhook.py,
api/websocket.py, voice/stream.py and brain/tools.py. Logs typically ship
to a less-restricted system (a log aggregator, stdout captured by whatever
runs the container) than the database itself, so this is the higher-risk
copy of the same data to close first. The database column stays unmasked
— the app needs the real number to place the call/meeting — this only
governs what gets written to the log stream.
"""
from __future__ import annotations


def mask_phone(phone: str) -> str:
    """'+91 9876543210' -> '+91 987XXXXXX'. Keeps enough to correlate log
    lines to a call without exposing the full number."""
    if not phone or len(phone) < 6:
        return phone
    visible = phone[:-5]
    return visible + "XXXXX"


def mask_email(email: str) -> str:
    """'jane.doe@example.com' -> 'ja***@example.com'."""
    if not email or "@" not in email:
        return email
    local, _, domain = email.partition("@")
    if len(local) <= 2:
        return f"{local[0]}***@{domain}" if local else email
    return f"{local[:2]}***@{domain}"
