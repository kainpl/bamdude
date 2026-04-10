"""Add type_code to maintenance_types for stable, locale-independent identification."""

version = 3
name = "maintenance_type_code"

# Mapping: English name (and known Ukrainian translations) → type_code
_NAME_TO_CODE = {
    # English
    "Clean Carbon Rods": "clean_carbon_rods",
    "Lubricate Steel Rods": "lubricate_steel_rods",
    "Clean Steel Rods": "clean_steel_rods",
    "Lubricate Linear Rails": "lubricate_linear_rails",
    "Clean Linear Rails": "clean_linear_rails",
    "Clean Nozzle/Hotend": "clean_nozzle",
    "Check Belt Tension": "check_belt_tension",
    "Clean Build Plate": "clean_build_plate",
    "Check PTFE Tube": "check_ptfe_tube",
    # Ukrainian
    "Очистити карбонові штанги": "clean_carbon_rods",
    "Змастити сталеві штанги": "lubricate_steel_rods",
    "Очистити сталеві штанги": "clean_steel_rods",
    "Змастити лінійні рейки": "lubricate_linear_rails",
    "Очистити лінійні рейки": "clean_linear_rails",
    "Очистити сопло/хотенд": "clean_nozzle",
    "Перевірити натяг ременів": "check_belt_tension",
    "Очистити робочий стіл": "clean_build_plate",
    "Перевірити PTFE трубку": "check_ptfe_tube",
}


async def upgrade(conn):
    from sqlalchemy import text

    from backend.app.migrations.helpers import add_column, column_exists

    if await column_exists(conn, "maintenance_types", "type_code"):
        return

    await add_column(conn, "maintenance_types", "type_code VARCHAR(50)")

    # Backfill system types by name
    for name, code in _NAME_TO_CODE.items():
        await conn.execute(
            text(
                "UPDATE maintenance_types SET type_code = :code "
                "WHERE name = :name AND type_code IS NULL AND is_system = 1"
            ),
            {"code": code, "name": name},
        )

    # Backfill custom types with custom_{id}
    await conn.execute(
        text(
            "UPDATE maintenance_types SET type_code = 'custom_' || CAST(id AS VARCHAR) "
            "WHERE type_code IS NULL AND is_system = 0"
        ),
    )

    # Backfill any remaining system types without a code (safety net)
    await conn.execute(
        text("UPDATE maintenance_types SET type_code = 'system_' || CAST(id AS VARCHAR) WHERE type_code IS NULL"),
    )
