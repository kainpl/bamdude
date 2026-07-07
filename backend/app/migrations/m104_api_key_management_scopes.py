"""Add ``can_manage_maintenance`` / ``can_manage_archives`` / ``can_manage_projects`` to ``api_keys``.

Companion to the API-key permission allowlist (see ``core/auth.py``). Upstream
Bambuddy carves three write surfaces out of the admin denylist so HA-style
automations can drive them via API key without broader printer control:

  - ``can_manage_maintenance`` (upstream #1832 follow-up / commit ``006c3113``) —
    per-printer maintenance log/reset, interval edits, and the type-catalog CRUD.
  - ``can_manage_archives`` (upstream #1888 / commit ``6358e954``) —
    ARCHIVES_CREATE / _UPDATE_* / _DELETE_* (NOT purge). OWN + ALL fold into the
    one scope (keys have no per-row ownership identity).
  - ``can_manage_projects`` (upstream #1893 / commit ``168d9d8f``) —
    PROJECTS_CREATE / _UPDATE / _DELETE + membership (add-archives, gated on
    PROJECTS_UPDATE).

Backfill differs from the m086 ``= can_queue`` mirror: all three permission sets
were EXPLICITLY on ``_APIKEY_DENIED_PERMISSIONS`` before this change (denied for
every API key regardless of scope), so no existing integration relied on them.
Existing rows therefore backfill to FALSE — the upgrade never silently widens
scope. The column DB default is TRUE (matches the model ``default=True``) so
UI-created keys opt in on by default going forward; operators toggle per key via
Settings → API Keys.

Each backfill is gated on the column having just been added (``add_column``
returning True) so ``DEBUG=true`` re-running the latest migration on startup can
never clobber operator-edited values on a subsequent restart. Upstream shipped
these as inline migrations in ``core/database.py``; BamDude uses numbered
migrations.
"""

from sqlalchemy import text

from backend.app.core.db_dialect import is_postgres
from backend.app.migrations.helpers import add_column

version = 104
name = "api_key_management_scopes"


async def upgrade(conn):
    true_lit = "TRUE" if is_postgres() else "1"
    false_lit = "FALSE" if is_postgres() else "0"

    added_maint = await add_column(conn, "api_keys", f"can_manage_maintenance BOOLEAN NOT NULL DEFAULT {true_lit}")
    added_arch = await add_column(conn, "api_keys", f"can_manage_archives BOOLEAN NOT NULL DEFAULT {true_lit}")
    added_proj = await add_column(conn, "api_keys", f"can_manage_projects BOOLEAN NOT NULL DEFAULT {true_lit}")

    # Existing rows → FALSE (these perms were denied for every key pre-migration;
    # no silent scope widening). Gated on the add so a DEBUG re-run can't clobber
    # operator edits.
    if added_maint:
        await conn.execute(text(f"UPDATE api_keys SET can_manage_maintenance = {false_lit}"))
    if added_arch:
        await conn.execute(text(f"UPDATE api_keys SET can_manage_archives = {false_lit}"))
    if added_proj:
        await conn.execute(text(f"UPDATE api_keys SET can_manage_projects = {false_lit}"))
