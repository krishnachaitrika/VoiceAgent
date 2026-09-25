import asyncio
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from typing import Any, Dict, Optional
from google.oauth2 import service_account
from googleapiclient.discovery import build
from sqlalchemy import update
from database.base import AsyncSessionLocal
from database import crud
from database.models import Call
from events import bus as event_bus
from brain.memory import get_history, get_digit_stream
from utils.phone import (
    normalize_and_validate_phone,
    build_user_digit_stream,
    phone_supported_by_history,
)
from notifications.notifier import EscalationAlert, dispatch_escalation
from utils.privacy import mask_phone
import config

logger = logging.getLogger(__name__)

# ─── TOOL SCHEMAS (OpenAI function calling format) ────────────────────────
# search_kb removed (VA-D1 fix) — a v1/v2 leftover, bound to no node's tool
# list since orchestrator/agents.py's knowledge_node was rewritten to run
# the KB search directly rather than through an LLM-invoked tool (see its
# docstring). _tool_search_kb could never actually be reached.

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "save_lead",
            "description": (
                "Save a caller's contact details and interest as a lead in the database. "
                "Use this when the caller shares their name, phone number, or interest area."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Caller's full name"},
                    "phone": {
                        "type": "string",
                        "description": (
                            f"Caller's phone number, exactly as they said it. Must be a "
                            f"complete phone number — "
                            f"if you only caught part of it, ask the caller to repeat it "
                            f"before calling this tool rather than guessing or padding digits. "
                            f"Never mention the digit count to the caller."
                        ),
                    },
                    "email": {
                        "type": "string",
                        "description": "Caller's email address (optional)",
                    },
                    "interest": {
                        "type": "string",
                        "description": "What the caller is interested in (e.g. ServiceNow, AI, Salesforce)",
                    },
                },
                "required": ["name", "phone"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "confirm_booking_details",
            "description": (
                "Use this the FIRST time you have all four booking details "
                "(name, phone, preferred_date, preferred_time), OR any time the "
                "caller corrects one of these details. This does NOT book "
                "anything and does NOT touch the database — it only returns a "
                "confirmation line for you to read back to the caller. "
                "You must call this and get an explicit 'yes' from the caller "
                "in their NEXT reply before ever calling book_meeting."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Caller's name as understood so far"},
                    "phone": {
                        "type": "string",
                        "description": (
                            f"Caller's phone number as understood so far — must be a "
                            f"complete phone number. Never mention the digit count to the caller."
                        ),
                    },
                    "preferred_date": {
                        "type": "string",
                        "description": "Preferred date in YYYY-MM-DD format",
                    },
                    "preferred_time": {
                        "type": "string",
                        "description": "Preferred time in HH:MM (24h) format",
                    },
                },
                "required": ["name", "phone", "preferred_date", "preferred_time"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "book_meeting",
            "description": (
                "Actually books the discovery call / meeting. ONLY call this "
                "after confirm_booking_details has been called AND the caller "
                "has clearly said the details are correct (e.g. 'yes', "
                "'correct', 'that's right'). Never call this on the same turn "
                "as confirm_booking_details. Creates a Google Calendar event "
                "and saves to database."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Caller's name"},
                    "phone": {"type": "string", "description": "Caller's phone number"},
                    "preferred_date": {
                        "type": "string",
                        "description": "Preferred date in YYYY-MM-DD format",
                    },
                    "preferred_time": {
                        "type": "string",
                        "description": "Preferred time in HH:MM (24h) format",
                    },
                },
                "required": ["name", "phone", "preferred_date", "preferred_time"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "escalate",
            "description": (
                "Escalate the call to a human agent. Use when caller is frustrated, "
                "asks for a human, or when confidence in answering is low."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "reason": {
                        "type": "string",
                        "description": "Reason for escalation",
                    },
                    "transcript_snippet": {
                        "type": "string",
                        "description": "Recent part of the conversation relevant to escalation",
                    },
                    "call_id": {
                        "type": "string",
                        "description": "The call ID",
                    },
                },
                "required": ["reason", "call_id"],
            },
        },
    },
]


# ─── TOOL IMPLEMENTATIONS ──────────────────────────────────────────────────

