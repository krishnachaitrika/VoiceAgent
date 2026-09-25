import logging
from datetime import timezone
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from database.base import get_db
from database import crud
from auth import require_dashboard_auth, require_dashboard_admin
from utils.privacy import mask_phone as _mask_phone

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/dashboard", dependencies=[Depends(require_dashboard_auth)])


def _utc_iso(dt) -> Optional[str]:
    """
    Serialize a genuine system-clock timestamp (created_at / resolved_at —
    always written via the DB's func.now(), and therefore always a real
    UTC instant) as an explicit UTC ISO string.

    Real dashboard testing showed the frontend displaying the wrong time
    (date was fine, clock was off by exactly the UTC offset): the plain
    `dt.isoformat()` this replaces has no 'Z'/offset marker, so the
    frontend's `new Date(iso)` parses it as browser-local time instead of
    UTC — the raw UTC clock digits were being shown as if they were
    already local time, never converted.

    ONLY use this for true system timestamps. meeting.datetime is a
    different kind of field entirely — the caller's literal spoken
    appointment time, built via datetime.strptime with no UTC conversion
    at all (see brain/tools.py: _tool_book_meeting) — and must NOT go
    through this helper, since it was never a UTC instant to begin with;
    tagging it as UTC would shift a caller's actual meeting time by the
    UTC offset when displayed, introducing a new bug in the other
    direction. See _serialize_meeting below: datetime stays on plain
    .isoformat(), only created_at gets this helper.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def _serialize_call(call) -> dict:
    return {
        "id": call.id,
        "phone_number": _mask_phone(call.phone_number),
        "duration_seconds": call.duration_seconds,
        "status": call.status,
        "openai_cost": call.openai_cost,
        # Kept under its original key for backward compatibility with any
        # existing frontend code. The name predates ElevenLabs taking over
        # STT — it now holds ALL speech cost (stt + tts). The two fields
        # below are the ones to read for a real breakdown.
        "sarvam_cost": call.sarvam_cost,
        "stt_cost": call.stt_cost,
        "tts_cost": call.tts_cost,
        # Twilio was MISSING from this total entirely, because the column did
        # not exist when this serializer was written. On a short outbound call
        # it is the single largest component — a 62-second call showed $0.0618
        # here against a real cost of $0.2016, i.e. the dashboard displayed
        # 31% of what the call actually cost. Anyone pricing the product from
        # this screen would have been badly wrong.
        "twilio_cost": call.twilio_cost,
        "twilio_cost_source": call.twilio_cost_source,
        "direction": call.direction,
        "total_cost": round(
            (call.openai_cost or 0)
            + (call.sarvam_cost or 0)
            + (call.twilio_cost or 0),
            4,
        ),
        "created_at": _utc_iso(call.created_at),
    }


def _serialize_lead(lead) -> dict:
    return {
        "id": lead.id,
        "call_id": lead.call_id,
        "name": lead.name,
        "phone": _mask_phone(lead.phone),
        "email": lead.email,
        "interest": lead.interest,
        "created_at": _utc_iso(lead.created_at),
    }


def _serialize_escalation(esc) -> dict:
    return {
        "id": esc.id,
        "call_id": esc.call_id,
        "reason": esc.reason,
        "transcript_snippet": esc.transcript_snippet,
        "resolved": esc.resolved,
        "resolved_at": _utc_iso(esc.resolved_at),
        "created_at": _utc_iso(esc.created_at),
    }


def _serialize_meeting(meeting) -> dict:
    lead = meeting.lead
    return {
        "id": meeting.id,
        "lead_id": meeting.lead_id,
        "name": lead.name if lead else None,
        "phone": _mask_phone(lead.phone) if lead else None,
        # NOT _utc_iso — this is the caller's literal spoken appointment
        # time (naive, never a UTC instant), not a system timestamp. See
        # _utc_iso's docstring above for why these two fields are handled
        # differently.
        "datetime": meeting.datetime.isoformat() if meeting.datetime else None,
        "status": meeting.status,
        "calendar_event_id": meeting.calendar_event_id,
        "created_at": _utc_iso(meeting.created_at),
    }


@router.get("/stats")
async def get_stats(db: AsyncSession = Depends(get_db)):
    """Returns aggregate stats for the dashboard overview."""
    try:
        return await crud.get_dashboard_stats(db)
    except Exception as e:
        logger.error(f"Stats error: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch stats")


@router.get("/calls")
async def get_calls(
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    """Returns paginated list of calls."""
    try:
        result = await crud.get_calls(db, page=page, limit=limit)
        return {
            "calls": [_serialize_call(c) for c in result["calls"]],
            "total": result["total"],
            "page": result["page"],
            "limit": result["limit"],
        }
    except Exception as e:
        logger.error(f"Get calls error: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch calls")


@router.get("/calls/{call_id}/transcript")
async def get_call_transcript(call_id: str, db: AsyncSession = Depends(get_db)):
    """Returns full transcript for a specific call."""
    try:
        transcript = await crud.get_transcript(db, call_id)
        if not transcript:
            raise HTTPException(status_code=404, detail="Transcript not found")
        return {
            "call_id": call_id,
            "full_text": transcript.full_text,
            "turns": transcript.turns_json,
            "created_at": _utc_iso(transcript.created_at),
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Get transcript error: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch transcript")


@router.get("/leads")
async def get_leads(
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    """Returns paginated leads list."""
    try:
        result = await crud.get_leads(db, page=page, limit=limit)
        return {
            "leads": [_serialize_lead(l) for l in result["leads"]],
            "total": result["total"],
            "page": result["page"],
            "limit": result["limit"],
        }
    except Exception as e:
        logger.error(f"Get leads error: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch leads")


@router.get("/escalations")
async def get_escalations(
    resolved: Optional[bool] = Query(None),
    db: AsyncSession = Depends(get_db),
):
    """Returns escalations, optionally filtered by resolved status."""
    try:
        escalations = await crud.get_escalations(db, resolved=resolved)
        return {"escalations": [_serialize_escalation(e) for e in escalations]}
    except Exception as e:
        logger.error(f"Get escalations error: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch escalations")


@router.post("/escalations/{escalation_id}/resolve")
async def resolve_escalation(escalation_id: int, db: AsyncSession = Depends(get_db)):
    """Mark an escalation as resolved."""
    try:
        success = await crud.resolve_escalation(db, escalation_id)
        if not success:
            raise HTTPException(status_code=404, detail="Escalation not found")
        return {"message": "Escalation resolved", "id": escalation_id}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Resolve escalation error: {e}")
        raise HTTPException(status_code=500, detail="Failed to resolve escalation")


@router.get("/meetings")
async def get_meetings(
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    """Returns paginated list of booked meetings, with lead name/phone attached."""
    try:
        result = await crud.get_meetings(db, page=page, limit=limit)
        return {
            "meetings": [_serialize_meeting(m) for m in result["meetings"]],
            "total": result["total"],
            "page": result["page"],
            "limit": result["limit"],
        }
    except Exception as e:
        logger.error(f"Get meetings error: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch meetings")


@router.delete("/callers/{phone}", dependencies=[Depends(require_dashboard_admin)])
async def erase_caller_data(
    phone: str,
    confirm: bool = Query(False, description="Must be true — deletion is immediate and permanent"),
    db: AsyncSession = Depends(get_db),
):
    """
    Right-to-erasure path (VA-B5 fix): permanently deletes every call,
    transcript, lead, meeting, escalation and sentiment report on file for
    one phone number. There was previously no deletion path for caller
    data at all. Verifying the request actually came from that caller is a
    policy decision outside this endpoint's scope — admin-only, same as
    the rest of the write surface.
    """
    if not confirm:
        raise HTTPException(status_code=400, detail="Pass ?confirm=true to permanently erase this caller's data")
    deleted = await crud.delete_caller_data(db, phone)
    return {"message": f"Deleted {deleted} row(s)", "phone": _mask_phone(phone)}