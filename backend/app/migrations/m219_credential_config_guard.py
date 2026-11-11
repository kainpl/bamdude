"""Reserve the installation-wide destination/credential transaction anchor.

Locking just an existing password row cannot serialize first-time/disjoint
Settings edits. The anchor precedes every adapter row on both databases. This
additive data migration changes no credentials and is safe to run repeatedly;
bootstrap/full restore also ensure it before opening admission. No request may
silently recreate a deleted anchor. m216/m217 belong to the Orca connector;
m218 is the accepted account-mail migration, so this unit starts at m219.
"""

from sqlalchemy import text

version = 219
name = "credential_config_guard"


async def upgrade(conn):
    await conn.execute(
        text("INSERT INTO settings (key, value) VALUES ('_credential_config_guard', '1') ON CONFLICT (key) DO NOTHING")
    )
