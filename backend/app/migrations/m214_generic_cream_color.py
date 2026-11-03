"""Name missing standard AMS picker shades through the shared catalog (GH104).

No picker RGB, printer, spool or matching data changes. Preserve any existing
name for a hex, or an edited Generic shade. Values and SQL columns are frozen
here: importing today's ORM table would insert future Python-default columns
that may not exist at this step of an upgrade.
"""

version = 214
name = "generic_cream_color"

_SHADES = (
    ("Cream", "FFFDD0"),
    ("Ivory", "FFFFF0"),
    ("Charcoal", "36454F"),
    ("Tan", "D2B48C"),
    ("Chocolate", "D2691E"),
    ("Olive", "808000"),
    ("Maroon", "800000"),
    ("Salmon", "FA8072"),
    ("Navy", "000080"),
    ("Teal", "008080"),
    ("Turquoise", "40E0D0"),
    ("Lime", "32CD32"),
    ("Silver", "C0C0C0"),
    ("Magenta", "FF00FF"),
    ("Violet", "EE82EE"),
    ("Indigo", "4B0082"),
    ("Slate", "708090"),
    ("Coral", "FF7F50"),
    ("Green", "00AA00"),
    ("Cyan", "00FFFF"),
    ("Brown", "8B4513"),
)


async def upgrade(conn):
    """No schema change; the missing catalog rows are added by seed."""


async def seed(session_factory):
    from sqlalchemy import Boolean, String, and_, column, func, insert, or_, select, table

    colors = table(
        "color_catalog",
        column("manufacturer", String),
        column("color_name", String),
        column("hex_color", String),
        column("material", String),
        column("is_default", Boolean),
    )
    async with session_factory() as db:
        for color_name, hex_color in _SHADES:
            existing = await db.scalar(
                select(colors.c.hex_color)
                .where(
                    or_(
                        func.lower(func.substr(func.replace(colors.c.hex_color, "#", ""), 1, 6)) == hex_color.lower(),
                        and_(
                            func.lower(func.trim(colors.c.manufacturer)) == "generic",
                            func.lower(func.trim(colors.c.color_name)) == color_name.lower(),
                        ),
                    )
                )
                .limit(1)
            )
            if existing is not None:
                continue
            await db.execute(
                insert(colors).values(
                    manufacturer="Generic",
                    color_name=color_name,
                    hex_color="#" + hex_color,
                    material="",
                    is_default=True,
                )
            )
        await db.commit()
