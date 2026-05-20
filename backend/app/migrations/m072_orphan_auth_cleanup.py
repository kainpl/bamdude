"""Heal orphan auth-related rows left behind by user-delete on SQLite.

``user_oidc_links``, ``user_totp``, ``user_otp_codes`` and
``long_lived_tokens`` all declare ``ON DELETE CASCADE`` on ``user_id`` in
their models. PostgreSQL enforces the cascade, but SQLite ships with
``PRAGMA foreign_keys=OFF`` (the project's existing pattern; the same
issue surfaces in PR #1182 for ``api_keys``). On SQLite, rows pointing
to a deleted user persisted indefinitely:

- ``UserOIDCLink``: the OIDC callback finds the orphan link, fails to
  resolve the (now missing) user, and falls through to
  ``account_inactive`` instead of triggering auto-create — blocking SSO
  re-login for the user's email forever.
- ``UserTOTP``: MFA secrets persist after the owning user.
- ``UserOTPCode``: pending email OTP codes linger.
- ``LongLivedToken``: per-user camera-stream tokens whose
  ``secret_hash`` is still valid — ``verify()`` would happily match
  them by ``lookup_prefix`` even though the user is gone.

This migration is **idempotent** on SQLite (second run finds nothing)
and a **no-op on PostgreSQL** (FK cascade already fired on the deletes
that originally happened — there are no orphans to find).

Upstream Bambuddy #1285 / commit 4d8dbc83.
"""

from sqlalchemy import text

version = 72
name = "orphan_auth_cleanup"


async def upgrade(conn):
    # Each DELETE is wrapped in a try/except guard via SQL-only IF EXISTS-
    # style: if a table doesn't yet exist (fresh install before these
    # tables were created — defensive only), we tolerate the OperationalError.
    # In practice all four tables are created by earlier migrations.
    for stmt in (
        "DELETE FROM user_oidc_links WHERE user_id NOT IN (SELECT id FROM users)",
        "DELETE FROM user_totp WHERE user_id NOT IN (SELECT id FROM users)",
        "DELETE FROM user_otp_codes WHERE user_id NOT IN (SELECT id FROM users)",
        "DELETE FROM long_lived_tokens WHERE user_id NOT IN (SELECT id FROM users)",
    ):
        try:
            await conn.execute(text(stmt))
        except Exception:
            # Table absent on a brand-new fresh install before its own
            # CREATE TABLE migration runs — tolerate and move on.
            pass
