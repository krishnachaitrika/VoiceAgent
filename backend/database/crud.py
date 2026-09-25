import logging
from datetime import datetime
from typing import Optional, List, Dict, Any
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update, func, and_, or_, desc
from sqlalchemy.orm import selectinload
import config
from database.models import Call, Transcript, Lead, Escalation, Meeting, Setting, Document, SentimentReport

logger = logging.getLogger(__name__)


# ── CALLS ──────────────────────────────────────────────────────────────────

async def create_call(
    db: AsyncSession, call_id: str, phone_number: str, direction: str = "inbound"
) -> Call:
    """
    Idempotent on call_id (VA-C5 fix): call_id is now the call's real
    Twilio CallSid (see api/websocket.py), and Twilio re-POSTs
    /incoming-call on a webhook timeout — a retried webhook mints a second
    valid stream token for the SAME CallSid, so a second WebSocket can
    legitimately reach here for a call this function already created.
    Previously call_id was a fresh uuid4 per WebSocket connection, so a
    retry silently produced a second, unrelated Call row for one real
    conversation (inflating call counts and cost totals, and splitting one
    call's turns across two transcripts) — a plain insert here would now
    instead crash on the primary key, which is what makes returning the
    existing row the correct behavior, not just a convenience.
    """
    existing = await db.get(Call, call_id)
    if existing is not None and direction and existing.direction != direction:
        # A retried webhook should never change a call's direction, but if it
        # somehow does, trust the later value and say so rather than leaving a
        # mispriced row behind silently.
        existing.direction = direction
        await db.commit()
    if existing is not None:
        return existing
    call = Call(id=call_id, phone_number=phone_number, direction=direction or "inbound")
    db.add(call)
    await db.commit()
    await db.refresh(call)
    return call


async def update_call(
    db: AsyncSession,
    call_id: str,
    duration_seconds: int,
    status: str,
    openai_cost: float,
    sarvam_cost: float,
    stt_cost: float = 0.0,
    tts_cost: float = 0.0,
    twilio_cost: float = 0.0,
) -> None:
    """Persist the call's outcome and its full cost breakdown.

    sarvam_cost keeps its (now inaccurate) name and its combined stt + tts
    value so the existing dashboard and historical rows are unaffected. The
    three new arguments default to 0.0 so any caller not yet updated works.
    """
    await db.execute(
        update(Call)
        .where(Call.id == call_id)
        .values(
            duration_seconds=duration_seconds,
            status=status,
            openai_cost=openai_cost,
            sarvam_cost=sarvam_cost,
            stt_cost=stt_cost,
            tts_cost=tts_cost,
            twilio_cost=twilio_cost,
            updated_at=datetime.utcnow(),
        )
    )
    await db.commit()


async def get_calls(db: AsyncSession, page: int = 1, limit: int = 20) -> Dict[str, Any]:
    offset = (page - 1) * limit
    result = await db.execute(
        select(Call).order_by(desc(Call.created_at)).offset(offset).limit(limit)
    )
    calls = result.scalars().all()
    count_result = await db.execute(select(func.count(Call.id)))
    total = count_result.scalar()
    return {"calls": calls, "total": total, "page": page, "limit": limit}


