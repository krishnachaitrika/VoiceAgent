"""
services/user_auth.py — password hashing and login verification (VA-T-002).

WHY ARGON2id

It is the Password Hashing Competition winner and the current OWASP first
choice. The property that matters is that it is deliberately expensive in
*memory* as well as time: bcrypt's cost is CPU-only, so a GPU or ASIC farm
parallelises it cheaply, while Argon2id's memory cost makes that hardware
advantage far smaller.

The hash string embeds its own random salt and cost parameters, so:
  - two people choosing the same password produce different hashes
  - raising the cost later does not invalidate existing hashes; they are
    re-hashed transparently on the owner's next successful login

WHY LOCKOUT, GIVEN THE HASHING

Hashing protects the passwords if the database leaks. It does nothing against
someone guessing at the login endpoint — that attacker never sees a hash, they
just try passwords. An internet-reachable dashboard with no lockout can be
attacked at HTTP speed indefinitely.

So failures are counted per account and the account locks for a spell after
too many. The counter resets on success, and the lock expires on its own so a
genuine user who mistyped is not permanently shut out and nobody has to run a
manual unlock at 2am.

WHAT THE CALLER IS TOLD

Login failures return ONE message regardless of cause — unknown username,
wrong password, deactivated account. Distinguishing them is a username oracle:
an attacker who can tell "no such user" from "wrong password" can enumerate
valid accounts and then concentrate on those.

Lockout is the deliberate exception. Staying silent there means a locked-out
colleague retries forever with the right password and no idea why it fails,
and an attacker who triggered the lock can infer it from timing anyway.
"""
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

import config
from database.models import User

logger = logging.getLogger(__name__)

# Defaults from argon2-cffi, which tracks the RFC 9106 recommendations. Left
# at the library's values on purpose: hand-tuning these without benchmarking
# on the target hardware usually makes them worse, and the library updates
# them as guidance moves.
_hasher = PasswordHasher()


def hash_password(password: str) -> str:
    """Hash a password for storage. The salt is generated internally."""
    return _hasher.hash(password)


def normalize_username(username: str) -> str:
    """Usernames are stored and compared lowercase.

    "Velu" and "velu" being two separate accounts is a support problem with no
    upside, and it also quietly weakens the unique constraint.
    """
    return (username or "").strip().lower()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _as_aware(dt: Optional[datetime]) -> Optional[datetime]:
    """Treat a naive timestamp as UTC.

    Every DateTime column is timezone=True, but a value that has been through
    a driver or a cache can still arrive naive — and comparing naive to aware
    raises TypeError, which here would mean a failed login taking down the
    endpoint rather than returning 401.
    """
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


async def get_user(db: AsyncSession, username: str) -> Optional[User]:
    result = await db.execute(
        select(User).where(User.username == normalize_username(username))
    )
    return result.scalar_one_or_none()


async def create_user(
    db: AsyncSession, username: str, password: str, role: str = "admin"
) -> User:
    """Create an account. Raises ValueError if the username is taken."""
    username = normalize_username(username)
    if not username:
        raise ValueError("Username cannot be empty")
    if await get_user(db, username):
        raise ValueError(f"User {username!r} already exists")

    user = User(
        id=str(uuid.uuid4()),
        username=username,
        password_hash=hash_password(password),
        role=role,
        is_active=True,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    logger.info(f"Created dashboard user {username!r} with role {role!r}")
    return user


async def set_password(db: AsyncSession, user: User, password: str) -> None:
    user.password_hash = hash_password(password)
    user.failed_login_count = 0
    user.locked_until = None
    await db.commit()
    logger.info(f"Password changed for dashboard user {user.username!r}")


async def count_users(db: AsyncSession) -> int:
    return (await db.execute(select(func.count(User.id)))).scalar() or 0


async def authenticate(
    db: AsyncSession, username: str, password: str
) -> Tuple[Optional[User], str]:
    """Verify credentials.

    Returns (user, "") on success, or (None, reason) on failure. The reason is
    safe to show the caller: it is the same string for every credential
    failure, and only differs for lockout.
    """
    generic_failure = "Incorrect username or password."
    user = await get_user(db, username)

    if user is None:
        # Hash the submitted password anyway. Returning early here would make
        # an unknown username measurably faster than a wrong password, which
        # is a timing oracle for enumerating valid accounts.
        _hasher.hash(password or "x")
        return None, generic_failure

    locked_until = _as_aware(user.locked_until)
    if locked_until and locked_until > _now():
        remaining = int((locked_until - _now()).total_seconds() / 60) + 1
        return None, (
            f"Account locked after too many failed attempts. "
            f"Try again in {remaining} minute(s)."
        )

    if not user.is_active:
        # Deliberately the generic message: whether an account exists but is
        # disabled is still information about who works here.
        return None, generic_failure

    try:
        _hasher.verify(user.password_hash, password or "")
    except (VerifyMismatchError, InvalidHashError):
        user.failed_login_count = (user.failed_login_count or 0) + 1
        if user.failed_login_count >= config.LOGIN_MAX_FAILED_ATTEMPTS:
            user.locked_until = _now() + timedelta(
                minutes=config.LOGIN_LOCKOUT_MINUTES
            )
            logger.warning(
                f"Dashboard account {user.username!r} locked for "
                f"{config.LOGIN_LOCKOUT_MINUTES} minute(s) after "
                f"{user.failed_login_count} failed attempts."
            )
        await db.commit()
        return None, generic_failure

    # Success. Re-hash if the library's parameters have moved on since this
    # hash was written — the only moment the plaintext is available to do it.
    if _hasher.check_needs_rehash(user.password_hash):
        user.password_hash = hash_password(password)
        logger.info(f"Re-hashed {user.username!r} with current Argon2 parameters")

    user.failed_login_count = 0
    user.locked_until = None
    user.last_login_at = _now()
    await db.commit()
    return user, ""