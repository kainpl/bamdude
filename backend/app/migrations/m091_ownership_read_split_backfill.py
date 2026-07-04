"""Backfill the archives/queue/library ownership read-split onto existing groups.

Port of upstream Bambuddy security-hardening #2. Upstream inlines this backfill
into ``core/database.py::seed_default_groups`` (runs every startup); our fork has
no such function, so — matching the ``m059`` precedent — the backfill lives in a
numbered migration ``seed()``. Data-only: no schema change.

Background: the API archive/queue/library read routes now gate on the ownership
split (``archives:read_own`` / ``archives:read_all``) instead of the flat
``archives:read``. That flag alone (which a custom role could hold) previously
returned **every** user's rows — an IDOR. Fresh installs get the correct split
via ``DEFAULT_GROUPS``; this seed fixes existing DBs.

BamDude policy (diverges from upstream on the DEFAULT tier — we are a shared
print farm, so operators are expected to see each other's work):

* **Administrators** — backfill ``*:read`` + ``*:read_own`` + ``*:read_all`` +
  ``orca_cloud:auth`` (``is_admin`` bypasses permission checks anyway, but we
  keep the stored set consistent with a fresh install).
* **Operators / Viewers** (system groups) — backfill ``*:read_all`` so reads
  stay farm-wide, keeping the legacy ``*:read`` flag as the frontend's
  download/preview gate. Operators also get ``orca_cloud:auth`` (needed for the
  Slice modal's Orca Cloud preset picker); Viewers do not.
* **Custom groups** — if a group carries the legacy ``*:read`` flag, add the
  ``*:read_own`` variant (fail-closed default per CWE-636: a role must be
  explicitly re-granted ``*:read_all`` to see cross-user data). The legacy flag
  is kept so the frontend gate doesn't degrade to disabled buttons — the backend
  no longer honours it, so keeping it is harmless.

Idempotent — set semantics on the JSON permissions list.
"""

version = 91
name = "ownership_read_split_backfill"

# (legacy flat read, own variant, all variant) per resource.
_READ_SPLIT = (
    ("archives:read", "archives:read_own", "archives:read_all"),
    ("queue:read", "queue:read_own", "queue:read_all"),
    ("library:read", "library:read_own", "library:read_all"),
)


async def upgrade(conn):
    # Data-only migration — the backfill happens in seed(). No DDL.
    return


async def seed(session_factory):
    import json

    from sqlalchemy import select, update

    from backend.app.models.group import Group

    # Column-explicit read + Core update — see feedback_migration_seed_columns.
    async with session_factory() as db:
        result = await db.execute(select(Group.id, Group.name, Group.is_system, Group.permissions))
        for row in result.all():
            perms = row.permissions or []
            if isinstance(perms, str):
                try:
                    perms = json.loads(perms)
                except (json.JSONDecodeError, TypeError):
                    perms = []
            perms = list(perms)
            existing = set(perms)

            is_admin_group = row.is_system and row.name == "Administrators"
            is_farm_read_group = row.is_system and row.name in ("Operators", "Viewers")

            wanted: list[str] = []
            for legacy, own, all_ in _READ_SPLIT:
                if is_admin_group:
                    # Full read set for parity with a fresh Administrators seed.
                    wanted += [legacy, own, all_]
                elif is_farm_read_group:
                    # Farm-wide reads; legacy flag stays for the frontend gate.
                    wanted.append(all_)
                elif legacy in existing:
                    # Custom role that used to see everything → fail-closed to
                    # own; keep legacy for the frontend gate.
                    wanted.append(own)

            # orca_cloud:auth — Administrators + Operators get it (fresh-install
            # parity); Viewers stay read-only.
            if is_admin_group or (row.is_system and row.name == "Operators"):
                wanted.append("orca_cloud:auth")

            to_add = [p for p in wanted if p not in existing]
            if to_add:
                perms.extend(to_add)
                await db.execute(update(Group).where(Group.id == row.id).values(permissions=perms))
        await db.commit()
