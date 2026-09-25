"""
scripts/create_user.py — create and manage dashboard accounts (VA-T-002).

    python scripts/create_user.py                      create a user (prompts)
    python scripts/create_user.py --username velu      username given, prompt for password
    python scripts/create_user.py --list               list accounts
    python scripts/create_user.py --reset-password velu
    python scripts/create_user.py --deactivate velu
    python scripts/create_user.py --unlock velu

WHY THE FIRST ACCOUNT IS CREATED HERE, NOT SEEDED

Three options were possible and two of them are how "admin/admin" reaches
production:

  1. Seed a default account in the migration. Every install would then share
     one known username and password until somebody remembered to change it.
     Nobody remembers.

  2. Read a bootstrap password from .env. Better, but that password then lives
     in plaintext in a file, in every backup of that file, and usually in a
     CI variable and a chat message too — and it stays valid forever because
     nothing forces a change.

  3. A human runs this, once, and types a password that is never written
     anywhere. That is this script.

The password is read with getpass, so it is not echoed to the terminal, not
written to shell history, and not visible in `ps` output — which is exactly
why it is not a --password command-line flag. Passing it as an argument would
put it in your history file and in the process list for any other user on the
machine to read.

setup_db.py reports when no account exists and points here, so a fresh install
cannot silently end up with an unreachable dashboard.
"""
import argparse
import asyncio
import getpass
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select  # noqa: E402

from database.base import AsyncSessionLocal, get_engine  # noqa: E402
from database.models import User  # noqa: E402
from services.user_auth import (  # noqa: E402
    count_users,
    create_user,
    get_user,
    normalize_username,
    set_password,
)

# Deliberately modest. A long passphrase beats a short complex string, and
# complexity rules push people toward "Password1!" — which satisfies every
# rule and is in every wordlist.
MIN_PASSWORD_LENGTH = 12


def prompt_password(confirm: bool = True) -> str:
    """Read a password twice without echoing it."""
    while True:
        password = getpass.getpass("Password: ")
        if len(password) < MIN_PASSWORD_LENGTH:
            print(f"  Too short — use at least {MIN_PASSWORD_LENGTH} characters.")
            continue
        if confirm and password != getpass.getpass("Confirm password: "):
            print("  Passwords did not match. Try again.")
            continue
        return password


async def cmd_create(username: str | None) -> None:
    async with AsyncSessionLocal() as db:
        existing_count = await count_users(db)
        if existing_count == 0:
            print("No dashboard accounts exist yet — creating the first one.\n")

        username = normalize_username(username or input("Username: "))
        if not username:
            print("Username cannot be empty."); sys.exit(1)
        if await get_user(db, username):
            print(f"User {username!r} already exists. Use --reset-password to change it.")
            sys.exit(1)

        password = prompt_password()
        user = await create_user(db, username, password)
        print(f"\n  Created {user.username!r} (role: {user.role})")
        print("  Sign in at the dashboard with these credentials.")


async def cmd_list() -> None:
    async with AsyncSessionLocal() as db:
        users = (await db.execute(select(User).order_by(User.username))).scalars().all()
        if not users:
            print("No dashboard accounts exist. Create one:")
            print("    python scripts/create_user.py")
            return
        print(f"{'USERNAME':24} {'ROLE':8} {'ACTIVE':7} {'LOCKED':7} LAST LOGIN")
        for u in users:
            locked = "yes" if u.locked_until else "no"
            last = u.last_login_at.strftime("%Y-%m-%d %H:%M") if u.last_login_at else "never"
            print(f"{u.username:24} {u.role:8} {str(u.is_active):7} {locked:7} {last}")


async def cmd_reset_password(username: str) -> None:
    async with AsyncSessionLocal() as db:
        user = await get_user(db, username)
        if user is None:
            print(f"No such user: {username!r}"); sys.exit(1)
        password = prompt_password()
        # set_password also clears any lockout: someone resetting a password
        # is almost always doing it BECAUSE the account is locked, and leaving
        # the lock in place would make the reset appear not to work.
        await set_password(db, user, password)
        print(f"  Password updated for {user.username!r} (any lockout cleared)")


async def cmd_set_active(username: str, active: bool) -> None:
    async with AsyncSessionLocal() as db:
        user = await get_user(db, username)
        if user is None:
            print(f"No such user: {username!r}"); sys.exit(1)
        user.is_active = active
        await db.commit()
        state = "activated" if active else "deactivated"
        print(f"  {user.username!r} {state}")
        if not active:
            # Honest about the limitation rather than letting someone assume
            # the session died with the account.
            print("  NOTE: an existing session stays valid until it expires")
            print("  (DASHBOARD_SESSION_TTL_HOURS). To kill every live session")
            print("  immediately, rotate DASHBOARD_SESSION_SECRET and restart.")


async def cmd_unlock(username: str) -> None:
    async with AsyncSessionLocal() as db:
        user = await get_user(db, username)
        if user is None:
            print(f"No such user: {username!r}"); sys.exit(1)
        user.failed_login_count = 0
        user.locked_until = None
        await db.commit()
        print(f"  {user.username!r} unlocked")


async def main() -> None:
    parser = argparse.ArgumentParser(
        description="Manage dashboard accounts. Passwords are always prompted "
                    "for, never accepted as an argument — a flag would put the "
                    "password in your shell history and in the process list."
    )
    parser.add_argument("--username", help="username to create")
    parser.add_argument("--list", action="store_true", help="list accounts")
    parser.add_argument("--reset-password", metavar="USERNAME")
    parser.add_argument("--deactivate", metavar="USERNAME")
    parser.add_argument("--activate", metavar="USERNAME")
    parser.add_argument("--unlock", metavar="USERNAME")
    args = parser.parse_args()

    try:
        if args.list:
            await cmd_list()
        elif args.reset_password:
            await cmd_reset_password(args.reset_password)
        elif args.deactivate:
            await cmd_set_active(args.deactivate, False)
        elif args.activate:
            await cmd_set_active(args.activate, True)
        elif args.unlock:
            await cmd_unlock(args.unlock)
        else:
            await cmd_create(args.username)
    except KeyboardInterrupt:
        print("\nCancelled.")
    finally:
        await get_engine().dispose()


if __name__ == "__main__":
    asyncio.run(main())