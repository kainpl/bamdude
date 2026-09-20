"""BamDude administrative CLI.

Invoked via ``python -m backend.app.cli <command>``. Commands are intentionally
small and destructive operations are explicit - this is a rescue / operator
utility, not a daily-driver surface. Commands:

* ``reset_admin`` - wipe the "setup_completed" flag so the next server start
  sends the user back through ``/setup`` to create a fresh admin. Use this when
  all admin users have been lost (forgotten credentials, mistaken deletions,
  etc.) and you still have file-system / container access.

* ``list_users`` - print the local accounts, so ``reset_password`` can be aimed
  without guessing at a username.

* ``reset_password`` - set a local account's password from the server console.
  This is the recovery path when nobody can receive the reset e-mail: an
  install with no SMTP has no self-service recovery at all (the login page
  hides the link, because offering it would be a promise nothing can keep),
  and the person who owns the machine is exactly the person who can be trusted
  with a shell on it. The new password goes through the same complexity rules
  the API enforces, so the console cannot set one the account could never set
  again through the UI.

* ``init_embedded_pg`` - initialise the bundled PostgreSQL cluster (initdb +
  our conf) WITHOUT starting it. Used by the Windows installer, which then
  registers the server as its own service; it reuses the application's own
  bootstrap so the flags and the configuration file have a single source of
  truth. Requires ``DATABASE_URL=embedded`` in the environment.

Existing non-admin users and all other data are left untouched.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import datetime, timezone

from sqlalchemy import delete, select

from backend.app.core.auth import has_any_admin
from backend.app.core.database import async_session, init_db
from backend.app.models.settings import Settings

logger = logging.getLogger("bamdude.cli")


async def _reset_admin() -> int:
    """Clear the setup-completed flag so the server boots into SetupPage."""
    await init_db()  # ensures Settings table exists and migrations are applied
    async with async_session() as db:
        if await has_any_admin(db):
            print(
                "Admin user(s) still exist. Refusing to reset - delete them first "
                "via the admin panel or directly in the database, then re-run "
                "this command.",
                file=sys.stderr,
            )
            return 2

        # Remove the advisory flags so the frontend + middleware route the
        # next request through /setup.
        await db.execute(delete(Settings).where(Settings.key.in_(["setup_completed", "auth_enabled"])))
        await db.commit()

        # Sanity check: re-query to confirm removal.
        result = await db.execute(select(Settings).where(Settings.key == "setup_completed"))
        assert result.scalar_one_or_none() is None, "failed to clear setup_completed"

    print(
        "Setup has been reset. Restart the server (or reload the browser) and "
        "you will be routed to /setup to create a new admin user.",
    )
    return 0


async def _list_users() -> int:
    """Print local accounts with the facts that decide whether a reset helps."""
    from backend.app.models.user import User

    await init_db()
    async with async_session() as db:
        rows = (await db.execute(select(User).order_by(User.username))).scalars().all()
        if not rows:
            print("No users exist. Run 'reset_admin' to re-enter the setup flow.")
            return 0
        print(f"{'username':<24} {'role':<8} {'source':<8} {'active':<7} email")
        for user in rows:
            print(
                f"{user.username:<24} {user.role or '':<8} "
                f"{getattr(user, 'auth_source', 'local') or 'local':<8} "
                f"{'yes' if user.is_active else 'NO':<7} {user.email or '-'}"
            )
    return 0


def _prompt_for_password() -> str | None:
    """Ask twice, echo neither. Returns None when the two do not agree."""
    import getpass

    first = getpass.getpass("New password: ")
    second = getpass.getpass("Repeat it: ")
    if first != second:
        print("The two entries do not match.", file=sys.stderr)
        return None
    return first


async def _reset_password(username: str, generate: bool, clear_2fa: bool) -> int:
    """Set a local account's password from the console."""
    from backend.app.core.auth import get_password_hash, get_user_by_username, revoke_all_refresh_tokens_for_user
    from backend.app.schemas.auth import _validate_password_complexity
    from backend.app.services.email_service import generate_secure_password

    await init_db()
    async with async_session() as db:
        # Case-insensitive, like every other username lookup in the codebase —
        # an operator typing "Admin" at 2am should not be told no such user.
        # Through ``get_user_by_username`` rather than its own query, so the
        # console shares the collision rule for case-variant accounts that
        # Unicode folding can now match together (spec §3.5): a recovery tool
        # must not be the one path that raises MultipleResultsFound.
        user = await get_user_by_username(db, username)
        if user is None:
            print(f"No user named {username!r}. Run 'list_users' to see who exists.", file=sys.stderr)
            return 2
        if getattr(user, "auth_source", "local") == "ldap":
            print(
                f"{user.username} authenticates against LDAP — its password lives on the "
                "directory server, and setting one here would change nothing.",
                file=sys.stderr,
            )
            return 2

        if generate:
            password = generate_secure_password()
        else:
            entered = _prompt_for_password()
            if entered is None:
                return 2
            password = entered

        try:
            _validate_password_complexity(password)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2

        user.password_hash = get_password_hash(password)
        user.password_changed_at = datetime.now(timezone.utc)
        # Sign out everything that was signed in on the old password: a reset
        # done because somebody else had the account is pointless if their
        # session keeps working.
        await revoke_all_refresh_tokens_for_user(db, user.username)

        if clear_2fa:
            from backend.app.models.user_otp_code import UserOTPCode
            from backend.app.models.user_totp import UserTOTP

            await db.execute(delete(UserTOTP).where(UserTOTP.user_id == user.id))
            await db.execute(delete(UserOTPCode).where(UserOTPCode.user_id == user.id))
            await db.execute(delete(Settings).where(Settings.key == f"user_{user.id}_email_2fa_enabled"))

        if not user.is_active:
            print(f"Note: {user.username} is deactivated and still cannot sign in. Re-enable it in the admin panel.")

        resolved = user.username
        await db.commit()

    if generate:
        # Printed once and never stored anywhere else — the operator has this
        # console and nothing else will show it again.
        print(f"New password for {resolved}: {password}")
    else:
        print(f"Password updated for {resolved}.")
    if clear_2fa:
        print("Two-factor authentication has been removed from this account.")
    print("Any sessions signed in on the old password have been signed out.")
    return 0


