"""
events/bus.py — Redis Streams event bus ("Event Bus" box in the v3+
architecture). Publishes lightweight, fire-and-forget events for things
that happen during a call — call_started, call_ended, lead_created,
meeting_booked, escalation_created — onto a single Redis Stream.

Why a stream instead of pub/sub: pub/sub messages are lost if no consumer
is listening at the moment they're published (fine for ephemeral chat, not
fine for "a lead was just created" — analytics/CRM-sync consumers may be
briefly down). A stream persists events so a consumer can catch up.

Consumers (not run automatically here, but supported):
  - analytics/sentiment.py can subscribe to `call_ended` to trigger the
    post-call sentiment analysis job asynchronously instead of inline.
  - A future CRM-sync worker could consume `lead_created` events.

This module never blocks or fails a call: every publish is wrapped in
try/except and a Redis outage just means the event is skipped, not that
the call breaks.
"""
import json
import logging
import time
from typing import Any, Dict, Optional

import config
from cache.redis_client import get_redis

logger = logging.getLogger(__name__)

STREAM_NAME = "voice_agent_events"
MAX_STREAM_LEN = 10_000  # approx trim — keeps the stream from growing forever


async def publish(event_type: str, payload: Dict[str, Any], call_id: Optional[str] = None) -> None:
    """
    Publish an event onto the shared Redis Stream. No-op if Redis/event bus
    is disabled or unreachable — logs at debug level and returns.

    Args:
        event_type: e.g. "call_started", "call_ended", "lead_created",
                    "meeting_booked", "escalation_created"
        payload:    JSON-serializable event data
        call_id:    optional call id for correlation
    """
    if not config.ENABLE_EVENT_BUS:
        return

    client = get_redis()
    if client is None:
        return

    event = {
        "type": event_type,
        "call_id": call_id or "",
        "timestamp": str(time.time()),
        "payload": json.dumps(payload, default=str),
    }
    try:
        await client.xadd(STREAM_NAME, event, maxlen=MAX_STREAM_LEN, approximate=True)
        logger.debug(f"[events] Published '{event_type}' for call_id={call_id}")
    except Exception as e:
        logger.warning(f"[events] Failed to publish '{event_type}': {e}")


async def read_recent(count: int = 50) -> list:
    """
    Read the most recent N events from the stream — used by a lightweight
    dashboard "live activity" feed. Returns [] if Redis is unavailable.
    """
    client = get_redis()
    if client is None:
        return []
    try:
        entries = await client.xrevrange(STREAM_NAME, count=count)
        results = []
        for entry_id, fields in entries:
            payload = fields.get("payload", "{}")
            try:
                payload = json.loads(payload)
            except json.JSONDecodeError:
                pass
            results.append({
                "id": entry_id,
                "type": fields.get("type"),
                "call_id": fields.get("call_id"),
                "timestamp": fields.get("timestamp"),
                "payload": payload,
            })
        return results
    except Exception as e:
        logger.warning(f"[events] Failed to read recent events: {e}")
        return []
