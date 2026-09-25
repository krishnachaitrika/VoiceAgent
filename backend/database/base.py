from functools import lru_cache

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
import config


@lru_cache
def get_engine():
    """Built on first use, not at import time — importing this module (or
    anything that imports it) used to require a reachable config.DATABASE_URL
    immediately, which is why nothing could be imported for a test without a
    live Postgres."""
    return create_async_engine(
        config.DATABASE_URL,
        echo=False,
        pool_pre_ping=True,
        # Sized to comfortably outlive config.MAX_CONCURRENT_CALLS (VA-B3
        # fix) — each call holds at most one session at a time, but
        # dashboard/API requests share this same pool, hence the overflow
        # headroom on top of the call cap rather than an exact match.
        pool_size=config.MAX_CONCURRENT_CALLS,
        max_overflow=config.MAX_CONCURRENT_CALLS,
    )


@lru_cache
def _sessionmaker():
    return async_sessionmaker(bind=get_engine(), class_=AsyncSession, expire_on_commit=False)


def AsyncSessionLocal() -> AsyncSession:
    """Kept as a callable (not the eager async_sessionmaker instance it used
    to be) so every existing `async with AsyncSessionLocal() as db:` call
    site keeps working unchanged, while the engine behind it is now lazy."""
    return _sessionmaker()()


async def get_db() -> AsyncSession:
    async with AsyncSessionLocal() as session:
        try:
            yield session
        finally:
            await session.close()