async def get_dashboard_stats(db: AsyncSession) -> Dict[str, Any]:
    today = datetime.utcnow().date()

    total_calls = (await db.execute(select(func.count(Call.id)))).scalar() or 0
    today_calls = (
        await db.execute(
            select(func.count(Call.id)).where(func.date(Call.created_at) == today)
        )
    ).scalar() or 0
    # Twilio was omitted here for the same reason as in _serialize_call: the
    # column postdates this query. It is often the largest component of a
    # short call, so "Today's Cost" was understating spend by roughly two
    # thirds. COALESCE because rows written before the per-provider split
    # have NULL in the newer column, and NULL would poison the whole SUM.
    today_cost = (
        await db.execute(
            select(
                func.sum(
                    func.coalesce(Call.openai_cost, 0.0)
                    + func.coalesce(Call.sarvam_cost, 0.0)
                    + func.coalesce(Call.twilio_cost, 0.0)
                )
            ).where(func.date(Call.created_at) == today)
        )
    ).scalar() or 0.0
    avg_duration = (
        await db.execute(select(func.avg(Call.duration_seconds)))
    ).scalar() or 0.0
    escalation_count = (
        await db.execute(
            select(func.count(Escalation.id)).where(Escalation.resolved == False)
        )
    ).scalar() or 0
    lead_count = (await db.execute(select(func.count(Lead.id)))).scalar() or 0
    error_count = (
        await db.execute(select(func.count(Call.id)).where(Call.status == "error"))
    ).scalar() or 0

    return {
        "total_calls": total_calls,
        "today_calls": today_calls,
        "today_cost": round(float(today_cost), 4),
        "avg_duration": round(float(avg_duration), 1),
        "escalation_count": escalation_count,
        "lead_count": lead_count,
        "error_count": error_count,
    }


# ── TRANSCRIPTS ────────────────────────────────────────────────────────────

async def save_transcript(
    db: AsyncSession, call_id: str, full_text: str, turns: List[Dict]
) -> None:
    """
    Idempotent on call_id (VA-C5 fix, paired with create_call above) — a
    duplicated stream connection for one retried CallSid used to insert a
    second Transcript row per call, which get_transcript's
    scalar_one_or_none() can't handle (there's supposed to be exactly one
    per call — see database/models.py's Call.transcript, uselist=False).
    """
    result = await db.execute(select(Transcript).where(Transcript.call_id == call_id))
    existing = result.scalar_one_or_none()
    if existing is not None:
        existing.full_text = full_text
        existing.turns_json = turns
    else:
        db.add(Transcript(call_id=call_id, full_text=full_text, turns_json=turns))
    await db.commit()


async def get_transcript(db: AsyncSession, call_id: str) -> Optional[Transcript]:
    result = await db.execute(
        select(Transcript).where(Transcript.call_id == call_id)
    )
    return result.scalar_one_or_none()


# ── LEADS ──────────────────────────────────────────────────────────────────

async def create_lead(
    db: AsyncSession,
    call_id: Optional[str],
    name: str,
    phone: str,
    email: Optional[str],
    interest: Optional[str],
) -> Lead:
    lead = Lead(
        call_id=call_id,
        name=name,
        phone=phone,
        email=email,
        interest=interest,
    )
    db.add(lead)
    await db.commit()
    await db.refresh(lead)
    return lead


async def upsert_lead(
    db: AsyncSession,
    call_id: Optional[str],
    name: str,
    phone: str,
    email: Optional[str] = None,
    interest: Optional[str] = None,
) -> Lead:
    """
    Create a lead for this call, or update it in place if one already exists
    for this call_id. Prevents duplicate lead rows when the caller repeats,
    corrects, or adds to their details (name/phone/interest) across turns.
    Only non-empty fields overwrite existing values.
    """
    lead: Optional[Lead] = None
    if call_id:
        result = await db.execute(select(Lead).where(Lead.call_id == call_id))
        lead = result.scalar_one_or_none()

    if lead:
        if name:
            lead.name = name
        if phone:
            lead.phone = phone
        if email:
            lead.email = email
        if interest:
            lead.interest = interest
        await db.commit()
        await db.refresh(lead)
        return lead

    lead = Lead(
        call_id=call_id,
        name=name,
        phone=phone,
        email=email,
        interest=interest,
    )
    db.add(lead)
    await db.commit()
    await db.refresh(lead)
    return lead


