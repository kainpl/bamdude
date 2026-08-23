"""users grows ``cloud_refresh_token`` (spec A §3 hardening): Bambu's login
response has always carried a refreshToken we discarded, forcing a manual
re-login (email code / captcha) every ~3 months when the access token died.
Stored alongside the access token, it lets the server renew the pair silently
on the genuine expiry 401 — with the old re-login path as the fallback, since
the refresh endpoint is community-documented, not official. The auth-disabled
mode stores its copy in the Settings key-value table (no DDL needed there).
"""

from backend.app.migrations.helpers import add_column

version = 152
name = "cloud_refresh_token"


async def upgrade(conn):
    await add_column(conn, "users", "cloud_refresh_token VARCHAR(500)")
