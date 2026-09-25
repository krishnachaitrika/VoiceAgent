"""
One-command database setup. Safe to run on an empty database or an existing one.

    python scripts/setup_db.py

WHAT CHANGED AND WHY

This script used to call `Base.metadata.create_all`, which has a trap: it
creates missing TABLES but silently ignores everything else. Add a column to a
model and re-run it, and it does nothing at all — no error, no warning. You
find out when a query fails mid-call, in front of a customer.

It now runs the Alembic migrations instead. Same single command, but the schema
has ONE definition (alembic/versions/) rather than two that can drift apart.
Adding the DDL here as well would mean every future column has to be written
twice, and the day someone updates one and forgets the other, dev and
production quietly diverge — a far worse failure than forgetting a command,
because nothing errors.

So: one command to run, one source of truth for the schema.

SAFE TO RE-RUN

  - pgvector          CREATE EXTENSION IF NOT EXISTS
  - schema            Alembic applies only migrations not yet applied
  - default settings  INSERT ONLY IF MISSING (see below)

That last point is a behaviour change worth knowing about. The old version
called upsert_setting(), which OVERWRITES. Re-running it reset system_prompt,
agent_name, voice_provider and elevenlabs_voice_id back to code defaults —
silently wiping anything tuned through the dashboard. Now existing values are
left alone and only missing keys are seeded.
"""
import asyncio
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text

from database.base import AsyncSessionLocal, get_engine
from database.crud import get_setting, upsert_setting
import config

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


async def enable_pgvector() -> None:
    print("  ➜ Enabling pgvector extension...")
    # get_engine() rather than a module-level `engine`: database/base.py
    # constructs the engine lazily so importing it never opens a connection.
    async with get_engine().begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
    print("  ✅ pgvector enabled")


def run_migrations() -> None:
    """Apply every migration not yet applied.

    Run as a subprocess rather than through Alembic's Python API: Alembic
    manages its own database connections and event loop, and mixing that with
    the async engine this script already holds open causes hard-to-diagnose
    hangs.
    """
    print("  ➜ Applying database migrations (alembic upgrade head)...")
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=BACKEND_DIR,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print("  ❌ Migrations FAILED — the database was not changed.\n")
        print(result.stdout)
        print(result.stderr)
        print(
            "  If this database predates Alembic (created by an older\n"
            "  setup_db.py), its tables already exist but Alembic does not\n"
            "  know that. Tell it the schema is current, once:\n\n"
            "      alembic stamp head\n\n"
            "  then re-run this script. `stamp` writes a version marker only —\n"
            "  it executes no DDL and touches no data."
        )
        sys.exit(1)

    # Alembic writes its progress to stderr, which is normal, not an error.
    output = (result.stdout + result.stderr).strip()
    applied = [ln for ln in output.splitlines() if "Running upgrade" in ln]
    if applied:
        for line in applied:
            print(f"     {line.strip()}")
        print(f"  ✅ {len(applied)} migration(s) applied")
    else:
        print("  ✅ Schema already up to date — nothing to apply")


async def seed_default_settings() -> None:
    """Insert defaults for keys that do not exist yet. Never overwrites.

    The dashboard's Settings page writes to this same table, so overwriting
    here would silently discard whatever the team had tuned — which is exactly
    what the previous version did on every re-run.
    """
    print("  ➜ Checking default settings...")
    defaults = {
        "agent_name": config.AGENT_NAME,
        "company_name": config.COMPANY_NAME,
        "voice_provider": "bulbul",
        "elevenlabs_voice_id": "",
        # system_prompt is deliberately NOT seeded.
        #
        # The agent's instructions live in brain/prompts.py: version
        # controlled, reviewable in a diff, and changed by deploy. Seeding a
        # copy into the settings table would recreate the runtime override
        # that api/settings.py now refuses and cache/settings_cache.py now
        # ignores — a second source of truth that can silently diverge from
        # the code actually running.
    }

    inserted, kept = [], []
    async with AsyncSessionLocal() as db:
        for key, value in defaults.items():
            existing = await get_setting(db, key)
            # `is None` deliberately, not falsiness: elevenlabs_voice_id is
            # legitimately an empty string, and `if not existing` would
            # re-seed it on every run.
            if existing is None:
                await upsert_setting(db, key, value)
                inserted.append(key)
            else:
                kept.append(key)

    if inserted:
        print(f"  ✅ Seeded: {', '.join(inserted)}")
    if kept:
        print(f"  ✅ Left unchanged (already set): {', '.join(kept)}")