async def upsert_meeting(
    db: AsyncSession,
    lead_id: Optional[int],
    meeting_datetime: datetime,
    calendar_event_id: Optional[str],
) -> Meeting:
    """
    Create a meeting for this lead, or update the existing one if the lead
    already has a meeting booked (e.g. caller changes the date/time before
    final confirmation). Prevents duplicate meeting rows.
    """
    meeting: Optional[Meeting] = None
    if lead_id:
        result = await db.execute(select(Meeting).where(Meeting.lead_id == lead_id))
        meeting = result.scalar_one_or_none()

    if meeting:
        meeting.datetime = meeting_datetime
        if calendar_event_id:
            meeting.calendar_event_id = calendar_event_id
        meeting.status = "scheduled"
        await db.commit()
        await db.refresh(meeting)
        return meeting

    meeting = Meeting(
        lead_id=lead_id,
        datetime=meeting_datetime,
        calendar_event_id=calendar_event_id,
    )
    db.add(meeting)
    await db.commit()
    await db.refresh(meeting)
    return meeting


async def get_meetings(db: AsyncSession, page: int = 1, limit: int = 20) -> Dict[str, Any]:
    """
    Paginated meetings list, each with its lead's name/phone attached
    (via selectinload so the relationship is available without a
    separate query per row).
    """
    offset = (page - 1) * limit
    result = await db.execute(
        select(Meeting)
        .options(selectinload(Meeting.lead))
        .order_by(desc(Meeting.datetime))
        .offset(offset)
        .limit(limit)
    )
    meetings = result.scalars().all()
    count_result = await db.execute(select(func.count(Meeting.id)))
    total = count_result.scalar()
    return {"meetings": meetings, "total": total, "page": page, "limit": limit}


async def get_leads(db: AsyncSession, page: int = 1, limit: int = 20) -> Dict[str, Any]:
    offset = (page - 1) * limit
    result = await db.execute(
        select(Lead).order_by(desc(Lead.created_at)).offset(offset).limit(limit)
    )
    leads = result.scalars().all()
    count_result = await db.execute(select(func.count(Lead.id)))
    total = count_result.scalar()
    return {"leads": leads, "total": total, "page": page, "limit": limit}


# ── ESCALATIONS ────────────────────────────────────────────────────────────

async def create_escalation(
    db: AsyncSession, call_id: Optional[str], reason: str, transcript_snippet: str
) -> Escalation:
    escalation = Escalation(
        call_id=call_id, reason=reason, transcript_snippet=transcript_snippet
    )
    db.add(escalation)
    await db.commit()
    await db.refresh(escalation)
    return escalation


async def upsert_escalation(
    db: AsyncSession, call_id: Optional[str], reason: str, transcript_snippet: str
) -> Escalation:
    """
    Create an escalation for this call, or update the existing unresolved
    one in place if this call already has one. Prevents duplicate
    escalation rows when sticky routing keeps sending follow-up turns
    ("okay and...", "can you also...") back into the escalation agent,
    which would otherwise call `escalate` again each turn.
    """
    escalation: Optional[Escalation] = None
    if call_id:
        result = await db.execute(
            select(Escalation).where(
                Escalation.call_id == call_id, Escalation.resolved == False
            )
        )
        escalation = result.scalars().first()

    if escalation:
        escalation.reason = reason
        if transcript_snippet:
            escalation.transcript_snippet = transcript_snippet
        await db.commit()
        await db.refresh(escalation)
        return escalation

    escalation = Escalation(
        call_id=call_id, reason=reason, transcript_snippet=transcript_snippet
    )
    db.add(escalation)
    await db.commit()
    await db.refresh(escalation)
    return escalation


async def get_escalations(
    db: AsyncSession, resolved: Optional[bool] = None
) -> List[Escalation]:
    query = select(Escalation).order_by(desc(Escalation.created_at))
    if resolved is not None:
        query = query.where(Escalation.resolved == resolved)
    result = await db.execute(query)
    return result.scalars().all()


async def resolve_escalation(db: AsyncSession, escalation_id: int) -> bool:
    result = await db.execute(
        update(Escalation)
        .where(Escalation.id == escalation_id)
        .values(resolved=True, resolved_at=datetime.utcnow())
        .returning(Escalation.id)
    )
    await db.commit()
    return result.scalar_one_or_none() is not None


# ── MEETINGS ───────────────────────────────────────────────────────────────

