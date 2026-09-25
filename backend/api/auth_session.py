"""
api/auth_session.py — dashboard login (VA-T-002).

WHERE THE CREDENTIAL CHECK LIVES, AND WHY

The password hashes are in Postgres, which only the backend talks to — so the
backend has to be what verifies a login. The Next.js frontend cannot check a
password it has no way to read.

The flow is therefore:

    browser → Next /api/auth/login → backend POST /api/auth/login
                                          ↓ verifies against users table
                                     ← {token, expires_at, username}
             ← sets httpOnly cookie ←

The frontend never sees the password after forwarding it, never sees a hash,
and stores only the returned token in a cookie JavaScript cannot read.

WHY A SIGNED TOKEN AND NOT A SESSION TABLE

The token is an HMAC over "<user id>.<username>.<expiry>", keyed on
DASHBOARD_SESSION_SECRET. That makes it verifiable by the frontend middleware
with no database round trip — important, because middleware runs on EVERY
request including static navigation, and a query per page load would be a
needless tax.

The trade-off is honest: a signed token cannot be revoked before it expires.
Deactivating a user stops them logging in again but does not kill a live
session until DASHBOARD_SESSION_TTL_HOURS elapses. That is acceptable at this
scale and with a 12 hour default; rotating DASHBOARD_SESSION_SECRET is the
immediate lever if a session must die now, and it invalidates every session at
once.

These endpoints are deliberately NOT behind require_dashboard_auth. They are
how you obtain access in the first place — gating them would be a loop. They
are rate limited instead.
"""
import hashlib
import hmac
import logging
import time
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

import config
from database.base import get_db
from rate_limit import limiter
from services.user_auth import authenticate

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/auth", tags=["auth"])


class LoginRequest(BaseModel):
    username: str
    password: str


def _sign(payload: str) -> str:
    return hmac.new(
        config.DASHBOARD_SESSION_SECRET.encode(),
        payload.encode(),
        hashlib.sha256,
    ).hexdigest()


def create_session_token(user_id: str, username: str) -> tuple[str, int]:
    """Return (token, expires_at_epoch_seconds)."""
    expires_at = int(time.time()) + config.DASHBOARD_SESSION_TTL_HOURS * 3600
    payload = f"{user_id}.{username}.{expires_at}"
    return f"{payload}.{_sign(payload)}", expires_at


def verify_session_token(token: Optional[str]) -> Optional[dict]:
    """Validate a token's signature and expiry. Returns its claims or None.

    Used by the backend itself; the frontend middleware performs the same
    check independently using the shared secret.
    """
    if not token or not config.DASHBOARD_SESSION_SECRET:
        return None

    parts = token.split(".")
    if len(parts) != 4:
        return None

    user_id, username, expires_raw, signature = parts
    payload = f"{user_id}.{username}.{expires_raw}"

    # compare_digest, never ==: a plain comparison returns early at the first
    # differing byte, leaking the signature's prefix through timing.
    if not hmac.compare_digest(signature, _sign(payload)):
        return None

    try:
        expires_at = int(expires_raw)
    except ValueError:
        return None

    if time.time() >= expires_at:
        return None

    return {"user_id": user_id, "username": username, "expires_at": expires_at}


@router.post("/login")
@limiter.limit(config.RATE_LIMIT_LOGIN)
async def login(
    request: Request,
    body: LoginRequest,
    db: AsyncSession = Depends(get_db),
):
    """Verify credentials and issue a session token.

    Rate limited per client because hashing protects a leaked database, not a
    login endpoint — an attacker guessing passwords never sees a hash. The
    per-account lockout in services/user_auth.py covers a single account; this
    limit covers someone spraying one common password across many usernames,
    which lockout alone would not catch.
    """
    if not config.DASHBOARD_SESSION_SECRET:
        # An operator error, stated plainly — whoever sees this needs to know
        # what to set, and it reveals nothing to an attacker.
        raise HTTPException(
            status_code=503,
            detail=(
                "Dashboard sessions are not configured: set "
                "DASHBOARD_SESSION_SECRET in .env and restart."
            ),
        )

    user, reason = await authenticate(db, body.username, body.password)
    if user is None:
        # The username is logged, the password never is — not even a length.
        logger.warning(
            f"Failed dashboard login for {body.username!r} from {request.client}"
        )
        raise HTTPException(status_code=401, detail=reason)

    token, expires_at = create_session_token(user.id, user.username)
    logger.info(f"Dashboard login: {user.username!r}")
    return {
        "token": token,
        "expires_at": expires_at,
        "username": user.username,
        "role": user.role,
    }


@router.get("/me")
async def whoami(authorization: Optional[str] = None):
    """Report the identity behind a session token.

    Lets the dashboard show who is signed in without a second source of
    truth about the session.
    """
    token = None
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization.split(" ", 1)[1]

    claims = verify_session_token(token)
    if claims is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return {"username": claims["username"], "expires_at": claims["expires_at"]}