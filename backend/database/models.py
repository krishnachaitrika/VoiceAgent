from datetime import datetime
from typing import Optional
from sqlalchemy import (
    Column, String, Integer, Float, Boolean, DateTime,
    ForeignKey, Text, JSON, func
)
from sqlalchemy.orm import DeclarativeBase, relationship
from pgvector.sqlalchemy import Vector
import config


class Base(DeclarativeBase):
    pass


class Call(Base):
    __tablename__ = "calls"

    id = Column(String, primary_key=True)
    phone_number = Column(String, nullable=False)
    duration_seconds = Column(Integer, default=0)
    status = Column(String, default="completed")  # completed / escalated / error
    # "inbound" (someone called us) or "outbound" (we dialled them). Taken from
    # Twilio's Direction field on the webhook, which the code previously
    # ignored. Needed because the two are priced an order of magnitude apart,
    # and because any dashboard will eventually want the split.
    direction = Column(String, default="inbound")
    openai_cost = Column(Float, default=0.0)
    # Retained for backward compatibility with the existing dashboard and with
    # rows written before the per-provider split. Holds ALL non-OpenAI speech
    # cost (stt_cost + tts_cost), which is why the name stopped matching
    # reality once ElevenLabs took over STT from Sarvam. Read the columns below
    # for analysis; read this one only for historical rows.
    sarvam_cost = Column(Float, default=0.0)

    # Per-provider breakdown. stt_cost and tts_cost already existed in the
    # database but were never declared on the model or written to; twilio_cost
    # is new (migration 0003). Splitting these out is what makes "what did
    # ElevenLabs cost us last month" answerable with a SELECT instead of
    # arithmetic that silently breaks when a rate changes.
    stt_cost = Column(Float, default=0.0)      # ElevenLabs Scribe, per minute of audio
    tts_cost = Column(Float, default=0.0)      # Sarvam or ElevenLabs, per 1K characters
    twilio_cost = Column(Float, default=0.0)   # voice minutes; estimate, then reconciled
    # "estimated" (duration x configured rate) or "actual" (fetched from
    # Twilio's Call resource). Without this, a row whose reconciliation never
    # completed is indistinguishable from a measured one — which is precisely
    # the ambiguity that makes an estimate untrustworthy.
    twilio_cost_source = Column(String, default="estimated")
    twilio_currency = Column(String, default="USD")
    created_at = Column(DateTime, default=func.now())
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())

    transcript = relationship("Transcript", back_populates="call", uselist=False)
    leads = relationship("Lead", back_populates="call")
    escalations = relationship("Escalation", back_populates="call")


class Transcript(Base):
    __tablename__ = "transcripts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    call_id = Column(String, ForeignKey("calls.id"), nullable=False)
    full_text = Column(Text, default="")
    turns_json = Column(JSON, default=list)
    created_at = Column(DateTime, default=func.now())

    call = relationship("Call", back_populates="transcript")


class Lead(Base):
    __tablename__ = "leads"

    id = Column(Integer, primary_key=True, autoincrement=True)
    call_id = Column(String, ForeignKey("calls.id"), nullable=True)
    name = Column(String, nullable=False)
    phone = Column(String, nullable=False)
    email = Column(String, nullable=True)
    interest = Column(String, nullable=True)
    created_at = Column(DateTime, default=func.now())

    call = relationship("Call", back_populates="leads")
    meetings = relationship("Meeting", back_populates="lead")


class Escalation(Base):
    __tablename__ = "escalations"

    id = Column(Integer, primary_key=True, autoincrement=True)
    call_id = Column(String, ForeignKey("calls.id"), nullable=True)
    reason = Column(Text, nullable=False)
    transcript_snippet = Column(Text, default="")
    resolved = Column(Boolean, default=False)
    resolved_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=func.now())

    call = relationship("Call", back_populates="escalations")


