"""add twilio price reconciliation columns

Records WHETHER a Twilio cost is a measured fact or a calculated guess.

WHY

`twilio_cost` was `ceil(duration/60) x rate_from_env`. That estimate is only as
good as the rate someone typed, it is valid for exactly one destination
country, and it goes quietly stale the day Twilio changes pricing. In all three
cases the stored number still looks entirely credible, which is what makes it
dangerous for anything financial.

billing/twilio_reconcile.py now fetches the amount Twilio actually charged and
overwrites the estimate. These columns record which of the two any row holds:

  twilio_cost_source   "estimated" — duration x configured rate
                       "actual"    — fetched from Twilio's Call resource
  twilio_currency      the account's billing currency (Twilio does not bill
                       everyone in USD, and silently summing mixed currencies
                       would produce a meaningless total)

Existing rows are backfilled as "estimated", which is what they are. They are
NOT retro-reconciled: Twilio retains call records for a limited window, so a
backfill would succeed for recent calls and fail for older ones, producing a
table where "actual" means "recent enough that the fetch worked" rather than
"measured". Leaving history honestly labelled is more useful than a partially
reconciled one.

Revision ID: 0004
Revises: 0003
"""
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE calls ADD COLUMN IF NOT EXISTS "
        "twilio_cost_source VARCHAR DEFAULT 'estimated'"
    )
    op.execute(
        "ALTER TABLE calls ADD COLUMN IF NOT EXISTS twilio_currency VARCHAR DEFAULT 'USD'"
    )

    op.execute(
        "UPDATE calls SET twilio_cost_source = 'estimated' WHERE twilio_cost_source IS NULL"
    )
    op.execute("UPDATE calls SET twilio_currency = 'USD' WHERE twilio_currency IS NULL")

    # "show me every call still on an estimate" is the query this table will be
    # asked most often once reconciliation is running — it is how you spot a
    # reconciliation job that has quietly stopped working.
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_calls_twilio_cost_source "
        "ON calls (twilio_cost_source)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_calls_twilio_cost_source")
    op.execute("ALTER TABLE calls DROP COLUMN IF EXISTS twilio_currency")
    op.execute("ALTER TABLE calls DROP COLUMN IF EXISTS twilio_cost_source")