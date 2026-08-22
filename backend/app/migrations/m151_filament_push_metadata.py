"""user_filament_presets grows the Bambu push metadata (spec B §5):
``pushed_cloud_id`` — the PFUS setting_id the cloud returned when this
authored/local preset was pushed (NULL = never pushed / cloud copy vanished);
``pushed_at`` — when; ``push_dirty`` — content edited locally after the push
(an explicit Re-push clears it; there is deliberately NO automatic re-push —
BamDude must not fight edits made in BS).
"""

from backend.app.migrations.helpers import add_column

version = 151
name = "filament_push_metadata"


async def upgrade(conn):
    await add_column(conn, "user_filament_presets", "pushed_cloud_id VARCHAR(64)")
    await add_column(conn, "user_filament_presets", "pushed_at DATETIME")
    await add_column(conn, "user_filament_presets", "push_dirty BOOLEAN DEFAULT 0")