async def create_meeting(
    db: AsyncSession,
    lead_id: Optional[int],
    meeting_datetime: datetime,
    calendar_event_id: Optional[str],
) -> Meeting:
    meeting = Meeting(
        lead_id=lead_id,
        datetime=meeting_datetime,
        calendar_event_id=calendar_event_id,
    )
    db.add(meeting)
    await db.commit()
    await db.refresh(meeting)
    return meeting


# ── SETTINGS ───────────────────────────────────────────────────────────────

async def get_all_settings(db: AsyncSession) -> List[Setting]:
    result = await db.execute(select(Setting))
    return result.scalars().all()


async def upsert_setting(db: AsyncSession, key: str, value: str) -> None:
    result = await db.execute(select(Setting).where(Setting.key == key))
    existing = result.scalar_one_or_none()
    if existing:
        existing.value = value
        existing.updated_at = datetime.utcnow()
    else:
        db.add(Setting(key=key, value=value))
    await db.commit()


async def get_setting(db: AsyncSession, key: str) -> Optional[str]:
    result = await db.execute(select(Setting).where(Setting.key == key))
    setting = result.scalar_one_or_none()
    return setting.value if setting else None


# ── DOCUMENTS (pgvector) ───────────────────────────────────────────────────

async def store_document(
    db: AsyncSession, content: str, embedding: List[float], metadata: Dict
) -> Document:
    doc = Document(content=content, embedding=embedding, metadata_json=metadata)
    db.add(doc)
    await db.commit()
    await db.refresh(doc)
    return doc


async def store_documents_bulk(
    db: AsyncSession, rows: List[Dict[str, Any]]
) -> int:
    """
    Insert many document chunks in a single round-trip instead of one
    commit per row. `rows` is a list of {"content", "embedding", "metadata"}
    dicts. No per-row refresh — ingestion doesn't need the generated IDs
    back, only the count stored.
    """
    docs = [
        Document(
            content=row["content"],
            embedding=row["embedding"],
            metadata_json=row["metadata"],
        )
        for row in rows
    ]
    db.add_all(docs)
    await db.commit()
    return len(docs)


async def get_document_sources(db: AsyncSession) -> List[Dict[str, Any]]:
    """
    Return one row per uploaded source file (grouped by metadata->source),
    with a chunk count — used by the v2 document-upload dashboard page.

    Only pulls the JSON `source` field, a chunk count, and the earliest
    `created_at` per source at the SQL level (GROUP BY) — the previous
    version did `select(Document)` with no column restriction, which
    pulled every chunk's full pgvector embedding into Python just to group
    by a JSON field in a Python loop.
    """
    source_expr = Document.metadata_json["source"].as_string()
    result = await db.execute(
        select(
            func.coalesce(source_expr, "unknown").label("source"),
            func.count(Document.id).label("chunk_count"),
            func.min(Document.created_at).label("uploaded_at"),
        ).group_by(source_expr)
    )
    return [
        {
            "source": row.source,
            "chunk_count": row.chunk_count,
            "uploaded_at": row.uploaded_at.isoformat() if row.uploaded_at else None,
        }
        for row in result.all()
    ]


async def delete_document_source(db: AsyncSession, source: str) -> int:
    """
    Delete all chunks belonging to one uploaded source file.

    Filters by the JSON `source` field directly in the DELETE statement
    instead of loading every Document row (embeddings included) into
    Python and re-checking metadata_json there.
    """
    source_expr = Document.metadata_json["source"].as_string()
    filter_expr = (
        source_expr == source if source != "unknown" else
        or_(source_expr == "unknown", source_expr.is_(None))
    )
    result = await db.execute(
        Document.__table__.delete().where(filter_expr)
    )
    await db.commit()
    return result.rowcount or 0


# ── SENTIMENT REPORTS (v3+ analytics) ───────────────────────────────────────

