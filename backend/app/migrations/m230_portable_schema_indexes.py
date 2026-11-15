"""Restore D4's frozen portable index set on existing installations.

Known duplicate keys retain rows and a durable, nonblocking residual. Unknown
same-name objects are never replaced. The settings marker and the migration
marker are committed by the caller; replay does not use live model metadata.
"""

from backend.app.core.schema_index_repair import native_upgrade

version = 230
name = "portable_schema_indexes"


async def upgrade(conn):
    await native_upgrade(conn)


async def replay(conn):
    await native_upgrade(conn)
