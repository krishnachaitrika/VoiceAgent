-- SUPERSEDED by alembic/versions/0002_drop_language_columns.py — that
-- migration does the same two drops (also via DROP COLUMN IF EXISTS) but as
-- a proper versioned Alembic revision, so `alembic upgrade head` on a fresh
-- database now creates AND drops these columns as part of one recorded
-- history, and `alembic history` shows the removal for anyone auditing an
-- existing database. Prefer `alembic upgrade head` over running this script
-- by hand from here on; this file is kept only for reference / for a
-- database managed outside Alembic entirely.
--
-- Run once against your live Supabase/Postgres database to match the
-- English-only build's ORM models (database/models.py no longer defines
-- Call.language_detected or Lead.language — Base.metadata.create_all in
-- scripts/setup_db.py only ever CREATEs tables/columns, it never drops
-- ones that already exist on a database that's already been set up, so
-- this has to be applied by hand).
--
-- Safe to run any time — the application code stopped reading/writing
-- these columns as part of this change, so dropping them loses only the
-- historical "what language was this call/lead in" value, nothing else.

ALTER TABLE calls DROP COLUMN IF EXISTS language_detected;
ALTER TABLE leads DROP COLUMN IF EXISTS language;