async def upsert_sentiment_report(
    db: AsyncSession,
    call_id: str,
    sentiment: str,
    sentiment_score: float,
    intent_summary: str,
    outcome: str,
    key_topics: List[str],
    flagged_for_review: bool,
    analysis_cost: float,
) -> SentimentReport:
    result = await db.execute(select(SentimentReport).where(SentimentReport.call_id == call_id))
    report = result.scalar_one_or_none()
    if report:
        report.sentiment = sentiment
        report.sentiment_score = sentiment_score
        report.intent_summary = intent_summary
        report.outcome = outcome
        report.key_topics = key_topics
        report.flagged_for_review = flagged_for_review
        report.analysis_cost = analysis_cost
    else:
        report = SentimentReport(
            call_id=call_id,
            sentiment=sentiment,
            sentiment_score=sentiment_score,
            intent_summary=intent_summary,
            outcome=outcome,
            key_topics=key_topics,
            flagged_for_review=flagged_for_review,
            analysis_cost=analysis_cost,
        )
        db.add(report)
    await db.commit()
    await db.refresh(report)
    return report


async def get_sentiment_reports(db: AsyncSession, page: int = 1, limit: int = 20) -> Dict[str, Any]:
    offset = (page - 1) * limit
    result = await db.execute(
        select(SentimentReport).order_by(desc(SentimentReport.created_at)).offset(offset).limit(limit)
    )
    reports = result.scalars().all()
    count_result = await db.execute(select(func.count(SentimentReport.id)))
    total = count_result.scalar()
    return {"reports": reports, "total": total, "page": page, "limit": limit}


async def get_sentiment_summary(db: AsyncSession) -> Dict[str, Any]:
    """Aggregate counts for the analytics dashboard overview cards."""
    total = (await db.execute(select(func.count(SentimentReport.id)))).scalar() or 0
    positive = (
        await db.execute(select(func.count(SentimentReport.id)).where(SentimentReport.sentiment == "positive"))
    ).scalar() or 0
    negative = (
        await db.execute(select(func.count(SentimentReport.id)).where(SentimentReport.sentiment == "negative"))
    ).scalar() or 0
    flagged = (
        await db.execute(select(func.count(SentimentReport.id)).where(SentimentReport.flagged_for_review == True))
    ).scalar() or 0
    avg_score = (await db.execute(select(func.avg(SentimentReport.sentiment_score)))).scalar() or 0.0
    return {
        "total_analyzed": total,
        "positive_count": positive,
        "negative_count": negative,
        "flagged_count": flagged,
        "avg_sentiment_score": round(float(avg_score), 3),
    }


# ── DATA RETENTION / ERASURE (VA-B5 fix) ────────────────────────────────────
# Caller PII (leads.phone, leads.email, calls.phone_number, full transcripts)
# was previously stored indefinitely with no retention window and no
# deletion path — masking only ever happened on read, at the dashboard API
# boundary. The two functions below give the business an actual technical
# mechanism to enforce a retention period and to honour a caller's erasure
# request; the retention period itself and how a request is verified as
# genuinely from that caller are policy/legal decisions, not code (see the
# QA report's note that VA-B5 needs legal review, not a purely technical
# fix).
#
# Deletion order matters — no FK has ON DELETE CASCADE, so children must go
# before their parent: sentiment_reports/escalations/transcripts before
# calls, meetings before leads.

async def _delete_calls_cascade(db: AsyncSession, call_ids: List[str]) -> int:
    if not call_ids:
        return 0
    lead_ids_result = await db.execute(select(Lead.id).where(Lead.call_id.in_(call_ids)))
    lead_ids = [row[0] for row in lead_ids_result.all()]

    if lead_ids:
        await db.execute(Meeting.__table__.delete().where(Meeting.lead_id.in_(lead_ids)))
    await db.execute(SentimentReport.__table__.delete().where(SentimentReport.call_id.in_(call_ids)))
    await db.execute(Escalation.__table__.delete().where(Escalation.call_id.in_(call_ids)))
    await db.execute(Transcript.__table__.delete().where(Transcript.call_id.in_(call_ids)))
    await db.execute(Lead.__table__.delete().where(Lead.call_id.in_(call_ids)))
    result = await db.execute(Call.__table__.delete().where(Call.id.in_(call_ids)))
    await db.commit()
    return result.rowcount or 0


