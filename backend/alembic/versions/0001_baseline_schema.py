"""Baseline schema — mirrors database/models.py as it exists today.

This replaces scripts/setup_db.py's Base.metadata.create_all as the source
of truth for new environments (VA-A4 fix — there was previously no
versioned schema at all, no reproducible test database, and no tested
upgrade path). Written by hand against database/models.py rather than
generated with --autogenerate against a live database, since introducing
Alembic here shouldn't require connecting to the real Supabase instance.

For an EXISTING database (tables already created by setup_db.py): run
`alembic stamp head` once to adopt migrations without re-running this DDL.
For a brand-new environment: `alembic upgrade head` creates everything.

Revision ID: 0001
Revises:
Create Date: 2026-09-11
"""
import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector

import config

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "calls",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("phone_number", sa.String(), nullable=False),
        sa.Column("duration_seconds", sa.Integer(), default=0),
        sa.Column("status", sa.String(), default="completed"),
        sa.Column("language_detected", sa.String(), default="en-IN"),
        sa.Column("openai_cost", sa.Float(), default=0.0),
        sa.Column("sarvam_cost", sa.Float(), default=0.0),
        sa.Column("created_at", sa.DateTime()),
        sa.Column("updated_at", sa.DateTime()),
    )

    op.create_table(
        "transcripts",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("call_id", sa.String(), sa.ForeignKey("calls.id"), nullable=False),
        sa.Column("full_text", sa.Text(), default=""),
        sa.Column("turns_json", sa.JSON(), default=list),
        sa.Column("created_at", sa.DateTime()),
    )

    op.create_table(
        "leads",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("call_id", sa.String(), sa.ForeignKey("calls.id"), nullable=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("phone", sa.String(), nullable=False),
        sa.Column("email", sa.String(), nullable=True),
        sa.Column("interest", sa.String(), nullable=True),
        sa.Column("language", sa.String(), default="en-IN"),
        sa.Column("created_at", sa.DateTime()),
    )

    op.create_table(
        "escalations",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("call_id", sa.String(), sa.ForeignKey("calls.id"), nullable=True),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("transcript_snippet", sa.Text(), default=""),
        sa.Column("resolved", sa.Boolean(), default=False),
        sa.Column("resolved_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime()),
    )

    op.create_table(
        "meetings",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("lead_id", sa.Integer(), sa.ForeignKey("leads.id"), nullable=True),
        sa.Column("datetime", sa.DateTime(), nullable=False),
        sa.Column("calendar_event_id", sa.String(), nullable=True),
        sa.Column("status", sa.String(), default="scheduled"),
        sa.Column("created_at", sa.DateTime()),
    )

    op.create_table(
        "settings",
        sa.Column("key", sa.String(), primary_key=True),
        sa.Column("value", sa.Text(), default=""),
        sa.Column("updated_at", sa.DateTime()),
    )

    op.create_table(
        "documents",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("embedding", Vector(config.EMBEDDING_DIM)),
        sa.Column("metadata_json", sa.JSON(), default=dict),
        sa.Column("created_at", sa.DateTime()),
    )

    op.create_table(
        "sentiment_reports",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("call_id", sa.String(), sa.ForeignKey("calls.id"), nullable=False, unique=True),
        sa.Column("sentiment", sa.String(), default="neutral"),
        sa.Column("sentiment_score", sa.Float(), default=0.0),
        sa.Column("intent_summary", sa.Text(), default=""),
        sa.Column("outcome", sa.String(), default="unresolved"),
        sa.Column("key_topics", sa.JSON(), default=list),
        sa.Column("flagged_for_review", sa.Boolean(), default=False),
        sa.Column("analysis_cost", sa.Float(), default=0.0),
        sa.Column("created_at", sa.DateTime()),
    )


def downgrade() -> None:
    op.drop_table("sentiment_reports")
    op.drop_table("documents")
    op.drop_table("settings")
    op.drop_table("meetings")
    op.drop_table("escalations")
    op.drop_table("leads")
    op.drop_table("transcripts")
    op.drop_table("calls")
