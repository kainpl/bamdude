"""Name the standard AMS picker's Cream through the shared catalog (GH104).

The picker already sends FFFDD0. Without an exact catalog entry the display
falls back to the broad White family. This only seeds a missing name: no
printer, spool, profile or matching data changes. Existing catalog choices
for that hex, or an edited Generic Cream, remain authoritative.
"""

version = 214
name = "generic_cream_color"


async def upgrade(conn):
    """No schema change; the catalog row is added by seed."""


async def seed(session_factory):
    from sqlalchemy import and_, func, or_, select

    from backend.app.models.color_catalog import ColorCatalogEntry

    async with session_factory() as db:
        existing = await db.scalar(
            select(ColorCatalogEntry.id)
            .where(
                or_(
                    func.lower(func.substr(func.replace(ColorCatalogEntry.hex_color, "#", ""), 1, 6)) == "fffdd0",
                    and_(
                        func.lower(func.trim(ColorCatalogEntry.manufacturer)) == "generic",
                        func.lower(func.trim(ColorCatalogEntry.color_name)) == "cream",
                    ),
                )
            )
            .limit(1)
        )
        if existing is not None:
            return
        db.add(
            ColorCatalogEntry(
                manufacturer="Generic", color_name="Cream", hex_color="#FFFDD0", material="", is_default=True
            )
        )
        await db.commit()