async def purge_calls_older_than(db: AsyncSession, cutoff: datetime) -> int:
    """Permanently delete every call (and its transcript, leads, meetings,
    escalations, sentiment report) created before `cutoff`. Used by
    scripts/purge_old_data.py to enforce config.DATA_RETENTION_DAYS."""
    call_ids_result = await db.execute(select(Call.id).where(Call.created_at < cutoff))
    call_ids = [row[0] for row in call_ids_result.all()]
    return await _delete_calls_cascade(db, call_ids)


async def delete_caller_data(db: AsyncSession, phone: str) -> int:
    """Erase every call and lead on file for one caller's phone number
    (right-to-erasure path — api/dashboard.py's DELETE /callers/{phone},
    admin-only). Matches on calls.phone_number and leads.phone
    independently, since a lead can exist with no call_id."""
    # VA-T-009 — MATCH ON DIGITS, NOT ON THE EXACT STRING.
    #
    # An exact == missed the same person recorded in a different format, and
    # the endpoint still reported success. One number reaches this system by
    # several routes:
    #
    #     calls.phone_number   "+916369996595"   from Twilio's From field
    #     leads.phone          "6369996595"      normalised by utils/phone.py
    #     a caller typing it   "+91 63699 96595" with spaces
    #
    # So an erasure request for "6369996595" deleted the lead and left every
    # call, transcript and recording of that person in place — while returning
    # "Deleted N records", which is worse than failing: the request is closed,
    # the data is still there, and nobody knows.
    #
    # Comparing on digits only makes all three forms the same person.
    # regexp_replace runs in Postgres so it still matches server-side rather
    # than pulling the table into Python; it is a sequential scan, which is
    # entirely acceptable for an operation that runs a handful of times a year
    # and must not miss anything.
    #
    # The last PHONE_NUMBER_DIGIT_LENGTH digits are compared, so a number
    # stored WITH a country code and the same number stored WITHOUT one still
    # resolve to the same person. Read from config rather than hardcoded to 10:
    # utils/phone.py already normalises against that same setting, and a
    # deployment serving a market with a different national number length
    # would otherwise silently fail to erase anything.
    digits = "".join(c for c in (phone or "") if c.isdigit())
    if not digits:
        return 0
    tail_len = config.PHONE_NUMBER_DIGIT_LENGTH
    tail = digits[-tail_len:] if len(digits) >= tail_len else digits

    phone_digits = func.regexp_replace(Call.phone_number, r"\D", "", "g")
    call_ids_result = await db.execute(
        select(Call.id).where(phone_digits.like(f"%{tail}"))
    )
    call_ids = [row[0] for row in call_ids_result.all()]
    deleted = await _delete_calls_cascade(db, call_ids)

    lead_digits = func.regexp_replace(Lead.phone, r"\D", "", "g")
    orphan_leads_result = await db.execute(
        select(Lead.id).where(lead_digits.like(f"%{tail}"), Lead.call_id.is_(None))
    )
    orphan_lead_ids = [row[0] for row in orphan_leads_result.all()]
    if orphan_lead_ids:
        await db.execute(Meeting.__table__.delete().where(Meeting.lead_id.in_(orphan_lead_ids)))
        result = await db.execute(Lead.__table__.delete().where(Lead.id.in_(orphan_lead_ids)))
        deleted += result.rowcount or 0
        await db.commit()

    return deleted

async def update_twilio_cost(
    db: AsyncSession,
    call_id: str,
    twilio_cost: float,
    source: str = "actual",
    currency: str = "USD",
) -> None:
    """Overwrite the Twilio cost with the reconciled figure.

    Separate from update_call because it runs LATER, on its own schedule,
    after Twilio has rated the call — see billing/twilio_reconcile.py. Touching
    only these three columns means it cannot race with, or clobber, anything
    the end-of-call write already stored.
    """
    await db.execute(
        update(Call)
        .where(Call.id == call_id)
        .values(
            twilio_cost=twilio_cost,
            twilio_cost_source=source,
            twilio_currency=currency,
            updated_at=datetime.utcnow(),
        )
    )
    await db.commit()