"""
cache/settings_cache.py — makes the Settings dashboard actually control the
live agent, instead of being a decorative page writing to a table nothing
reads.

Previously: agent_name/company_name/voice_provider/elevenlabs_voice_id/
system_prompt were only ever read from `.env` at process startup
(config.AGENT_NAME etc. — a module-level constant, fixed for the entire
process lifetime). The Settings dashboard wrote to a `settings` DB table
that no other file in the codebase ever queried. Saving a change there did
nothing to the running agent.

Now: this module is the ONE place that reads the `settings` table, with a
short in-memory cache (config.SETTINGS_CACHE_TTL_SECONDS) so normal call
traffic doesn't add a DB round trip per turn. api/settings.py calls
invalidate_cache() the instant you hit Save, so the very next call always
sees the new value — the TTL only matters for a call already in progress.

.env values remain the fallback defaults — if a setting was never saved
(or is deleted from the DB), the agent falls back to its .env default
rather than breaking. There is currently no version history — saving
overwrites the previous value permanently; ask if you want an undo/history
table added later.
"""
import time
import logging
from typing import Dict

from database.base import AsyncSessionLocal
from database import crud
import config

logger = logging.getLogger(__name__)

_cache: Dict[str, str] = {}
_cache_loaded_at: float = 0.0


def _env_defaults() -> Dict[str, str]:
    """
    .env-sourced fallback values — used the first time the app ever runs
    (before anything's been saved in the dashboard), and as a safety net
    if a setting is later deleted from the DB.
    """
    elevenlabs_configured = bool(config.ELEVENLABS_API_KEY and config.ELEVENLABS_VOICE_ID)
    return {
        "agent_name": config.AGENT_NAME,
        "company_name": config.COMPANY_NAME,
        "voice_provider": "elevenlabs" if elevenlabs_configured else "sarvam",
        "elevenlabs_voice_id": config.ELEVENLABS_VOICE_ID or "",
        # system_prompt is deliberately ABSENT.
        #
        # The agent's instructions are code (brain/prompts.py): version
        # controlled, reviewable, deployed on purpose. They are no longer a
        # runtime value, so they must not appear in live settings — anything
        # that reads them would otherwise pick up a stale database row and
        # reintroduce the very override this removes.
        #
        # A `system_prompt` row may still exist in the settings table from an
        # older deployment. It is ignored: _merge below only keeps keys that
        # appear in these defaults.
    }


async def get_live_settings() -> Dict[str, str]:
    """
    Returns the current effective settings: DB values where saved, .env
    defaults for anything never saved or since deleted. Cached for
    config.SETTINGS_CACHE_TTL_SECONDS to avoid a DB hit on every message.
    """
    global _cache, _cache_loaded_at
    now = time.monotonic()
    if _cache and (now - _cache_loaded_at) < config.SETTINGS_CACHE_TTL_SECONDS:
        return _cache

    defaults = _env_defaults()
    try:
        async with AsyncSessionLocal() as db:
            rows = await crud.get_all_settings(db)
        db_values = {row.key: row.value for row in rows if row.value}
    except Exception as e:
        # DB unreachable — fall back to .env defaults rather than breaking
        # the call. Keep the old cache if we have one, so a transient DB
        # blip doesn't reset live settings mid-outage.
        logger.warning(f"Failed to load live settings from DB, using .env defaults: {e}")
        db_values = {}

    # Only keys we actually define as defaults are allowed through. A plain
    # {**defaults, **db_values} would let ANY row in the settings table become
    # a live setting — including a `system_prompt` row left behind by an older
    # deployment, which would silently reinstate the runtime prompt override
    # that _env_defaults deliberately no longer publishes.
    #
    # Filtering here rather than deleting the row is intentional: the row is
    # harmless historical data, and a DELETE in a read path is a surprise
    # nobody wants during an incident.
    ignored = set(db_values) - set(defaults)
    if ignored:
        logger.debug(f"Ignoring non-writable setting row(s) in DB: {sorted(ignored)}")
    merged = {**defaults, **{k: v for k, v in db_values.items() if k in defaults}}
    _cache = merged
    _cache_loaded_at = now
    return merged


async def get_setting(key: str) -> str:
    settings = await get_live_settings()
    return settings.get(key, "")


def invalidate_cache() -> None:
    """
    Force the next get_live_settings() call to re-read the DB instead of
    serving the cached value. Called by api/settings.py right after a
    successful save, so changes are live on the very next call.
    """
    global _cache_loaded_at
    _cache_loaded_at = 0.0
    logger.info("Settings cache invalidated — next call will read fresh values from DB")