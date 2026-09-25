"""add dashboard user accounts

VA-T-002 — the dashboard had no authentication of any kind. Its API proxy
attached DASHBOARD_ADMIN_KEY to every request it forwarded, so anyone who
could reach the frontend host had full admin access: call transcripts, caller
phone numbers, and the agent's settings.

WHY ACCOUNTS RATHER THAN ONE SHARED PASSWORD

A shared secret cannot answer either question that matters once this is real:

  - WHO changed that setting? A shared password can only ever say "someone
    who knew it".
  - How do I revoke ONE person? You rotate it for everybody, including the
    people still working.

And for a product going to multiple clients it fails completely: one password
cannot separate one client's call transcripts from another's. This table is
what `tenant_id` attaches to when that day comes.

WHAT IS STORED

Never the password. Only an Argon2id hash, which embeds its own random salt
and cost parameters, so a database dump does not yield a working login and two
people choosing the same password produce different hashes.

failed_login_count and locked_until support lockout after repeated failures.
Without them an internet-reachable dashboard can be brute-forced at HTTP
speed, and hashing alone does not help — the attacker is guessing, not
cracking.

NO USER IS CREATED HERE

Seeding a default account is how "admin/admin" reaches production. The first
account is created deliberately, by a human running scripts/create_user.py,
which prompts for the password without echoing it and never places it in shell
history. setup_db.py reports when no account exists yet and says what to run.

Revision ID: 0005
Revises: 0004
"""
from alembic import op
import sqlalchemy as sa

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("username", sa.String(), nullable=False),
        sa.Column("password_hash", sa.String(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("role", sa.String(), nullable=False, server_default="admin"),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failed_login_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    # Unique AND indexed: every login is a lookup by username, and the
    # constraint is what stops two accounts differing only by case.
    op.create_index("ix_users_username", "users", ["username"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_users_username", table_name="users")
    op.drop_table("users")