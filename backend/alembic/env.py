import asyncio
import sys
from logging.config import fileConfig
from pathlib import Path

from sqlalchemy import pool
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context

# backend/ isn't on sys.path when alembic is invoked from elsewhere.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config as app_config  # noqa: E402
from database.models import Base  # noqa: E402

alembic_config = context.config

if alembic_config.config_file_name is not None:
    fileConfig(alembic_config.config_file_name)

# Source of truth for the schema is database/models.py, not a hardcoded URL
# in alembic.ini — same DATABASE_URL the app itself connects with, and what
# `alembic revision --autogenerate` diffs future model changes against.
#
# Kept OUT of alembic_config (never passed through set_main_option /
# get_section) rather than the more obvious approach — configparser
# interpolation chokes on a literal "%" in the URL (a real password with a
# URL-encoded "%40" broke this the first way it was written), so the URL is
# threaded through as a plain Python string instead.
db_url = app_config.DATABASE_URL
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=db_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def _do_run_migrations(connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    connectable = async_engine_from_config(
        {"sqlalchemy.url": db_url},
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(_do_run_migrations)
    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
