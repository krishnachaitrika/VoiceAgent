import logging
from fastapi import APIRouter, HTTPException, Request, Response
from twilio.twiml.voice_response import VoiceResponse, Connect, Stream
import config
from twilio_auth import validate_twilio_signature, issue_stream_token
from rate_limit import limiter
from utils.privacy import mask_phone

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/incoming-call")
@limiter.limit(config.RATE_LIMIT_TWILIO_WEBHOOK)
async def incoming_call(request: Request) -> Response:
    """
    Twilio webhook — called when someone dials the configured phone number.
    Returns TwiML that streams the call audio to our WebSocket endpoint.
    """
    form = await request.form()
    from_number = form.get("From", "Unknown")
    call_sid = form.get("CallSid", "unknown")
    # Twilio sends this on every webhook and the code previously ignored it.
    # "inbound" when someone dials us, "outbound-api" when we place the call
    # (scripts/make_test_call.py, or any future outbound campaign). Normalised
    # to two values because the exact outbound variant differs by how the call
    # was created ("outbound-api", "outbound-dial") and nothing downstream
    # cares which.
    raw_direction = (form.get("Direction", "") or "").lower()
    direction = "outbound" if raw_direction.startswith("outbound") else "inbound"

    signature = request.headers.get("X-Twilio-Signature")
    request_url = f"https://{_get_host(request)}{request.url.path}"
    if not validate_twilio_signature(request_url, dict(form), signature):
        logger.warning(
            f"Rejected /incoming-call webhook with invalid Twilio signature "
            f"(SID: {call_sid}, from: {request.client})"
        )
        raise HTTPException(status_code=403, detail="Invalid signature")

    logger.info(
        f"{direction.capitalize()} call from {mask_phone(from_number)} | SID: {call_sid}"
    )

    # Build TwiML. The stream URL carries a short-lived signed token tied to
    # this CallSid (see twilio_auth.py) — Twilio's Media Streams WebSocket
    # has no signature mechanism of its own, so this is what lets
    # api/websocket.py reject a connection that didn't come from a call this
    # webhook actually validated.
    token, expiry = issue_stream_token(call_sid)
    twiml = VoiceResponse()
    connect = Connect()
    # CREDENTIAL IN THE PATH, NOT A QUERY STRING.
    #
    # The "?sid=...&exp=...&token=..." form caused every real Twilio call to be
    # rejected: the three values never reached api/websocket.py, so query.get()
    # returned empty strings and verify_stream_token failed closed. The HMAC was
    # correct throughout — the transport was losing the values.
    #
    # A query string is the fragile part of a URL: it can be dropped by a proxy
    # or tunnel and the request still ARRIVES, just with silently blank
    # credentials, which is indistinguishable from a forged connection in the
    # logs. A path segment is part of the HTTP request line, so a hop that loses
    # one produces a 404 instead — a far more diagnosable failure.
    #
    # All three values are URL-safe already (CallSid alphanumeric, expiry an
    # integer, token lowercase hex from hexdigest()), so no encoding is needed.
    stream_url = f"wss://{_get_host(request)}/stream/{call_sid}/{expiry}/{token}"
    stream = Stream(url=stream_url)

    # REAL-BUG FIX: Twilio's Media Streams "start" event does NOT include
    # the caller's phone number or CallSid automatically — without these
    # explicit <Parameter> elements, api/websocket.py had no way to learn
    # who was calling, and every single Call record in the database was
    # permanently stored with phone_number="unknown", regardless of who
    # actually called. <Parameter> values are echoed back verbatim inside
    # the WebSocket "start" event's start.customParameters object (see
    # voice/stream.py's handling of the "start" event), which is the only
    # way this data reaches the stream handler at all.
    stream.parameter(name="from", value=from_number)
    stream.parameter(name="callSid", value=call_sid)
    # Direction has to travel this way too: the media-stream WebSocket carries
    # no Twilio metadata of its own, so anything the stream handler needs must
    # be passed explicitly as a <Parameter> (same mechanism as "from").
    stream.parameter(name="direction", value=direction)

    connect.append(stream)
    twiml.append(connect)

    return Response(content=str(twiml), media_type="application/xml")


def _get_host(request: Request) -> str:
    """Get the public-facing host from ngrok or the request host."""
    if config.NGROK_URL:
        return config.NGROK_URL.replace("https://", "").replace("http://", "").rstrip("/")
    return request.headers.get("host", "localhost:8000")