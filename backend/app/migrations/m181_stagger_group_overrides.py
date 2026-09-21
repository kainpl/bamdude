"""Make stagger group limits true overrides without raising existing heating caps.

The old resolver used min(default, tag, location). Clamp every saved valid
override to that default once, including inactive groups, before switching to
min(tag-or-default, location-or-default). Missing overrides stay inherited.

The settings marker is atomic with the rewrite, unlike the runner's separate
_migrations record. DEBUG deletes that record and reruns the latest migration;
it must NOT clamp overrides the operator explicitly raised after upgrading.
Fresh installations also get the marker. Both SQLite and PostgreSQL use the
same parameterized DML. No imports from evolving runtime policy code.

Vault: 60-specs/stagger-group-override-spec-plan.
"""

import json

from sqlalchemy import text

version = 181
name = "stagger_group_overrides"

_MARKER = "migration_stagger_group_overrides_v1"
_LIMIT_KEYS = ("stagger_tag_limits", "stagger_location_limits")


async def upgrade(conn):
    keys = (_MARKER, "stagger_concurrent", *_LIMIT_KEYS)
    rows = await conn.execute(
        text("SELECT key, value FROM settings WHERE key IN (:marker, :base, :tags, :locations)"),
        dict(zip(("marker", "base", "tags", "locations"), keys, strict=True)),
    )
    saved = dict(rows.all())
    if _MARKER in saved:
        return

    # Match the old enabled scheduler (including absent/empty and nonpositive).
    # An invalid integer already prevented dispatch; do not guess a safe cap.
    base = max(1, int(saved.get("stagger_concurrent") or "2"))
    for key in _LIMIT_KEYS:
        raw = saved.get(key)
        if not raw:
            continue
        try:
            limits = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(limits, dict):
            continue
        changed = False
        for ident, cap in limits.items():
            try:
                int(ident)
            except (TypeError, ValueError):
                continue
            if isinstance(cap, int) and not isinstance(cap, bool) and cap > base:
                limits[ident] = base
                changed = True
        if changed:
            await conn.execute(
                text("UPDATE settings SET value = :value WHERE key = :key"),
                {"key": key, "value": json.dumps(limits)},
            )
    await conn.execute(text("INSERT INTO settings (key, value) VALUES (:key, :value)"), {"key": _MARKER, "value": "1"})
