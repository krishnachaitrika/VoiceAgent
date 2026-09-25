import logging
from datetime import timezone
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from database.base import get_db
from database import crud
from cache.settings_cache import invalidate_cache
from auth import require_dashboard_auth, require_dashboard_admin

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/settings", dependencies=[Depends(require_dashboard_auth)])


def _utc_iso(dt) -> Optional[str]:
    """Serialize a system-clock timestamp (func.now()/onupdate=func.now(),
    a real UTC instant) as an explicit UTC ISO string — see
    api/dashboard.py's _utc_iso for the full explanation and the real
    dashboard bug this was written to fix."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


# WRITABLE SETTINGS — AN ALLOW-LIST, NOT A UI DECISION.
#
# This endpoint used to accept ANY key, so `system_prompt` could be rewritten
# through it — changing what the agent says to real customers, live, within
# SETTINGS_CACHE_TTL_SECONDS, with no review and no version history.
#
# The prompt now lives ONLY in brain/prompts.py, where it is version
# controlled, reviewable in a diff, and deployed deliberately. Removing the
# field from the dashboard alone would not have been enough: the button would
# be gone but a single curl with the admin key could still set it. The control
# has to live here, on the server.
#
# These four remain writable on purpose — they are operational (swapping voice
# provider mid-incident) or cosmetic, and none of them change what the agent
# is instructed to do.
WRITABLE_SETTING_KEYS = frozenset(
    {"agent_name", "company_name", "voice_provider", "elevenlabs_voice_id"}
)

# Explicitly named so the error explains WHY rather than just refusing.
CODE_MANAGED_SETTING_KEYS = frozenset({"system_prompt"})


class SettingUpdate(BaseModel):
    key: str
    value: str


@router.get("")
async def get_settings(db: AsyncSession = Depends(get_db)):
    """Return all settings as a key-value list."""
    try:
        settings = await crud.get_all_settings(db)
        return {
            "settings": [
                {
                    "key": s.key,
                    "value": s.value,
                    "updated_at": _utc_iso(s.updated_at),
                }
                for s in settings
            ]
        }
    except Exception as e:
        logger.error(f"Get settings error: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch settings")


@router.post("", dependencies=[Depends(require_dashboard_admin)])
async def update_setting(body: SettingUpdate, db: AsyncSession = Depends(get_db)):
    """Create or update a setting."""
    if body.key in CODE_MANAGED_SETTING_KEYS:
        logger.warning(
            f"Refused write to code-managed setting {body.key!r} — the agent's "
            f"instructions live in brain/prompts.py and are changed by deploy, "
            f"not at runtime."
        )
        raise HTTPException(
            status_code=403,
            detail=(
                f"'{body.key}' is managed in code (brain/prompts.py), not at runtime. "
                f"Change it in a commit and deploy."
            ),
        )
    if body.key not in WRITABLE_SETTING_KEYS:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Unknown setting '{body.key}'. Writable settings: "
                f"{', '.join(sorted(WRITABLE_SETTING_KEYS))}."
            ),
        )
    try:
        await crud.upsert_setting(db, key=body.key, value=body.value)
        invalidate_cache()
        return {"message": "Setting updated", "key": body.key}
    except Exception as e:
        logger.error(f"Update setting error: {e}")
        raise HTTPException(status_code=500, detail="Failed to update setting")