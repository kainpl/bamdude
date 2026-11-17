"""A notification channel can opt out of camera frames (upstream 7f64e2ba8).

``notification_providers.attach_photo`` -- on for every existing channel, which
is what they did before the column: a frame went to every channel able to take
one. Turning it off sends that channel text only. It is a transport option of
the channel, like its quiet hours; it is NOT an event toggle, so the rule that
a Telegram chat decides its own events is untouched.
"""

from backend.app.migrations.helpers import add_column

version = 239
name = "notification_attach_photo"


async def upgrade(conn):
    await add_column(conn, "notification_providers", "attach_photo BOOLEAN DEFAULT 1")
