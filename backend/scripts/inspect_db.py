"""
scripts/inspect_db.py — READ-ONLY database inspection.

Run this BEFORE `alembic stamp head` or any migration, to establish exactly
what your live database contains and how it differs from what the code
expects.

THIS SCRIPT NEVER WRITES. No CREATE, no ALTER, no DROP, no INSERT. It only
issues SELECTs against information_schema and pg_indexes, plus row counts. It
is safe to run against production at any time.

    cd backend
    python scripts/inspect_db.py

What it reports:

  1. Alembic state      — is this database already under Alembic control?
  2. Tables             — which of the 8 expected tables exist
  3. Columns            — per table, live vs database/models.py, in both
                          directions (missing AND extra)
  4. Indexes            — including whether a pgvector index exists
  5. Row counts         — so you know how much real data is at stake
  6. Verdict            — the specific next command for YOUR situation

Exit codes: 0 = inspected fine, 1 = could not connect. A schema mismatch is
reported, not treated as a failure — the whole point is to see it.
"""
import asyncio
import os
import sys
from collections import OrderedDict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text  # noqa: E402

from database.base import get_engine  # noqa: E402
from database.models import Base  # noqa: E402
import config  # noqa: E402


# ── Formatting helpers ───────────────────────────────────────────────────────

def h1(title: str) -> None:
    print(f"\n{'═' * 78}\n {title}\n{'═' * 78}")


def h2(title: str) -> None:
    print(f"\n── {title} " + "─" * max(0, 74 - len(title)))


OK, WARN, BAD, INFO = "  OK  ", " WARN ", " DIFF ", "      "


# ── What the code expects, read straight from the ORM ────────────────────────

def expected_schema() -> "OrderedDict[str, list[str]]":
    """Authoritative because it is the same metadata the app runs against —
    no second copy to drift out of sync."""
    out: "OrderedDict[str, list[str]]" = OrderedDict()
    for table_name, table in Base.metadata.tables.items():
        out[table_name] = [c.name for c in table.columns]
    return out


# ── Live inspection queries (all read-only) ──────────────────────────────────

SQL_TABLES = """
SELECT table_name
FROM information_schema.tables
WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
ORDER BY table_name
"""

SQL_COLUMNS = """
SELECT table_name, column_name, data_type, is_nullable, column_default
FROM information_schema.columns
WHERE table_schema = 'public'
ORDER BY table_name, ordinal_position
"""

SQL_INDEXES = """
SELECT tablename, indexname, indexdef
FROM pg_indexes
WHERE schemaname = 'public'
ORDER BY tablename, indexname
"""

SQL_EXTENSIONS = "SELECT extname, extversion FROM pg_extension ORDER BY extname"

SQL_ALEMBIC = "SELECT version_num FROM alembic_version"

SQL_ALEMBIC_EXISTS = """
SELECT EXISTS (
  SELECT 1 FROM information_schema.tables
  WHERE table_schema = 'public' AND table_name = 'alembic_version'
)
"""


