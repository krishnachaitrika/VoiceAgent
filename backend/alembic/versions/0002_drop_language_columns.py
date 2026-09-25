"""Drop calls.language_detected and leads.language — English-only build.

database/models.py's Call and Lead classes have no language_detected/
language fields (the app stopped reading/writing them once the codebase
went English-only), but 0001_baseline_schema.py still creates both columns
on a fresh `alembic upgrade head` since it mirrors what models.py looked
like at the time it was written. Nobody had gone back to touch that
baseline (editing an already-applied migration is unsafe), so the only
place this drop existed was scripts/drop_language_columns.sql — a raw SQL
script applied by hand, outside Alembic's history entirely. That meant a
brand-new environment via `alembic upgrade head` still ended up with two
columns the ORM never uses, and an existing database that already had the
raw SQL run against it had no migration recording that the columns were
ever removed.

This migration is the versioned equivalent of scripts/drop_language_columns.sql
(now superseded by this migration — see the note at the top of that file).
It's safe to run even on a database that already had the raw SQL applied by
hand: both drops use `DROP COLUMN IF EXISTS`, same as the .sql script, so
re-running this against a database where the columns are already gone is a
no-op rather than an error.

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-15
"""
import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE calls DROP COLUMN IF EXISTS language_detected")
    op.execute("ALTER TABLE leads DROP COLUMN IF EXISTS language")


def downgrade() -> None:
    # Re-add with the exact type/default/nullability from 0001_baseline_schema.py.
    # `default=` here matches the baseline exactly — a client-side SQLAlchemy
    # default applied on INSERT via the ORM/Core, not a database-level
    # DEFAULT constraint (0001 never set one either), so both columns come
    # back nullable with no server-side default, same as originally created.
    op.add_column(
        "calls",
        sa.Column("language_detected", sa.String(), default="en-IN"),
    )
    op.add_column(
        "leads",
        sa.Column("language", sa.String(), default="en-IN"),
    )
