"""Orca Cloud own-identity + write-leg bookkeeping.

``user_filament_presets`` grows the Orca half of the per-ecosystem push
metadata (a row can be pushed to BOTH clouds, so the m151 Bambu trio cannot be
shared): ``orca_pushed_profile_id`` — the client-minted profile uuid;
``orca_pushed_at``; ``orca_push_dirty`` — edited locally after the push
(explicit Re-push only, same doctrine as Bambu); ``orca_pushed_updated_time``
— the SERVER ``updated_time`` from our last push/force-push response. That
last one is the optimistic-lock anchor: "the cloud changed since our push" is
only detectable against the value we last wrote, never against a fresh pull
(a freshly pulled timestamp has, by construction, just been seen).

``users.orca_cloud_scope`` stores the granted scope from the token response —
the UI gates the push controls on it instead of guessing (settings-table
deployments use the ``orca_cloud_scope`` settings key instead).
"""

from backend.app.migrations.helpers import add_column

version = 154
name = "orca_push_bookkeeping"


async def upgrade(conn):
    await add_column(conn, "user_filament_presets", "orca_pushed_profile_id VARCHAR(64)")
    await add_column(conn, "user_filament_presets", "orca_pushed_at DATETIME")
    await add_column(conn, "user_filament_presets", "orca_push_dirty BOOLEAN DEFAULT 0")
    await add_column(conn, "user_filament_presets", "orca_pushed_updated_time INTEGER")
    await add_column(conn, "users", "orca_cloud_scope VARCHAR(128)")
