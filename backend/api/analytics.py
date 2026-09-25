"""
api/analytics.py — v3+ "Sentiment Reports" + live event feed for the
dashboard's Analytics page.
"""
import logging
from datetime import timezone
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from database.base import get_db
from database import crud
from events import bus as event_bus
from cache.redis_client import ping as redis_ping
from auth import require_dashboard_auth

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/analytics", dependencies=[Depends(require_dashboard_auth)])


def _utc_iso(dt) -> Optional[str]:
    """Serialize a system-clock timestamp (func.now(), a real UTC instant)
    as an explicit UTC ISO string — without the 'Z'/offset marker, the
    frontend's `new Date(iso)` parses it as browser-local time instead of
    UTC, showing raw UTC digits instead of the viewer's actual local time.
    See api/dashboard.py's _utc_iso for the full explanation and the real
    dashboard bug this was written to fix."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def _serialize_report(r) -> dict:
    return {
        "id": r.id,
        "call_id": r.call_id,
        "sentiment": r.sentiment,
        "sentiment_score": r.sentiment_score,
        "intent_summary": r.intent_summary,
        "outcome": r.outcome,
        "key_topics": r.key_topics,
        "flagged_for_review": r.flagged_for_review,
        "created_at": _utc_iso(r.created_at),
    }


@router.get("/sentiment/summary")
async def sentiment_summary(db: AsyncSession = Depends(get_db)):
    try:
        return await crud.get_sentiment_summary(db)
    except Exception as e:
        logger.error(f"Sentiment summary error: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch sentiment summary")


@router.get("/sentiment")
async def sentiment_reports(
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    try:
        result = await crud.get_sentiment_reports(db, page=page, limit=limit)
        return {
            "reports": [_serialize_report(r) for r in result["reports"]],
            "total": result["total"],
            "page": result["page"],
            "limit": result["limit"],
        }
    except Exception as e:
        logger.error(f"Sentiment reports error: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch sentiment reports")


@router.get("/events")
async def recent_events(count: int = Query(50, ge=1, le=200)):
    """Live activity feed — recent events off the Redis Streams event bus."""
    events = await event_bus.read_recent(count=count)
    return {"events": events}


@router.get("/system-health")
async def system_health():
    """Quick status of the enterprise layer (Redis) for a dashboard badge."""
    return {"redis_connected": await redis_ping()}