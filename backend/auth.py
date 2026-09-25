"""
auth.py — dashboard API authentication (VA-B1 fix).

api/dashboard.py, api/settings.py, api/documents.py and api/analytics.py
previously had no auth dependency at all — every route's only dependency
was Depends(get_db), so anyone who could reach the port could read every
call transcript and lead's PII, and overwrite the agent's system prompt or
the knowledge base with nothing but curl.

Two tiers, both shared-secret bearer tokens compared with hmac.compare_digest
(never a plain == on a secret):
  - require_dashboard_auth: read access — accepts EITHER key.
  - require_dashboard_admin: write access to settings/knowledge-base —
    accepts ONLY the admin key. Settings control the live system prompt and
    the knowledge base feeds every caller's answers, so writes need the
    stronger tier per the QA report's explicit call-out.

Fails closed: an unset (empty) configured key never matches anything, so a
deployment that forgets to set DASHBOARD_API_KEY/DASHBOARD_ADMIN_KEY locks
the dashboard out entirely rather than leaving it open by default.
"""
import hmac
from typing import Optional

from fastapi import Header, HTTPException

import config


def _extract_bearer_token(authorization: Optional[str]) -> Optional[str]:
    if not authorization:
        return None
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return None
    return token


def _matches(expected: str, provided: Optional[str]) -> bool:
    return bool(expected) and bool(provided) and hmac.compare_digest(expected, provided)


async def require_dashboard_auth(authorization: Optional[str] = Header(default=None)) -> None:
    token = _extract_bearer_token(authorization)
    if _matches(config.DASHBOARD_API_KEY, token) or _matches(config.DASHBOARD_ADMIN_KEY, token):
        return
    raise HTTPException(status_code=401, detail="Unauthorized")


async def require_dashboard_admin(authorization: Optional[str] = Header(default=None)) -> None:
    token = _extract_bearer_token(authorization)
    if _matches(config.DASHBOARD_ADMIN_KEY, token):
        return
    raise HTTPException(status_code=401, detail="Unauthorized")
