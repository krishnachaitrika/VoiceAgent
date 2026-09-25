import asyncio
from collections import Counter
import logging
from fastapi import APIRouter, WebSocket, BackgroundTasks
from database.base import AsyncSessionLocal
from database import crud
from voice.stream import handle_twilio_stream
from events import bus as event_bus
from analytics.sentiment import analyze_call
from billing.twilio_reconcile import dispatch_reconciliation
from twilio_auth import verify_stream_token
from utils.privacy import mask_phone
import config

logger = logging.getLogger(__name__)
router = APIRouter()

# Concurrency cap (VA-B3 fix) — nothing previously bounded the number of
# simultaneous calls, so a burst of connections could exhaust the DB pool
# (database/base.py) and, combined with VA-B2's forged-call risk, run up an
# unbounded provider bill. Acquired below before a Call row is created or
# any provider is touched, so a call rejected here costs nothing.
_call_semaphore = asyncio.Semaphore(config.MAX_CONCURRENT_CALLS)

# Graceful shutdown (VA-C6 fix) — main.py's lifespan shutdown used to close
# Redis and the pooled HTTP clients only; live WebSocket sessions were never
# tracked or drained, so a rolling deploy or HPA scale-in cut active callers
# off mid-sentence and lost their transcript/cost (both are written only in
# handle_twilio_stream's finally block, which never got a chance to run).
# begin_shutdown() stops NEW connections from being accepted; wait_for_drain
# gives in-flight calls a bounded window to finish naturally before the
# process actually exits.
# VA-T-004 FIX — A SET LOSES COUNT WHEN ONE CallSid HAS TWO CONNECTIONS.
#
# call_id is the Twilio CallSid (VA-C5), which is correct for keying the
# database. But Twilio RETRIES /incoming-call on a webhook timeout, and each
# attempt mints its own valid stream token for the SAME CallSid — so two live
# connections can legitimately share one id.
#
# With a set, the first of those two to end called discard() and removed the
# id outright. wait_for_drain then saw an empty set, concluded the pod was
# idle, and let the process exit while the second caller was still mid
# sentence — losing their transcript and cost, which are only written in
# handle_twilio_stream's finally block.
#
# A Counter tracks DEPTH rather than presence: two connections increment to 2,
# one ending decrements to 1, and the drain keeps waiting until it reaches 0.
_active_calls: "Counter[str]" = Counter()
_shutting_down = False


def begin_shutdown() -> None:
    global _shutting_down
    _shutting_down = True


