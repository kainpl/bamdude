"""Label designs become data: templates, sheets, and the six old names seeded.

Two tables. ``label_templates`` holds a design — a size and a list of boxes;
``label_sheets`` holds a page of them. The six names ``POST /inventory/labels``
has always accepted split across both, because four of them were labels and two
were pages that had been filed with the labels.

The seed inserts those six under their old keys, so a caller that knows nothing
about templates notices nothing, plus two starters at sizes a thermal label
printer can actually take — three of the four built-ins are wider than a B1's
printhead, and without the starters somebody who plugs one in is looking at a
list with nothing in it that fits.

⚠️ **Labels will not come out pixel-identical.** The layout these replace
adjusts itself in ways a movable design cannot: it drops rows that would
collide and omits the colour block in monochrome. The seed reproduces its
geometry from the same formulas and keeps its fixed-size truncation, so the
difference is small — but it is real, and it is in the CHANGELOG.

The two permissions are seeded here as well. Administrators are not self-healed
at startup and our migrations are frozen, so a permission not seeded here is a
permission nobody ever has.

⚠️ **A design declares which printer it is drawn for** (``target``). One design
served both output paths at first: the PDF renderer drew a colour swatch, the
one-bit raster silently skipped it, and the same template printed acceptably
either way. That holds while colour is a small extra on an otherwise monochrome
label. It stops holding once colour is something you *design around* — a label
going out through a driver may be landing on an inkjet, where a filled shape in
the spool's colour is the point of it, and that is not a design which degrades
gracefully on a thermal head; it is one that arrives missing its subject. So the
editor refuses a colour element on a design declared thermal, rather than
accepting it and dropping it at print time. ``RasterCanvas.swatch`` still skips
rather than refuses, so anything drawn by hand keeps printing as it did.

⚠️ **``description`` is text, not a translation key**, and that is the trade. The
print dialog used to be six buttons hard-coded in the frontend, each with a
translated title and hint, while this table sat there being ignored. Making the
dialog read the catalogue means the row has to carry the sentence — and a
sentence somebody can edit cannot also be translated. The seeded ones are in
English.

Both of those arrived as their own migrations while this one was still
unreleased, and were folded back in here: a column added to a table nobody has
yet is that table's column.
"""

from __future__ import annotations

import logging

from sqlalchemy import select, text, update

from backend.app.core.db_dialect import is_sqlite
from backend.app.migrations.helpers import table_exists

logger = logging.getLogger(__name__)

version = 146
name = "label_templates"

NEW_PERMISSIONS = ["label_templates:read", "label_templates:write"]
#: Viewers may see which design a label used; changing one is not a read.
VIEWER_PERMISSIONS = ["label_templates:read"]


async def upgrade(conn):
    sqlite = is_sqlite()
    pk = "INTEGER PRIMARY KEY AUTOINCREMENT" if sqlite else "SERIAL PRIMARY KEY"
    ts = "DATETIME" if sqlite else "TIMESTAMP"
    json_type = "JSON" if sqlite else "JSONB"

    if not await table_exists(conn, "label_templates"):
        await conn.execute(
            text(
                f"""
                CREATE TABLE label_templates (
                    id {pk},
                    name VARCHAR(120) NOT NULL,
                    width_mm FLOAT NOT NULL,
                    height_mm FLOAT NOT NULL,
                    shape VARCHAR(16) NOT NULL DEFAULT 'rect',
                    -- Which kind of printer this design is drawn for. 'driver'
                    -- goes out as PDF and may use colour — it could be landing
                    -- on an inkjet or a laser; 'thermal' goes to a one-bit head
                    -- where a colour element is refused rather than dropped.
                    target VARCHAR(16) NOT NULL DEFAULT 'driver',
                    -- One line saying what the label is for, shown beside the
                    -- name wherever a design is offered.
                    description VARCHAR(300) NOT NULL DEFAULT '',
                    elements {json_type} NOT NULL,
                    builtin_key VARCHAR(64) UNIQUE,
                    created_by INTEGER,
                    created_at {ts},
                    updated_at {ts}
                )
                """
            )
        )
        logger.info("m146: created label_templates")

    if not await table_exists(conn, "label_sheets"):
        await conn.execute(
            text(
                f"""
                CREATE TABLE label_sheets (
                    id {pk},
                    name VARCHAR(120) NOT NULL,
                    builtin_key VARCHAR(64) UNIQUE,
                    page_size VARCHAR(16) NOT NULL DEFAULT 'A4',
                    cell_width_mm FLOAT NOT NULL,
                    cell_height_mm FLOAT NOT NULL,
                    cols INTEGER NOT NULL,
                    rows INTEGER NOT NULL,
                    margin_top_mm FLOAT NOT NULL DEFAULT 0,
                    margin_left_mm FLOAT NOT NULL DEFAULT 0,
                    gap_x_mm FLOAT NOT NULL DEFAULT 0,
                    gap_y_mm FLOAT NOT NULL DEFAULT 0,
                    created_at {ts}
                )
                """
            )
        )
        logger.info("m146: created label_sheets")


async def seed(session_factory):
    """Insert the built-ins, the sheets, the starters and the permissions.

    Column-explicit reads and Core updates, per the seed discipline: an
    entity-wide ``select(Model)`` would emit columns a later migration has not
    added yet and break an upgrade chain that runs this migration first.
    """
    from backend.app.models.group import Group
    from backend.app.models.label_template import LabelSheet, LabelTemplate
    from backend.app.services.label_seed import (
        BUILTIN_SHEETS,
        BUILTIN_TEMPLATES,
        STARTER_TEMPLATES,
    )

    async with session_factory() as db:
        existing_keys = {
            key
            for (key,) in (
                await db.execute(select(LabelTemplate.builtin_key).where(LabelTemplate.builtin_key.is_not(None)))
            ).all()
        }
        existing_names = {name_ for (name_,) in (await db.execute(select(LabelTemplate.name))).all()}

        added = 0
        for row in BUILTIN_TEMPLATES:
            if row["builtin_key"] in existing_keys:
                continue
            db.add(LabelTemplate(**row))
            added += 1

        for row in STARTER_TEMPLATES:
            # Starters have no key, so they are matched by name — enough to stop
            # a second run duplicating them, and they stay deletable.
            if row["name"] in existing_names:
                continue
            db.add(LabelTemplate(**row))
            added += 1

        sheet_keys = {
            key
            for (key,) in (
                await db.execute(select(LabelSheet.builtin_key).where(LabelSheet.builtin_key.is_not(None)))
            ).all()
        }
        for row in BUILTIN_SHEETS:
            if row["builtin_key"] in sheet_keys:
                continue
            db.add(LabelSheet(**row))
            added += 1

        result = await db.execute(select(Group.id, Group.name, Group.is_system, Group.permissions))
        dirty = 0
        for row in result.all():
            existing = set(row.permissions or [])
            if row.is_system and row.name in ("Administrators", "Operators"):
                wanted = NEW_PERMISSIONS
            elif row.is_system and row.name == "Viewers":
                wanted = VIEWER_PERMISSIONS
            else:
                continue

            to_add = [p for p in wanted if p not in existing]
            if not to_add:
                continue
            await db.execute(
                update(Group).where(Group.id == row.id).values(permissions=list(row.permissions or []) + to_add)
            )
            dirty += 1

        if added or dirty:
            await db.commit()
        if added:
            logger.info("m146: seeded %d label template(s) and sheet(s)", added)
        if dirty:
            logger.info("m146: seeded label-template permissions into %d group(s)", dirty)
