"""A group of people can be limited to printers and locations (N62, upstream Bambuddy #1727).

``groups.restrict_printers`` limits a group's members to the printers it reaches: those
in ``group_printers`` and every printer standing in one of its ``group_locations`` or in a
location below it (``core/printer_scope``). A group without the flag limits nothing.

No backfill: every existing group is created unrestricted, so an install behaves as before
until an administrator limits a group. Foreign keys CASCADE on PostgreSQL; on SQLite they
are off, and the group, printer and location deletes remove the rows themselves.
"""

from backend.app.migrations.helpers import add_column, table_exists

version = 269
name = "printer_scoped_groups"


async def upgrade(conn):
    await add_column(conn, "groups", "restrict_printers BOOLEAN NOT NULL DEFAULT FALSE")
    if not await table_exists(conn, "group_printers"):
        await conn.exec_driver_sql(
            """
            CREATE TABLE group_printers (
                group_id INTEGER NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
                printer_id INTEGER NOT NULL REFERENCES printers(id) ON DELETE CASCADE,
                PRIMARY KEY (group_id, printer_id)
            )
            """
        )
    if not await table_exists(conn, "group_locations"):
        await conn.exec_driver_sql(
            """
            CREATE TABLE group_locations (
                group_id INTEGER NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
                location_id INTEGER NOT NULL REFERENCES printer_locations(id) ON DELETE CASCADE,
                PRIMARY KEY (group_id, location_id)
            )
            """
        )
