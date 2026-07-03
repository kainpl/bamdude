"""Add ``can_manage_library`` + ``can_manage_inventory`` scope flags to ``api_keys``.

Companion to the API-key permission allowlist (upstream Bambuddy
GHSA-r2qv-8222-hqg3, v0.2.4.5). Before the allowlist, ``_check_apikey_permissions``
let any valid key satisfy every non-denylisted route, so a "queue-only" key
could upload/rename/delete library files and write inventory. The allowlist
maps ``LIBRARY_UPLOAD`` / ``LIBRARY_UPDATE_OWN`` / ``LIBRARY_DELETE_OWN`` /
``LIBRARY_NOTES_WRITE`` / ``MAKERWORLD_IMPORT`` to ``can_manage_library`` and
the inventory-write perms to ``can_manage_inventory`` — two new scope flags.

Backfill mirrors ``can_queue`` per-row: an operator's existing "queue-only"
key was implicitly relying on the upload+queue+inventory-write workflow the
queue scope already let through, so mirroring preserves intent; a hardened
read-only key (``can_queue=False``) does NOT silently gain writes on upgrade.

The backfill is gated on the column having just been added (``add_column``
returning True) so ``DEBUG=true`` re-running the latest migration on startup
can never clobber operator-edited values on a subsequent restart.
"""

from sqlalchemy import text

from backend.app.core.db_dialect import is_postgres
from backend.app.migrations.helpers import add_column

version = 86
name = "apikey_library_inventory_scopes"


async def upgrade(conn):
    default = "TRUE" if is_postgres() else "1"
    added_lib = await add_column(conn, "api_keys", f"can_manage_library BOOLEAN NOT NULL DEFAULT {default}")
    added_inv = await add_column(conn, "api_keys", f"can_manage_inventory BOOLEAN NOT NULL DEFAULT {default}")

    if added_lib:
        await conn.execute(text("UPDATE api_keys SET can_manage_library = can_queue"))
    if added_inv:
        await conn.execute(text("UPDATE api_keys SET can_manage_inventory = can_queue"))