async def execute_tool(tool_name: str, args: Dict[str, Any]) -> str:
    """Dispatch tool call to the correct handler."""
    try:
        if tool_name == "save_lead":
            return await _tool_save_lead(args)
        elif tool_name == "confirm_booking_details":
            return await _tool_confirm_booking_details(args)
        elif tool_name == "book_meeting":
            return await _tool_book_meeting(args)
        elif tool_name == "escalate":
            return await _tool_escalate(args)
        else:
            return f"Unknown tool: {tool_name}"
    except Exception as e:
        logger.error(f"Tool '{tool_name}' failed: {e}")
        return f"Tool execution failed: {str(e)}"


async def _validate_phone_or_reject(call_id: str, phone: str, tool_label: str) -> tuple[str, str]:
    """Run both phone checks used by every phone-touching tool:
      1. normalize_and_validate_phone — exact digit-count/format check.
      2. phone_supported_by_history — the number must actually trace back
         to digits the caller said in THIS call, not just be the right
         length (see utils/phone.py's phone_supported_by_history
         docstring for the real fabricated-number bug this catches).

    Returns (normalized_phone, "") on success, or ("", rejection_message)
    on failure — check the second element to decide whether to proceed.
    """
    result = normalize_and_validate_phone(phone)
    if not result.is_valid:
        logger.warning(f"[{tool_label}] Rejected invalid phone for call_id={call_id}: '{mask_phone(phone)}' -> {result.reason}")
        return "", result.reason

    try:
        history = await get_history(call_id) if call_id else []
    except Exception as e:
        logger.warning(f"[{tool_label}] Could not fetch history for call_id={call_id}: {e}")
        history = None  # fail open on infra hiccups — don't block on a Redis blip

    if history is not None:
        # Prefer the append-only digit ledger (brain/memory.py). Rebuilding the
        # stream from history alone was a REAL BUG: once the rolling summary
        # fires, old turns are replaced by a single role == "system" message, so
        # a number given early in the call vanishes from the user turns and a
        # perfectly valid phone number gets rejected. Seen live 2026-09-16 —
        # caller said "my mobile number is 6369996595" at turn 4, and by turn 8
        # the reconstructed stream was just "100" (digits of "1:00 p.m.").
        #
        # The history rebuild stays as a fallback for a session predating the
        # ledger or one that lost it to a Redis blip; whichever stream is longer
        # wins, so neither path can lose digits the other has.
        user_texts = [h.get("content", "") for h in history if h.get("role") == "user"]
        history_stream = build_user_digit_stream(user_texts)
        ledger_stream = await get_digit_stream(call_id) if call_id else ""
        digit_stream = ledger_stream if len(ledger_stream) >= len(history_stream) else history_stream
        if digit_stream and not phone_supported_by_history(result.normalized, digit_stream):
            logger.warning(
                f"[{tool_label}] Rejected phone that doesn't match call transcript for "
                f"call_id={call_id}: proposed='{result.normalized}' stream='{digit_stream}'"
            )
            return "", (
                f"the number '{result.normalized}' doesn't match what the caller actually "
                f"said in this call — it looks like some digits were guessed or padded to "
                f"reach {config.PHONE_NUMBER_DIGIT_LENGTH} digits rather than genuinely heard."
            )

    return result.normalized, ""


async def _tool_save_lead(args: Dict) -> str:
    name = args.get("name", "")
    phone = args.get("phone", "")
    email = args.get("email")
    interest = args.get("interest")
    call_id = args.get("call_id")

    # Hard validation, independent of whatever digits the model decided
    # were "enough" or "plausible" — see _validate_phone_or_reject / utils/phone.py
    # for why prompt instructions alone can't be trusted for this. A caller
    # sharing only a name/interest with no phone yet is fine (phone stays
    # empty); an actual attempted phone number that doesn't validate, or
    # doesn't trace back to what the caller actually said, must NOT be
    # silently saved.
    normalized_phone = ""
    if phone.strip():
        normalized_phone, rejection = await _validate_phone_or_reject(call_id, phone, "save_lead")
        if rejection:
            return (
                f"PHONE NUMBER NOT SAVED — {rejection} Ask the caller "
                f"to repeat their full phone number, then call save_lead again "
                f"with the complete number. Do not tell the caller it was saved."
            )

    logger.info(f"[save_lead] Upserting lead for call_id={call_id}: {name} / {mask_phone(normalized_phone)}")
    async with AsyncSessionLocal() as db:
        await crud.upsert_lead(
            db,
            call_id=call_id,
            name=name,
            phone=normalized_phone,
            email=email,
            interest=interest,
        )
    await event_bus.publish("lead_created", {"name": name, "interest": interest}, call_id=call_id)
    return f"Lead saved successfully for {name}."