class Meeting(Base):
    __tablename__ = "meetings"

    id = Column(Integer, primary_key=True, autoincrement=True)
    lead_id = Column(Integer, ForeignKey("leads.id"), nullable=True)
    # Naive — convention: local time in config.GOOGLE_CALENDAR_TIMEZONE,
    # NOT UTC (unlike created_at elsewhere in this file, which is a real
    # UTC instant via func.now()). This is the caller's literal spoken
    # appointment time, unconverted, so it displays correctly on the
    # dashboard and matches what was sent to Google Calendar (see
    # brain/tools.py's _tool_book_meeting and api/dashboard.py's _utc_iso
    # docstring, which explains why this one deliberately isn't UTC).
    # brain/tools.py's _validate_meeting_datetime (VA-C7 fix) is what
    # guarantees a value reaching this column is a real, future,
    # in-business-hours time — the column itself still can't enforce that.
    datetime = Column(DateTime, nullable=False)
    calendar_event_id = Column(String, nullable=True)
    status = Column(String, default="scheduled")  # scheduled / cancelled / completed
    created_at = Column(DateTime, default=func.now())

    lead = relationship("Lead", back_populates="meetings")


class Setting(Base):
    __tablename__ = "settings"

    key = Column(String, primary_key=True)
    value = Column(Text, default="")
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())


class Document(Base):
    __tablename__ = "documents"

    id = Column(Integer, primary_key=True, autoincrement=True)
    content = Column(Text, nullable=False)
    embedding = Column(Vector(config.EMBEDDING_DIM))
    metadata_json = Column(JSON, default=dict)
    created_at = Column(DateTime, default=func.now())


class SentimentReport(Base):
    """
    v3+ post-call analytics — "Sentiment Reports" in the architecture
    diagram. One row per completed call, generated asynchronously after the
    call ends (see analytics/sentiment.py) so it never touches the
    real-time voice latency budget.
    """
    __tablename__ = "sentiment_reports"

    id = Column(Integer, primary_key=True, autoincrement=True)
    call_id = Column(String, ForeignKey("calls.id"), nullable=False, unique=True)
    sentiment = Column(String, default="neutral")     # positive / neutral / negative
    sentiment_score = Column(Float, default=0.0)       # -1.0 (very negative) .. 1.0 (very positive)
    intent_summary = Column(Text, default="")          # 1-2 sentence summary of what the caller wanted
    outcome = Column(String, default="unresolved")     # resolved / lead_captured / booked / escalated / unresolved
    key_topics = Column(JSON, default=list)             # e.g. ["ServiceNow", "pricing"]
    flagged_for_review = Column(Boolean, default=False)
    analysis_cost = Column(Float, default=0.0)
    created_at = Column(DateTime, default=func.now())

class User(Base):
    """A dashboard operator account (VA-T-002).

    The dashboard previously had no login at all: its proxy attached the admin
    key to every request, so anyone who could reach the host saw call
    transcripts and caller phone numbers.

    Per-user accounts rather than one shared password, because a shared secret
    cannot answer the two questions that matter in production: WHO changed a
    setting, and how do you revoke ONE person's access when they leave. It is
    also the row that `tenant_id` attaches to when this serves more than one
    client — a shared password could never separate one client's call
    transcripts from another's.

    The password itself is never stored. Only an Argon2id hash, which embeds
    its own salt and cost parameters, so a database dump does not hand anyone
    a working login.
    """

    __tablename__ = "users"

    id = Column(String, primary_key=True)  # uuid4, generated on creation
    # Stored lowercase and unique. Case-insensitive because "Velu" and "velu"
    # being two accounts is a support problem nobody needs.
    username = Column(String, unique=True, nullable=False, index=True)
    password_hash = Column(String, nullable=False)

    # Separate from deletion so revoking access keeps the audit trail intact —
    # a deleted row would orphan any record of what that person did.
    is_active = Column(Boolean, default=True, nullable=False)
    # Reserved for the admin/viewer split. Every account created today is an
    # admin; the column exists now so adding roles later is not a migration
    # against a populated table.
    role = Column(String, default="admin", nullable=False)

    last_login_at = Column(DateTime(timezone=True), nullable=True)
    # Cleared on success. Used to lock an account after repeated failures, so
    # an exposed dashboard cannot be brute-forced at HTTP speed.
    failed_login_count = Column(Integer, default=0, nullable=False)
    locked_until = Column(DateTime(timezone=True), nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())