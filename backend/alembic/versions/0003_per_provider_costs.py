"""add per-provider cost columns to calls

Splits call cost out by provider instead of lumping every non-OpenAI charge
into `sarvam_cost`.

WHY

`calls` had two cost columns: `openai_cost` and `sarvam_cost`. The second one
was named when Sarvam handled both speech-to-text and text-to-speech. Once
ElevenLabs took over STT, that column started holding two different providers'
charges added together, and the name stopped describing its contents. Twilio
voice minutes — roughly 20% of a short call's real cost — were not counted
anywhere at all.

The practical consequence: "what did ElevenLabs cost us last month" could not
be answered from the database. It could only be estimated with arithmetic
(`duration x rate`) that silently misreports every historical row the moment a
rate changes, and breaks entirely if the TTS provider is switched.

WHAT THIS DOES

  stt_cost      ElevenLabs Scribe, billed per minute of audio
  tts_cost      Sarvam or ElevenLabs, billed per 1K characters
  twilio_cost   voice minutes, rounded up, priced by direction
  direction     "inbound" or "outbound"

`direction` is here for pricing, not reporting. Inbound to a US number costs
around $0.0085/min; outbound to an Indian mobile is $0.10-0.15/min. Applying
one rate to both under-reports outbound by roughly 93% — worse than no
tracking, because the number still looks plausible. Twilio sends Direction on
every webhook; the code simply ignored it.

`stt_cost` and `tts_cost` already exist in some environments — they were added
directly to the database at some point but never declared on the model or
written to. `IF NOT EXISTS` makes this migration safe either way.

`sarvam_cost` is deliberately NOT dropped or renamed. It keeps its combined
`stt + tts` value so the existing dashboard and every historical row are
unaffected. Dropping a column is irreversible and this one still has readers.

Revision ID: 0003
Revises: 0002
"""
from alembic import op
import sqlalchemy as sa

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Raw SQL rather than op.add_column: only ADD COLUMN IF NOT EXISTS is
    # idempotent against environments where stt_cost/tts_cost were already
    # added by hand outside of Alembic.
    op.execute("ALTER TABLE calls ADD COLUMN IF NOT EXISTS stt_cost DOUBLE PRECISION DEFAULT 0.0")
    op.execute("ALTER TABLE calls ADD COLUMN IF NOT EXISTS tts_cost DOUBLE PRECISION DEFAULT 0.0")
    op.execute("ALTER TABLE calls ADD COLUMN IF NOT EXISTS twilio_cost DOUBLE PRECISION DEFAULT 0.0")
    op.execute("ALTER TABLE calls ADD COLUMN IF NOT EXISTS direction VARCHAR DEFAULT 'inbound'")

    # Backfill existing rows so historical calls are not NULL. These are
    # ESTIMATES, flagged as such:
    #
    #   stt_cost  is recoverable because ElevenLabs bills purely on duration,
    #             so duration x rate reproduces what was charged.
    #   tts_cost  is whatever is left in sarvam_cost after subtracting that,
    #             clamped at zero — rows written before the rates were
    #             configured have sarvam_cost = 0 and would otherwise go
    #             negative.
    #   twilio_cost is left at 0.0 rather than backfilled. It was never
    #             recorded, and inventing a figure from the current rate would
    #             be indistinguishable from a measured one later.
    op.execute(
        """
        UPDATE calls
        SET stt_cost = COALESCE(stt_cost, 0.0),
            tts_cost = COALESCE(tts_cost, 0.0)
        WHERE stt_cost IS NULL OR tts_cost IS NULL
        """
    )
    op.execute("UPDATE calls SET twilio_cost = 0.0 WHERE twilio_cost IS NULL")

    # Every existing row predates outbound support, so they are all inbound.
    # That is a fact about this deployment's history, not a guess.
    op.execute("UPDATE calls SET direction = 'inbound' WHERE direction IS NULL")

    # Reporting on direction is the obvious next use; index it now while the
    # table is small rather than after it has grown.
    op.execute("CREATE INDEX IF NOT EXISTS idx_calls_direction ON calls (direction)")


def downgrade() -> None:
    # Only twilio_cost is dropped. stt_cost and tts_cost may predate this
    # migration in some environments, and dropping a column someone else added
    # would destroy data this migration never created.
    op.execute("DROP INDEX IF EXISTS idx_calls_direction")
    op.execute("ALTER TABLE calls DROP COLUMN IF EXISTS direction")
    op.execute("ALTER TABLE calls DROP COLUMN IF EXISTS twilio_cost")