async def _init_embedded_pg() -> int:
    """initdb + write our conf for the bundled server, but do not start it.

    The Windows installer runs this, then registers the cluster as its own
    service. Reuses services/embedded_postgres so initdb flags and the conf are
    written by exactly the code the child-process path uses.
    """
    from backend.app.core.config import settings
    from backend.app.services import embedded_postgres as ep

    if not settings.embedded_postgres:
        print("DATABASE_URL is not 'embedded' — nothing to initialise.", file=sys.stderr)
        return 2

    ep._refuse_other_major()
    pgdata = settings.embedded_pg_data_dir
    if pgdata is not None and (pgdata / "PG_VERSION").exists():
        print(f"Embedded PostgreSQL cluster already present at {pgdata}; leaving it as is.")
    else:
        await ep._initdb()
    ep._write_conf()
    print(f"Embedded PostgreSQL initialised at {pgdata} (port {settings.embedded_pg_port}).")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m backend.app.cli", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("reset_admin", help="Clear setup flag so the next boot re-enters the setup flow.")
    sub.add_parser("list_users", help="List local accounts (username, role, source, active, email).")
    reset_pw = sub.add_parser(
        "reset_password",
        help="Set a local account's password from the server console (no e-mail needed).",
    )
    reset_pw.add_argument("--username", required=True, help="Account to reset.")
    reset_pw.add_argument(
        "--generate",
        action="store_true",
        help="Generate a strong password and print it once, instead of prompting for one.",
    )
    reset_pw.add_argument(
        "--clear-2fa",
        action="store_true",
        help="Also remove TOTP, e-mail OTP and backup codes — for when the second factor is lost too.",
    )
    sub.add_parser(
        "init_embedded_pg",
        help="initdb + conf for the bundled PostgreSQL without starting it (Windows installer).",
    )

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    if args.command == "reset_admin":
        return asyncio.run(_reset_admin())
    if args.command == "list_users":
        return asyncio.run(_list_users())
    if args.command == "reset_password":
        return asyncio.run(_reset_password(args.username, args.generate, args.clear_2fa))
    if args.command == "init_embedded_pg":
        return asyncio.run(_init_embedded_pg())
    parser.error(f"Unknown command: {args.command}")
    return 1  # unreachable


if __name__ == "__main__":
    raise SystemExit(main())
