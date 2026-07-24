"""Add ``users.cloud_token_invalid_at`` — the durable "Bambu rejected this token" flag.

Bambu's access token is opaque (no readable expiry) and BamDude does not persist
the refresh token, so a dead credential is indistinguishable from a live one.
``BambuCloudService`` used to paper over that by stamping ``token_expiry = now +
30 days`` every time a stored token was *loaded* — re-derived from *now* on every
request — which made ``is_authenticated`` incapable of ever returning False:
``/cloud/status`` answered "connected" indefinitely while every cloud call 401'd
(upstream #2562).

This column is the record. It is stamped the first time Bambu answers its
genuine expiry 401 (``{"code":4,"error":"Please login."}`` — a plain 401 is
transient and deliberately does NOT set it), and cleared on any fresh login or
logout. Because it lives on the user row, MakerWorld, cloud profiles, slicer
presets and firmware checks all agree at once instead of each rediscovering the
dead token for itself.

NULL means "not known to be dead" — the correct state for every existing row, so
the column is nullable with no default and needs no backfill. Fresh installs get
it from the model's ``create_all``; this adds it to existing DBs. Idempotent
(``add_column`` no-ops when the column exists, incl. the DEBUG-startup re-run).
"""

from backend.app.migrations.helpers import add_column

version = 109
name = "user_cloud_token_invalid_at"


async def upgrade(conn):
    await add_column(conn, "users", "cloud_token_invalid_at DATETIME")