def _format_meeting_readback(name: str, phone: str, preferred_date: str, preferred_time: str) -> str:
    """Build a natural-language confirmation line from raw booking fields."""
    try:
        dt = datetime.strptime(f"{preferred_date} {preferred_time}", "%Y-%m-%d %H:%M")
        when = dt.strftime("%A, %d %B at %I:%M %p")
    except ValueError:
        when = f"{preferred_date} at {preferred_time}"
    return f"{name}, {phone}, {when}"


async def _tool_confirm_booking_details(args: Dict) -> str:
    """
    Pure read-back tool — does NOT write to the database or calendar.
    Returns a confirmation string the agent should speak to the caller,
    then WAIT for the caller's next reply before calling book_meeting.
    """
    name = args.get("name", "")
    phone = args.get("phone", "")
    preferred_date = args.get("preferred_date", "")
    preferred_time = args.get("preferred_time", "")
    call_id = args.get("call_id")

    # Same hard check as save_lead — never read back (and thereby implicitly
    # confirm) a phone number that isn't actually a complete, genuine number.
    # A caller agreeing to an incomplete or fabricated readback is how bad
    # numbers used to slip all the way through to book_meeting.
    normalized_phone, rejection = await _validate_phone_or_reject(call_id, phone, "confirm_booking_details")
    if rejection:
        return (
            f"CANNOT CONFIRM YET — {rejection} Ask the caller to "
            f"repeat their full phone number before calling "
            f"confirm_booking_details again. Do not read back or confirm "
            f"an incomplete or uncertain number."
        )

    readback = _format_meeting_readback(name, normalized_phone, preferred_date, preferred_time)
    logger.info(f"[confirm_booking_details] Read-back to caller: {readback}")

    return (
        f"DETAILS TO CONFIRM (do not book yet — read this back to the caller "
        f"and wait for their yes/no): {readback}. "
        f"If the caller confirms, call book_meeting with these exact details "
        f"(phone: {normalized_phone}). "
        f"If the caller corrects anything, call confirm_booking_details again "
        f"with the corrected value(s)."
    )


def _validate_meeting_datetime(preferred_date: str, preferred_time: str) -> tuple[Optional[datetime], Optional[str]]:
    """
    Parse + validate a proposed meeting time (VA-C7 fix). Returns
    (naive_local_datetime, None) on success — naive because it's meant to
    be handed straight to the Google Calendar API alongside a separate
    timeZone field, exactly as book_meeting already does — or
    (None, rejection_reason) if it fails to parse, is in the past, is
    further out than config.MEETING_MAX_DAYS_AHEAD, or falls outside
    config.MEETING_BUSINESS_HOUR_START/END local business hours.

    "Local" here is config.GOOGLE_CALENDAR_TIMEZONE — the business's own
    timezone, not the caller's — since that's the only timezone this
    codebase actually knows: GREETING_TEMPLATE and the whole system prompt
    already assume a single-region deployment.
    """
    try:
        naive_dt = datetime.strptime(f"{preferred_date} {preferred_time}", "%Y-%m-%d %H:%M")
    except ValueError:
        return None, (
            f"'{preferred_date} {preferred_time}' isn't a valid date/time "
            f"(expected YYYY-MM-DD and HH:MM, 24-hour). Ask the caller to "
            f"restate the date and time."
        )

    tz = ZoneInfo(config.GOOGLE_CALENDAR_TIMEZONE)
    aware_dt = naive_dt.replace(tzinfo=tz)
    now = datetime.now(tz)

    if aware_dt <= now:
        return None, (
            f"'{preferred_date} {preferred_time}' is in the past (current time is "
            f"{now.strftime('%Y-%m-%d %H:%M')} {config.GOOGLE_CALENDAR_TIMEZONE}). "
            f"Ask the caller for a future date/time — do not silently pick one yourself."
        )

    if aware_dt > now + timedelta(days=config.MEETING_MAX_DAYS_AHEAD):
        return None, (
            f"'{preferred_date} {preferred_time}' is more than "
            f"{config.MEETING_MAX_DAYS_AHEAD} days away, which is further out than "
            f"meetings can be booked. Ask the caller for a sooner date."
        )

    if not (config.MEETING_BUSINESS_HOUR_START <= aware_dt.hour < config.MEETING_BUSINESS_HOUR_END):
        return None, (
            f"{preferred_time} is outside business hours "
            f"({config.MEETING_BUSINESS_HOUR_START:02d}:00–{config.MEETING_BUSINESS_HOUR_END:02d}:00 "
            f"{config.GOOGLE_CALENDAR_TIMEZONE}). Ask the caller for a time within business hours."
        )

    return naive_dt, None


