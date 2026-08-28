"""Per-chat duration floor for progress-milestone notifications (#28).

Telegram events are configured strictly per chat (m045) — a single global
"only for prints longer than N minutes" cannot express an admin who wants
60 while an operator's chat on the same bot wants 10. NULL inherits the
global setting, 0 always sends, N mutes prints estimated shorter than N
minutes.
"""

from backend.app.migrations.helpers import add_column

version = 157
name = "telegram_chat_progress_floor"


async def upgrade(conn):
    await add_column(conn, "telegram_chats", "progress_min_duration_minutes INTEGER")
