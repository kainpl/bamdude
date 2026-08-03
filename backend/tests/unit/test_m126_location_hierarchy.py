"""The uniqueness this migration relaxes is a TABLE CONSTRAINT, not an index.

Verified against a real database: ``UNIQUE (name)`` sits in the CREATE TABLE and
is backed by ``sqlite_autoindex_printer_locations_1``, which DROP INDEX cannot
remove. SQLite has had ALTER TABLE DROP COLUMN since 3.35 and this repository
uses it -- but there is no DROP CONSTRAINT at any version, so this one needs the
table rebuilt.
"""

import pytest
from sqlalchemy import text


@pytest.mark.asyncio
async def test_two_shelves_may_share_a_name_under_different_parents(db_session):
    from backend.app.models.printer_location import PrinterLocation

    workshop = PrinterLocation(name="Workshop", name_key="workshop")
    hall = PrinterLocation(name="Hall", name_key="hall")
    db_session.add_all([workshop, hall])
    await db_session.commit()
    await db_session.refresh(workshop)
    await db_session.refresh(hall)

    db_session.add(PrinterLocation(name="Shelf 1", name_key="shelf 1", parent_id=workshop.id))
    db_session.add(PrinterLocation(name="Shelf 1", name_key="shelf 1", parent_id=hall.id))
    await db_session.commit()

    rows = (
        await db_session.execute(text("SELECT COUNT(*) FROM printer_locations WHERE name_key = 'shelf 1'"))
    ).scalar()
    assert rows == 2, "the whole point of the tree: a shelf 1 in each workshop"


@pytest.mark.asyncio
async def test_the_table_no_longer_carries_a_global_unique_on_name(db_session):
    """A fresh install builds this table from the model, so the model is what
    has to stop declaring it. Left in place, the second 'Shelf 1' fails on a
    constraint nobody can drop without rebuilding the table."""
    rows = await db_session.execute(text("SELECT sql FROM sqlite_master WHERE name = 'printer_locations'"))
    ddl = rows.scalar() or ""

    assert "UNIQUE (name)" not in ddl
    assert "UNIQUE (name_key)" not in ddl