# ─── Google Calendar helper ────────────────────────────────────────────────
# Credentials are parsed ONCE and cached at module scope. The previous code
# re-read the service-account JSON from disk and re-parsed the RSA key on every
# single booking — a file read plus an asymmetric key parse, on the blocking
# path, inside the event loop.
_CALENDAR_CREDS = None
_CALENDAR_CREDS_LOADED = False


def _insert_calendar_event_blocking(event_body: dict):
    """Synchronous Calendar insert. MUST only ever be called via
    asyncio.to_thread — calling it directly from async code blocks the event
    loop and stalls every concurrent call.

    cache_discovery=False matters: without it the client tries to write a
    discovery cache to disk, which logs noisily and fails outright on a
    read-only container filesystem."""
    global _CALENDAR_CREDS, _CALENDAR_CREDS_LOADED
    if not _CALENDAR_CREDS_LOADED:
        _CALENDAR_CREDS_LOADED = True
        _CALENDAR_CREDS = service_account.Credentials.from_service_account_file(
            config.GOOGLE_CREDENTIALS_JSON,
            scopes=["https://www.googleapis.com/auth/calendar"],
        )
    service = build("calendar", "v3", credentials=_CALENDAR_CREDS, cache_discovery=False)
    created = service.events().insert(
        calendarId=config.GOOGLE_CALENDAR_ID,
        body=event_body,
    ).execute()
    return created.get("id")


