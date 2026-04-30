"""Add per-spool extra_colors / effect_type / category / low_stock_threshold_pct.

Why
---
Bundles two upstream features that touch the same table:

* **B.1 — Multi-colour gradients & visual effects (#1154)**
  Adds ``extra_colors`` (comma-separated hex stops, up to 8) and
  ``effect_type`` (sparkle / wood / marble / glow / matte / silk / galaxy /
  rainbow / metal / translucent — pure rendering hint). Purely additive;
  existing rows stay solid (NULL columns).

* **B.8 — Per-spool category + low-stock threshold override (#729 minimal)**
  Adds ``category`` (free-text, 50-char max — UI autocompletes from existing
  categories) and ``low_stock_threshold_pct`` (1..99 override of the global
  ``low_stock_threshold`` setting; NULL keeps the global behaviour).

Both fields are nullable + idempotent via ``add_column`` so reruns and old
upgrade paths are safe. No data backfill — every existing row keeps the
solid-colour, no-effect, no-category, global-threshold default.
"""

from backend.app.migrations.helpers import add_column

version = 26
name = "spool_extensions"


async def upgrade(conn):
    # B.1 — gradient stops + effect overlay
    await add_column(conn, "spool", "extra_colors VARCHAR(255)")
    await add_column(conn, "spool", "effect_type VARCHAR(20)")
    # B.8 — category + per-spool low-stock threshold override
    await add_column(conn, "spool", "category VARCHAR(50)")
    await add_column(conn, "spool", "low_stock_threshold_pct INTEGER")


async def seed(session_factory):  # pragma: no cover — no-op
    async with session_factory() as db:
        _ = db  # noqa: ARG001