async def wait_for_drain(deadline_sec: float) -> None:
    """Poll until every in-flight call has finished, or deadline_sec runs
    out — whichever comes first. Called from main.py's lifespan shutdown,
    before Redis/HTTP clients are torn down, so a call still finishing up
    in this window still has those available."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + deadline_sec
    while _active_calls and loop.time() < deadline:
        await asyncio.sleep(0.5)
    if _active_calls:
        logger.warning(
            f"Graceful shutdown deadline ({deadline_sec}s) reached with "
            f"{sum(_active_calls.values())} connection(s) across "
            f"{len(_active_calls)} call(s) still active — they will be cut off."
        )
    else:
        logger.info("Graceful shutdown: all calls drained.")


@router.websocket("/stream/{sid}/{exp}/{token}")
async def stream_websocket(websocket: WebSocket, sid: str, exp: str, token: str) -> None:
    """
    WebSocket endpoint for Twilio media streams.
    Twilio connects here to stream call audio bidirectionally.

    Twilio's Media Streams protocol has no signature mechanism of its own —
    unlike the webhook POST, this connection can't be verified with
    X-Twilio-Signature. Instead it must carry the sid/exp/token query
    params minted by api/twilio_webhook.py (see twilio_auth.py) for a call
    that webhook actually validated; anything else is rejected before the
    handshake completes.
    """
    call_sid = sid
    if not verify_stream_token(call_sid, exp, token):
        logger.warning(f"Rejected /stream connection with invalid or missing stream token "
                        f"(client: {websocket.client})")
        await websocket.close(code=1008)
        return

    if _shutting_down:
        logger.warning(f"Rejected /stream connection — server is shutting down (client: {websocket.client})")
        await websocket.close(code=1012)  # 1012 Service Restart
        return

    if _call_semaphore.locked():
        logger.warning(
            f"Rejected /stream connection — at MAX_CONCURRENT_CALLS "
            f"({config.MAX_CONCURRENT_CALLS}) (client: {websocket.client})"
        )
        await websocket.close(code=1013)  # 1013 Try Again Later
        return
    # No `await` between the locked() check above and this acquire — the
    # event loop can't switch to another connection in between, so the
    # permit this just confirmed free is still free.
    await _call_semaphore.acquire()

    # VA-C5 fix: call_id used to be a fresh uuid4 per WebSocket connection,
    # unrelated to the call it belonged to. Twilio re-POSTs /incoming-call
    # on a webhook timeout, and each attempt mints its own valid stream
    # token (see api/twilio_webhook.py) for the SAME CallSid — a retry's
    # random uuid4 produced a second, unrelated Call row and split one
    # real conversation's turns across two transcripts. call_sid is
    # already verified above (it's the token's own signed subject), so
    # using it directly as call_id makes every table naturally keyed on
    # the one real call, and create_call/save_transcript (database/crud.py)
    # are now idempotent on it for the case this connection actually is a
    # genuine duplicate.
    call_id = call_sid
    _active_calls[call_id] += 1

    logger.info(f"[{call_id}] /stream WebSocket route ENTERED")

    # REAL-BUG FIX: phone_number used to be a fixed "unknown" local variable
    # here that was never updated — every Call record was permanently
    # stored with no real caller ID. voice/stream.py now extracts the
    # actual number from Twilio's Stream customParameters (see
    # api/twilio_webhook.py) and passes it into on_call_start below the
    # moment the "start" event arrives, once per call, before any audio
    # processing begins — this has no effect on per-turn latency.
    # Captured here so on_call_end can price Twilio minutes correctly — the
    # two handlers are separate closures and the direction is only known at
    # call start.
    call_direction = "inbound"

    async def on_call_start(
        cid: str, phone_number: str = "unknown", direction: str = "inbound"
    ) -> None:
        nonlocal call_direction
        call_direction = direction or "inbound"
        async with AsyncSessionLocal() as db:
            await crud.create_call(
                db, call_id=cid, phone_number=phone_number, direction=call_direction
            )
        logger.info(f"[{cid}] Call record created in DB | Caller: {mask_phone(phone_number)}")
        await event_bus.publish("call_started", {"phone_number": phone_number}, call_id=cid)

    async def on_call_end(
        call_id: str,
        duration: int,
        openai_cost: float,
        sarvam_cost: float,
        turns: list,
        full_text: str,
    ) -> None:
        # VA-C2 fix: ElevenLabs STT (voice/stt.py) runs continuously for the
        # whole call and was never metered at all — unlike TTS, its cost
        # isn't tied to any one turn, so call duration (already computed by
        # the caller) is the natural unit to bill it against, added here
        # rather than threaded through voice/stream.py. `sarvam_cost` (the
        # param name predates ElevenLabs — see database/models.py) is really
        # "speech cost" at this point: TTS (whichever provider handled it,
        # see StreamingTTSSession.cost_usd) plus this STT figure.
        stt_cost = (duration / 60) * config.ELEVENLABS_STT_COST_PER_MINUTE

        # Twilio was the last untracked provider — no config, no code, roughly
        # 20% of a short call's real cost sitting invisible.
        #
        # Twilio bills inbound voice per minute and ROUNDS UP, so a 57-second
        # call costs a full minute. `-(-x // 60)` is integer ceiling division;
        # it avoids the float rounding math.ceil(duration/60) can hit on exact
        # boundaries (a true 120-second call must bill 2 minutes, not 3).
        billable_minutes = -(-duration // 60) if duration > 0 else 0
        twilio_rate = (
            config.TWILIO_OUTBOUND_COST_PER_MINUTE
            if call_direction == "outbound"
            else config.TWILIO_INBOUND_COST_PER_MINUTE
        )
        twilio_cost = billable_minutes * twilio_rate

        # The incoming `sarvam_cost` parameter is really the TTS figure —
        # StreamingTTSSession.cost_usd() for whichever provider handled it.
        # Named for history, not accuracy; see database/models.py.
        tts_cost = sarvam_cost
        speech_cost = tts_cost + stt_cost   # what the legacy sarvam_cost column stores
        async with AsyncSessionLocal() as db:
            await crud.update_call(
                db,
                call_id=call_id,
                duration_seconds=duration,
                status="completed",
                openai_cost=openai_cost,
                sarvam_cost=speech_cost,     # legacy combined column, unchanged
                stt_cost=stt_cost,           # ElevenLabs Scribe
                tts_cost=tts_cost,           # Sarvam or ElevenLabs
                twilio_cost=twilio_cost,     # inbound voice minutes
            )
            await crud.save_transcript(db, call_id=call_id, full_text=full_text, turns=turns)
        total_cost = openai_cost + speech_cost + twilio_cost
        logger.info(
            f"[{call_id}] Call record updated. Duration={duration}s "
            f"total=${total_cost:.5f} "
            f"(openai=${openai_cost:.5f} stt=${stt_cost:.5f} "
            f"tts=${tts_cost:.5f} twilio=${twilio_cost:.5f} [{call_direction}])"
        )

        # Replace the estimate with what Twilio actually charged, once it has
        # rated the call. Fire-and-forget: the call is over, nothing waits.
        dispatch_reconciliation(call_id, twilio_cost)

        await event_bus.publish(
            "call_ended",
            {
                "duration": duration,
                "openai_cost": openai_cost,
                "sarvam_cost": speech_cost,
                "stt_cost": stt_cost,
                "tts_cost": tts_cost,
                "twilio_cost": twilio_cost,
                "total_cost": total_cost,
                "direction": call_direction,
            },
            call_id=call_id,
        )

        # v3+ post-call sentiment analysis — fire-and-forget, off the
        # latency path entirely (call has already ended by this point).
        asyncio.create_task(analyze_call(call_id, full_text))

    try:
        await handle_twilio_stream(
            websocket=websocket,
            call_id=call_id,
            on_call_start=on_call_start,
            on_call_end=on_call_end,
        )
    except Exception:
        logger.exception(f"[{call_id}] handle_twilio_stream CRASHED")
        raise
    finally:
        _call_semaphore.release()
        # Decrement, and only drop the key when the LAST connection for this
        # CallSid ends. -= on a Counter can go negative, so guard it.
        if _active_calls[call_id] > 1:
            _active_calls[call_id] -= 1
        else:
            _active_calls.pop(call_id, None)


@router.websocket("/stream")
async def stream_websocket_legacy(websocket: WebSocket) -> None:
    """
    Deprecated query-string form, kept only so a call already in flight across
    a restart is not cut off mid-sentence, and so a failure here is
    diagnosable: it logs exactly what arrived rather than silently rejecting.

    Delete once no traffic has hit it for a day — grep for "Legacy /stream".
    """
    query = websocket.query_params
    logger.warning(
        "Legacy /stream query-param route used. "
        f"raw_query={str(websocket.url.query)!r} "
        f"param_keys={list(query.keys())} "
        f"sid_len={len(query.get('sid', ''))} "
        f"exp={query.get('exp', '')!r} "
        f"token_len={len(query.get('token', ''))} "
        f"(client: {websocket.client})"
    )
    return await stream_websocket(
        websocket, query.get("sid", ""), query.get("exp", ""), query.get("token", "")
    )