"""Stock forecasting + shopping list tables, notification toggles, permission backfill.

Adapted from upstream Bambuddy ``37c9d5f2`` (#1184). The upstream PR inlines
~200 lines of DDL into ``core/database.py`` — we collapse that into a single
migration here, dropping the legacy-rebuild branches we never had (upstream's
``safety_margin_days`` → ``safety_margin_value`` rename was a beta-cycle fix
that doesn't apply to us; we ship the final shape from day one).

What this migration does:

1. Create ``filament_sku_settings`` — per-SKU reorder configuration
   (material/subtype/brand → lead-time, safety-margin, snooze).
2. Create ``filament_shopping_list`` — queue of SKUs marked for purchase.
3. Add two ``notification_providers`` columns: ``on_stock_reorder_alert``,
   ``on_stock_break_alert``. These are scaffold — no backend trigger fires
   them today; ``ForecastPanel.tsx`` renders alerts client-side. The
   toggles are wired into the provider model + telegram per-chat authority
   so a future scheduled aggregator can flip them live without a schema
   change.
4. Backfill permissions: every group with ``inventory:read`` gets
   ``inventory:forecast_read``; every group with ``inventory:update`` gets
   ``inventory:forecast_write``. Default groups updated in
   ``DEFAULT_GROUPS`` apply only to fresh installs; this seed handles
   existing custom groups.

Forecasting itself runs entirely in the frontend — backend just persists
operator preferences and serves raw ``spool_usage_history`` via the
existing ``/inventory/usage-history`` endpoint.
"""

from sqlalchemy import text

from backend.app.core.db_dialect import is_postgres
from backend.app.migrations.helpers import add_column, table_exists

version = 59
name = "stock_forecasting"


async def upgrade(conn):
    # ── filament_sku_settings ──────────────────────────────────────────────
    if not await table_exists(conn, "filament_sku_settings"):
        if is_postgres():
            await conn.execute(
                text("""
                    CREATE TABLE filament_sku_settings (
                        id SERIAL PRIMARY KEY,
                        material VARCHAR(50) NOT NULL,
                        subtype VARCHAR(50),
                        brand VARCHAR(100),
                        lead_time_days INTEGER NOT NULL DEFAULT 0,
                        safety_margin_value INTEGER NOT NULL DEFAULT 14,
                        safety_margin_unit VARCHAR(10) NOT NULL DEFAULT 'days',
                        alerts_snoozed BOOLEAN NOT NULL DEFAULT FALSE,
                        created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        CONSTRAINT uq_filament_sku UNIQUE (material, subtype, brand)
                    )
                """)
            )
        else:
            await conn.execute(
                text("""
                    CREATE TABLE filament_sku_settings (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        material VARCHAR(50) NOT NULL,
                        subtype VARCHAR(50),
                        brand VARCHAR(100),
                        lead_time_days INTEGER NOT NULL DEFAULT 0,
                        safety_margin_value INTEGER NOT NULL DEFAULT 14,
                        safety_margin_unit VARCHAR(10) NOT NULL DEFAULT 'days',
                        alerts_snoozed BOOLEAN NOT NULL DEFAULT 0,
                        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        CONSTRAINT uq_filament_sku UNIQUE (material, subtype, brand)
                    )
                """)
            )
            # SQLite has no ON UPDATE — emulate via trigger so ORM .refresh()
            # after a row update sees the bumped updated_at.
            await conn.execute(
                text("""
                    CREATE TRIGGER IF NOT EXISTS trg_filament_sku_settings_updated_at
                    AFTER UPDATE ON filament_sku_settings
                    FOR EACH ROW
                    BEGIN
                        UPDATE filament_sku_settings
                        SET updated_at = CURRENT_TIMESTAMP
                        WHERE id = OLD.id;
                    END
                """)
            )

    # ── filament_shopping_list ────────────────────────────────────────────
    if not await table_exists(conn, "filament_shopping_list"):
        if is_postgres():
            await conn.execute(
                text("""
                    CREATE TABLE filament_shopping_list (
                        id SERIAL PRIMARY KEY,
                        material VARCHAR(50) NOT NULL,
                        subtype VARCHAR(50),
                        brand VARCHAR(100),
                        quantity_spools INTEGER NOT NULL DEFAULT 1,
                        note VARCHAR(500),
                        status VARCHAR(20) NOT NULL DEFAULT 'pending',
                        purchased_at TIMESTAMP,
                        added_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                    )
                """)
            )
        else:
            await conn.execute(
                text("""
                    CREATE TABLE filament_shopping_list (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        material VARCHAR(50) NOT NULL,
                        subtype VARCHAR(50),
                        brand VARCHAR(100),
                        quantity_spools INTEGER NOT NULL DEFAULT 1,
                        note VARCHAR(500),
                        status VARCHAR(20) NOT NULL DEFAULT 'pending',
                        purchased_at DATETIME,
                        added_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                    )
                """)
            )

    # ── notification_providers: stock toggles (scaffold) ──────────────────
    await add_column(conn, "notification_providers", "on_stock_reorder_alert BOOLEAN NOT NULL DEFAULT 0")
    await add_column(conn, "notification_providers", "on_stock_break_alert BOOLEAN NOT NULL DEFAULT 0")


async def seed(session_factory):
    """Backfill ``inventory:forecast_*`` permissions onto existing groups.

    Mirrors the entitlement already encoded in ``DEFAULT_GROUPS`` for fresh
    installs: viewers + readers get ``forecast_read``; anyone with
    ``inventory:update`` gets ``forecast_write``. Idempotent — uses set
    semantics on the JSON permissions list.
    """
    import json

    from sqlalchemy import select, update

    from backend.app.core.permissions import Permission
    from backend.app.models.group import Group

    READ_KEY = Permission.INVENTORY_FORECAST_READ.value
    WRITE_KEY = Permission.INVENTORY_FORECAST_WRITE.value

    # Column-explicit read + Core update — see feedback_migration_seed_columns.
    async with session_factory() as db:
        result = await db.execute(select(Group.id, Group.permissions))
        for row in result.all():
            perms = row.permissions or []
            if isinstance(perms, str):
                try:
                    perms = json.loads(perms)
                except (json.JSONDecodeError, TypeError):
                    perms = []
            perms = list(perms)
            changed = False
            if "inventory:read" in perms and READ_KEY not in perms:
                perms.append(READ_KEY)
                changed = True
            if "inventory:update" in perms and WRITE_KEY not in perms:
                perms.append(WRITE_KEY)
                changed = True
            if changed:
                await db.execute(update(Group).where(Group.id == row.id).values(permissions=perms))
        await db.commit()