async def create_vector_index() -> None:
    """Create the pgvector index on documents.embedding.

    Without it every knowledge-base search is a sequential scan computing
    cosine distance across every chunk — on the per-turn latency path, on
    every factual question. Invisible at a few hundred chunks and painful
    past a few thousand, which is exactly the point at which nobody
    remembers this step.

    HNSW rather than IVFFlat: better recall at low latency, and no list
    count to pick or rebuild as the corpus grows.

    vector_cosine_ops because rag/search.py orders by the `<=>` operator,
    which is cosine distance. An index built with a different opclass is
    simply ignored by the planner — it would sit there looking correct
    while every query still scanned the table.

    CONCURRENTLY cannot run inside a transaction block, so this is issued
    on its own connection with autocommit. It is also why this lives here
    rather than in a migration: Alembic wraps each migration in a
    transaction, and a plain CREATE INDEX would lock the table against
    writes for its duration.
    """
    print("  ➜ Ensuring pgvector index on documents.embedding...")
    engine = get_engine()
    try:
        async with engine.connect() as conn:
            await conn.execution_options(isolation_level="AUTOCOMMIT")
            await conn.execute(
                text(
                    "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_documents_embedding_hnsw "
                    "ON documents USING hnsw (embedding vector_cosine_ops)"
                )
            )
            await conn.execute(text("ANALYZE documents"))
        print("  ✅ Vector index present")
    except Exception as e:
        # Not fatal: the app works without it, just slowly. Say so plainly
        # rather than failing a setup that is otherwise complete.
        print(f"  ⚠️  Could not create the vector index ({e})")
        print("      Searches will still work but scan the whole table. Retry with:")
        print("      CREATE INDEX CONCURRENTLY idx_documents_embedding_hnsw")
        print("        ON documents USING hnsw (embedding vector_cosine_ops);")


async def report_dashboard_accounts() -> None:
    """Say whether anyone can actually log in.

    VA-T-002 added per-user accounts, and migration 0005 deliberately seeds
    NONE — a default account is how "admin/admin" reaches production. That
    leaves a real trap: a perfectly successful setup whose dashboard nobody
    can open, with nothing saying why.

    So the gap is reported here, where someone is already looking, rather
    than discovered at a login screen that only says "incorrect username or
    password".
    """
    from services.user_auth import count_users

    print("  ➜ Checking dashboard accounts...")
    try:
        async with AsyncSessionLocal() as db:
            total = await count_users(db)
    except Exception as e:
        print(f"  ⚠️  Could not check dashboard accounts: {e}")
        return

    if total:
        print(f"  ✅ {total} dashboard account(s) exist")
        return

    print("  ⚠️  NO dashboard accounts exist — nobody can sign in to the console.")
    print("      Create the first one (you will be prompted for a password):")
    print()
    print("          python scripts/create_user.py")
    print()
    print("      No default account is seeded on purpose: a shipped username")
    print("      and password is one nobody remembers to change.")


async def setup() -> None:
    print(f"🔧 Setting up {config.COMPANY_NAME} Voice Agent database...\n")

    if not config.DATABASE_URL:
        print("  ❌ DATABASE_URL is not set. Add it to backend/.env and re-run.")
        sys.exit(1)

    await enable_pgvector()
    run_migrations()
    await create_vector_index()
    await seed_default_settings()

    await report_dashboard_accounts()

    await get_engine().dispose()
    print("\n✅ Database setup complete! You can now run: python scripts/ingest_documents.py")


if __name__ == "__main__":
    asyncio.run(setup())