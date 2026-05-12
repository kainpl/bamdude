"""Normalise stale permission keys in ``groups.permissions`` JSON.

Why
---
``groups.permissions`` is a JSON list of strings like ``"inventory:read"``.
``Permission`` enum values evolved across BamDude releases — six historical
keys were renamed:

* ``filaments:{read,create,update,delete}`` → ``inventory:{read,create,update,delete}``
* ``github:{backup,restore}`` → ``git:{backup,restore}``

When the codebase added the new keys it never rewrote the JSON list on
existing ``groups`` rows. After upgrade a custom group whose ``permissions``
list still carried the old keys looked broken from the UI:

* Group editor showed ``101 selected / 95`` (the 6 stale keys padded the
  count past the current ``ALL_PERMISSIONS`` total).
* Save attempts returned ``400 Invalid permissions: filaments:read, …``
  from ``routes/groups.py`` because the create/update validators reject
  every key not present in the live ``ALL_PERMISSIONS``.

What this does
--------------
Walk every ``groups.permissions`` list once at upgrade time and:

1. Map each known-renamed key to its replacement.
2. Drop any other key the live ``ALL_PERMISSIONS`` doesn't recognise
   (defence against even older legacy keys we no longer track — better
   to silently drop than to leave a group permanently un-saveable).
3. De-duplicate (a group might already carry *both* the old and the new
   key, e.g. ``filaments:read`` *and* ``inventory:read`` — collapse to one).
4. Preserve order of first occurrence so the UI doesn't reshuffle the
   apparent toggle order on operators with muscle memory.

Idempotent. Re-running on an already-clean DB is a no-op (every key is
already in ``ALL_PERMISSIONS``, so the rewrite produces the same list).

System groups (``Administrators`` / ``Operators`` / ``Viewers``) are
re-seeded on every startup from ``permissions.py::DEFAULT_GROUPS`` —
this migration touches them too for consistency, but their next startup
re-seed will overwrite them with the canonical list anyway.
"""

from sqlalchemy import select, update

from backend.app.models.group import Group

version = 46
name = "normalize_group_permissions"


# Map historical → current permission keys. Source of truth for what was
# renamed across releases; if a future rename happens, add a row here so
# the upgrade path heals existing groups in place.
_PERMISSION_RENAMES: dict[str, str] = {
    "filaments:read": "inventory:read",
    "filaments:create": "inventory:create",
    "filaments:update": "inventory:update",
    "filaments:delete": "inventory:delete",
    "github:backup": "git:backup",
    "github:restore": "git:restore",
}


def _normalize(perms: list[str], valid: set[str]) -> list[str]:
    """Apply renames + drop unknown keys + de-dupe, preserving first-seen
    order. Pure function for unit testing."""
    seen: set[str] = set()
    out: list[str] = []
    for key in perms:
        if not isinstance(key, str):
            continue  # guard against legacy NULLs / non-string entries
        canonical = _PERMISSION_RENAMES.get(key, key)
        if canonical not in valid:
            continue
        if canonical in seen:
            continue
        seen.add(canonical)
        out.append(canonical)
    return out


async def seed(session_factory):
    # Lazy-import to avoid load-order cycles: permissions.py imports nothing
    # from migrations, but importing it at module top would force the enum
    # to materialise before m046 is registered.
    from backend.app.core.permissions import ALL_PERMISSIONS

    valid = set(ALL_PERMISSIONS)

    # Column-explicit read + Core update — the model evolves, and an
    # entity-wide ``select(Group)`` would emit future columns in the SQL
    # and crash an upgrade chain where this migration runs before they
    # exist. See feedback_migration_seed_columns.
    async with session_factory() as db:
        result = await db.execute(select(Group.id, Group.permissions))
        rows = result.all()
        dirty = 0
        for row in rows:
            old = list(row.permissions or [])
            new = _normalize(old, valid)
            if new != old:
                await db.execute(update(Group).where(Group.id == row.id).values(permissions=new))
                dirty += 1
        if dirty:
            await db.commit()