async def inspect() -> None:
    expected = expected_schema()

    h1("VOICE AGENT — DATABASE INSPECTION (read-only)")
    # Never print the full URL; it contains the password.
    url = config.DATABASE_URL or ""
    host = url.split("@")[-1].split("/")[0] if "@" in url else "(not set)"
    print(f" Target host : {host}")
    print(f" Models      : database/models.py — {len(expected)} tables expected")
    print(f" Environment : {getattr(config, 'ENVIRONMENT', 'unknown')}")

    if not url:
        print("\n DATABASE_URL is not set. Set it in backend/.env and re-run.")
        sys.exit(1)

    engine = get_engine()

    try:
        async with engine.connect() as conn:
            live_tables = {r[0] for r in (await conn.execute(text(SQL_TABLES)))}

            live_cols: "OrderedDict[str, list[tuple]]" = OrderedDict()
            for row in await conn.execute(text(SQL_COLUMNS)):
                live_cols.setdefault(row[0], []).append((row[1], row[2], row[3], row[4]))

            indexes: "OrderedDict[str, list[tuple]]" = OrderedDict()
            for row in await conn.execute(text(SQL_INDEXES)):
                indexes.setdefault(row[0], []).append((row[1], row[2]))

            extensions = {r[0]: r[1] for r in (await conn.execute(text(SQL_EXTENSIONS)))}

            alembic_present = (await conn.execute(text(SQL_ALEMBIC_EXISTS))).scalar()
            alembic_rev = None
            if alembic_present:
                alembic_rev = (await conn.execute(text(SQL_ALEMBIC))).scalar()

            counts = {}
            for t in sorted(live_tables & set(expected)):
                # Safe: table name comes from information_schema, not user input.
                counts[t] = (await conn.execute(text(f'SELECT COUNT(*) FROM "{t}"'))).scalar()

    except Exception as e:
        print(f"\n Could not connect or query: {e}")
        print("\n Check DATABASE_URL in backend/.env. It must use the asyncpg")
        print(" driver, i.e. postgresql+asyncpg://...")
        sys.exit(1)
    finally:
        await engine.dispose()

    # ── 1. Alembic state ─────────────────────────────────────────────────────
    h2("1. Alembic migration state")
    if not alembic_present:
        print(f"{INFO}No alembic_version table — this database is NOT yet under")
        print(f"{INFO}Alembic control. That is expected if it was created by")
        print(f"{INFO}scripts/setup_db.py.")
    else:
        print(f"{OK}alembic_version exists — current revision: {alembic_rev!r}")

    # ── 2. Extensions ────────────────────────────────────────────────────────
    h2("2. Extensions")
    if "vector" in extensions:
        print(f"{OK}pgvector installed (version {extensions['vector']})")
    else:
        print(f"{WARN}pgvector NOT installed — the documents table and all")
        print(f"{INFO}knowledge-base search will fail.")

    # ── 3. Tables ────────────────────────────────────────────────────────────
    h2("3. Tables")
    missing_tables = [t for t in expected if t not in live_tables]
    extra_tables = sorted(live_tables - set(expected) - {"alembic_version"})

    for t in expected:
        if t in live_tables:
            print(f"{OK}{t:<20} {counts.get(t, 0):>8} rows")
        else:
            print(f"{BAD}{t:<20} MISSING")
    for t in extra_tables:
        print(f"{INFO}{t:<20} (extra — not defined in models.py)")

    # ── 4. Columns ───────────────────────────────────────────────────────────
    h2("4. Columns — live database vs database/models.py")
    total_missing = total_extra = 0

    for table, exp_cols in expected.items():
        if table not in live_tables:
            continue
        live = [c[0] for c in live_cols.get(table, [])]
        missing = [c for c in exp_cols if c not in live]      # code expects, DB lacks
        extra = [c for c in live if c not in exp_cols]        # DB has, code ignores

        total_missing += len(missing)
        total_extra += len(extra)

        if not missing and not extra:
            print(f"{OK}{table:<20} {len(live)} columns, exact match")
            continue

        print(f"{BAD}{table:<20}")
        for c in missing:
            print(f"{INFO}    MISSING in DB : {c}   <-- code will break on this")
        for c in extra:
            print(f"{INFO}    EXTRA in DB   : {c}   <-- harmless, code ignores it")

    # ── 5. Indexes ───────────────────────────────────────────────────────────
    h2("5. Indexes")
    vector_index = None
    for table, idxs in indexes.items():
        for name, definition in idxs:
            if "hnsw" in definition.lower() or "ivfflat" in definition.lower():
                vector_index = (table, name)

    for table in expected:
        if table not in live_tables:
            continue
        idxs = indexes.get(table, [])
        non_pk = [n for n, d in idxs if not n.endswith("_pkey")]
        if non_pk:
            print(f"{OK}{table:<20} {', '.join(non_pk)}")
        else:
            print(f"{WARN}{table:<20} primary key only — no secondary indexes")

    if vector_index:
        print(f"\n{OK}Vector index present: {vector_index[1]} on {vector_index[0]}")
    else:
        print(f"\n{WARN}No pgvector (hnsw/ivfflat) index found. Every knowledge-base")
        print(f"{INFO}search is a sequential scan, on the per-turn latency path.")

    # ── 6. Verdict ───────────────────────────────────────────────────────────
    h1("VERDICT — what to run next")

    if total_missing:
        print(" Your database is MISSING columns the code expects.")
        print(" Re-running scripts/setup_db.py will NOT fix this — create_all")
        print(" only creates missing TABLES, never adds columns to existing ones.")
        print(" You need a migration that ALTERs those tables.\n")
    elif missing_tables:
        print(" Tables are missing. On an empty/new database run:")
        print("     alembic upgrade head\n")
    else:
        print(" Every table and column the code expects is present.")
        print(" No data migration is needed.\n")

    if not alembic_present:
        print(" To adopt Alembic without re-running any DDL on this existing")
        print(" database (safe, writes only a version marker):")
        print("     cd backend && alembic stamp head\n")
        print(" Verify afterwards with:")
        print("     alembic current\n")
    else:
        print(f" Already under Alembic control at revision {alembic_rev!r}.")
        print(" Check for pending migrations with:")
        print("     alembic current && alembic heads\n")

    if total_extra:
        print(f" {total_extra} extra column(s) exist that the code no longer uses.")
        print(" These are harmless — leave them alone unless you have a reason.")
        print(" Dropping columns is irreversible and gains you nothing here.\n")

    print(" Nothing was modified. This script is read-only.")


if __name__ == "__main__":
    asyncio.run(inspect())