"""Per-provider duration floor for progress-milestone notifications (#28).

Telegram chats carry their own value (m157); every other provider carries
its own here. NULL or 0 = always send — there is no global fallback: one
shared number boxed a farm in, and a phone push and an email digest
legitimately want different floors.
"""

from backend.app.migrations.helpers import add_column

version = 158
name = "provider_progress_floor"


async def upgrade(conn):
    await add_column(conn, "notification_providers", "progress_min_duration_minutes INTEGER")