async def _tool_book_meeting(args: Dict) -> str:
    name = args.get("name", "")
    phone = args.get("phone", "")
    preferred_date = args.get("preferred_date", "")
    preferred_time = args.get("preferred_time", "09:00")
    call_id = args.get("call_id")

    # Final defense-in-depth check — confirm_booking_details should already
    # have caught an invalid or fabricated number, but book_meeting must
    # never itself trust the model's argument as the last line of defense
    # (e.g. if the caller "corrected" the number after confirming and the
    # model called book_meeting directly instead of re-confirming).
    normalized_phone, rejection = await _validate_phone_or_reject(call_id, phone, "book_meeting")
    if rejection:
        return (
            f"BOOKING BLOCKED — {rejection} Ask the caller to "
            f"repeat their full phone number, call confirm_booking_details "
            f"again with the corrected number, get their confirmation, and "
            f"only then call book_meeting."
        )
    phone = normalized_phone

    logger.info(f"[book_meeting] Booking for {name} on {preferred_date} at {preferred_time}")

    # VA-C7 fix: this used to accept any parseable datetime with no other
    # check, and silently booked "now" instead of rejecting one that failed
    # to parse at all — the only defence against a nonsense booking (a real
    # 2023 booking is the confirmed past incident) was the model reading
    # "today" correctly, which had already failed once. meeting_dt below is
    # naive-local-in-GOOGLE_CALENDAR_TIMEZONE, same as before — required
    # as-is for the Calendar API call further down.
    meeting_dt, rejection = _validate_meeting_datetime(preferred_date, preferred_time)
    if rejection:
        return f"BOOKING BLOCKED — {rejection}"

    calendar_event_id: Optional[str] = None

    # Try Google Calendar
    try:
        creds_path = config.GOOGLE_CREDENTIALS_JSON
        import os
        if os.path.exists(creds_path):
            event_body = {
                "summary": f"{config.COMPANY_NAME} Discovery Call — {name}",
                "description": f"Discovery call with {name} ({phone})",
                "start": {
                    "dateTime": meeting_dt.isoformat(),
                    "timeZone": config.GOOGLE_CALENDAR_TIMEZONE,
                },
                "end": {
                    "dateTime": (meeting_dt + timedelta(minutes=30)).isoformat(),
                    "timeZone": config.GOOGLE_CALENDAR_TIMEZONE,
                },
            }
            # EVENT-LOOP BLOCKING FIX:
            # google-api-python-client is SYNCHRONOUS (httplib2 underneath).
            # Calling build() and .execute() directly inside this async function
            # blocked the entire asyncio event loop for the whole Google
            # round-trip — measured at ~5s in a live call log (13:19:10 ->
            # 13:19:15, 7.8s total turn latency).
            #
            # In a voice server that is severe and non-obvious: while those calls
            # are in flight EVERY OTHER LIVE CALL on this worker stops. No audio
            # forwarded to Twilio, no STT chunks sent, no barge-in detected, no
            # WebSocket frames read. Ten concurrent callers all go silent because
            # one of them booked a meeting.
            #
            # to_thread moves it off the loop; wait_for bounds it so a hung
            # Google request degrades one booking instead of stalling the turn.
            calendar_event_id = await asyncio.wait_for(
                asyncio.to_thread(_insert_calendar_event_blocking, event_body),
                timeout=config.GOOGLE_CALENDAR_TIMEOUT_SEC,
            )
            logger.info(f"[book_meeting] Calendar event created: {calendar_event_id}")
    except asyncio.TimeoutError:
        logger.error(
            f"[book_meeting] Google Calendar timed out after "
            f"{config.GOOGLE_CALENDAR_TIMEOUT_SEC}s — saving to DB only"
        )
    except Exception as e:
        logger.warning(f"[book_meeting] Google Calendar failed: {e}. Saving to DB only.")

    # Save to database — upsert by call_id so this doesn't create a second
    # lead row on top of whatever save_lead already created for this call,
    # and upsert the meeting by lead_id so a corrected booking updates the
    # same row instead of adding a duplicate.
    #
    # meeting_dt stays naive-local-in-GOOGLE_CALENDAR_TIMEZONE, unconverted
    # — same convention as before this fix (see database/models.py and
    # api/dashboard.py's _utc_iso docstring, which explains this is
    # intentional: it's the caller's literal spoken appointment time, and
    # converting it would break the dashboard's display of it).
    call_id = args.get("call_id")
    async with AsyncSessionLocal() as db:
        lead = await crud.upsert_lead(
            db,
            call_id=call_id,
            name=name,
            phone=phone,
            email=None,
            interest="Discovery Call",
        )
        await crud.upsert_meeting(
            db,
            lead_id=lead.id,
            meeting_datetime=meeting_dt,
            calendar_event_id=calendar_event_id,
        )

    date_str = meeting_dt.strftime("%A, %d %B %Y at %I:%M %p")
    await event_bus.publish(
        "meeting_booked", {"name": name, "datetime": meeting_dt.isoformat()}, call_id=call_id
    )
    return f"Meeting booked for {name} on {date_str}. The team will call you at {phone}."


async def _tool_escalate(args: Dict) -> str:
    reason = args.get("reason", "Caller requested escalation")
    transcript_snippet = args.get("transcript_snippet", "")
    call_id = args.get("call_id")

    logger.info(f"[escalate] Reason: {reason}")
    async with AsyncSessionLocal() as db:
        await crud.upsert_escalation(
            db,
            call_id=call_id,
            reason=reason,
            transcript_snippet=transcript_snippet,
        )
        # Read the caller's number while this session is already open — the
        # escalate tool's own arguments do not carry it, and the notification
        # needs it to say who is waiting.
        caller_phone = "unknown"
        if call_id:
            await db.execute(
                update(Call).where(Call.id == call_id).values(status="escalated")
            )
            await db.commit()
            call_row = await db.get(Call, call_id)
            if call_row is not None and call_row.phone_number:
                caller_phone = call_row.phone_number

    await event_bus.publish("escalation_created", {"reason": reason}, call_id=call_id)

    # THE FIX: until now this wrote a row, flipped the status, published an
    # event — and told no human, while the line returned below promises the
    # caller that someone will follow up.
    #
    # Fire-and-forget by design: the caller's turn never waits on a Slack
    # round-trip, a failing channel cannot affect the call, and with no channel
    # configured this returns immediately. The database row remains the source
    # of truth; this is delivery on top of it.
    dispatch_escalation(
        EscalationAlert(
            call_id=call_id or "unknown",
            reason=reason,
            phone_number=caller_phone,
            transcript_snippet=transcript_snippet,
        )
    )
    return "Escalated successfully. Our team will follow up with you shortly."