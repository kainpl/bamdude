"""Add ``can_send_notifications`` to ``api_keys``: a key may speak as an external app.

Another program of the farm -- Home Assistant, a script -- tells the farm's people something through
BamDude's channels as the catalog event ``external_app_message`` (``POST /notifications/external-message``,
spec external-app-messages-spec, owner 2026-11-21). Like ``can_update_energy_cost`` (m073) it is a narrow,
deliberate opt-in on the key's row, off by default: no key that exists at the upgrade starts sending
messages on its own.
"""

from backend.app.migrations.helpers import add_column

version = 252
name = "api_key_send_notifications"


async def upgrade(conn):
    await add_column(conn, "api_keys", "can_send_notifications BOOLEAN DEFAULT 0 NOT NULL")
