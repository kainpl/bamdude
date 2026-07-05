"""Add color_name to filament SKU settings + shopping list (colour-aware forecasting).

Upstream Bambuddy #1814 (6c5b40dd). Upstream inlines this DDL in
core/database.py::run_migrations; we ship it discretely per our mNNN convention.
Adds nullable color_name to filament_sku_settings + filament_shopping_list and
widens the SKU uniqueness key (material, subtype, brand) -> (..., color_name) so
the forecast panel persists per-colour reorder settings. Existing rows keep
color_name = NULL; the frontend falls back to the NULL-colour row so pre-upgrade
overrides survive.

Idempotent + DEBUG re-run safe:
  * add_column() no-ops when the column already exists.
  * SQLite rebuild is gated on inspecting the UNIQUE index columns — it only
    fires when a UNIQUE index over ``material`` still lacks ``color_name``
    (i.e. a pre-upgrade table). Fresh installs (create_all already ships the
    wide key) and re-runs are skipped.
  * PostgreSQL constraint swap is gated on a pg_constraint lookup so repeated
    startups don't take an ACCESS EXCLUSIVE lock churning the constraint.
"""

from sqlalchemy import text

from backend.app.core.db_dialect import is_postgres
from backend.app.migrations.helpers import add_column

version = 94
name = "forecast_color_grouping"


async def upgrade(conn):
    # 1) Add the nullable column to both tables (idempotent on both dialects).
    await add_column(conn, "filament_sku_settings", "color_name VARCHAR(100)")
    await add_column(conn, "filament_shopping_list", "color_name VARCHAR(100)")

    if is_postgres():
        # Widen UNIQUE (material, subtype, brand) → (..., color_name). The
        # constraint was declared with name="uq_filament_sku" in the model, so
        # drop/re-add by that name. Gated on a pg_constraint lookup so the swap
        # only runs when color_name is missing from the key — otherwise every
        # startup (DEBUG re-run) would take an ACCESS EXCLUSIVE lock.
        uq_check = await conn.execute(
            text(
                "SELECT 1 FROM pg_constraint c "
                "JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = ANY(c.conkey) "
                "WHERE c.conname = 'uq_filament_sku' AND a.attname = 'color_name' LIMIT 1"
            )
        )
        if uq_check.scalar_one_or_none() is None:
            await conn.execute(text("ALTER TABLE filament_sku_settings DROP CONSTRAINT IF EXISTS uq_filament_sku"))
            await conn.execute(
                text(
                    "ALTER TABLE filament_sku_settings ADD CONSTRAINT uq_filament_sku "
                    "UNIQUE (material, subtype, brand, color_name)"
                )
            )
        return

    # ── SQLite ────────────────────────────────────────────────────────────────
    # ADD COLUMN above leaves the auto-created UNIQUE index covering only
    # (material, subtype, brand); SQLite can't ALTER a constraint, so rebuild the
    # table with the wider key. Detect the stale key by inspecting index columns
    # and skip once color_name is already part of it (fresh install / re-run).
    idx_rows = await conn.execute(text("PRAGMA index_list(filament_sku_settings)"))
    needs_uq_rebuild = False
    for idx in idx_rows.fetchall():
        # index_list columns: seq, name, unique, origin, partial. origin 'u' =
        # UNIQUE constraint, 'c' = CREATE INDEX, 'pk' = primary key.
        if idx[3] != "u":
            continue
        info = await conn.execute(text(f"PRAGMA index_info({idx[1]})"))
        cols = {row[2] for row in info.fetchall()}
        if "material" in cols and "color_name" not in cols:
            needs_uq_rebuild = True
            break

    if not needs_uq_rebuild:
        return

    async with conn.begin_nested():
        await conn.execute(text("DROP TABLE IF EXISTS filament_sku_settings_new"))
        # Column set copied verbatim from m059 (creating migration) + color_name.
        await conn.execute(
            text(
                """
                CREATE TABLE filament_sku_settings_new (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    material VARCHAR(50) NOT NULL,
                    subtype VARCHAR(50),
                    brand VARCHAR(100),
                    color_name VARCHAR(100),
                    lead_time_days INTEGER NOT NULL DEFAULT 0,
                    safety_margin_value INTEGER NOT NULL DEFAULT 14,
                    safety_margin_unit VARCHAR(10) NOT NULL DEFAULT 'days',
                    alerts_snoozed BOOLEAN NOT NULL DEFAULT 0,
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    CONSTRAINT uq_filament_sku UNIQUE (material, subtype, brand, color_name)
                )
                """
            )
        )
        await conn.execute(
            text(
                """
                INSERT INTO filament_sku_settings_new
                    (id, material, subtype, brand, color_name, lead_time_days, safety_margin_value,
                     safety_margin_unit, alerts_snoozed, created_at, updated_at)
                SELECT id, material, subtype, brand, color_name, lead_time_days, safety_margin_value,
                       safety_margin_unit, COALESCE(alerts_snoozed, 0), created_at, updated_at
                FROM filament_sku_settings
                """
            )
        )
        await conn.execute(text("DROP TABLE filament_sku_settings"))
        await conn.execute(text("ALTER TABLE filament_sku_settings_new RENAME TO filament_sku_settings"))
        # DROP TABLE dropped the updated_at trigger with the old table — restore
        # it (m059 emulates SQLite's missing ON UPDATE via this trigger).
        await conn.execute(
            text(
                """
                CREATE TRIGGER IF NOT EXISTS trg_filament_sku_settings_updated_at
                AFTER UPDATE ON filament_sku_settings
                FOR EACH ROW
                BEGIN
                    UPDATE filament_sku_settings
                    SET updated_at = CURRENT_TIMESTAMP
                    WHERE id = OLD.id;
                END
                """
            )
        )
