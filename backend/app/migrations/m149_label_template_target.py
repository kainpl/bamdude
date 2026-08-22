"""A label design says which kind of printer it is drawn for.

Until now one design served both output paths: the PDF renderer drew a colour
swatch, the one-bit raster silently skipped it, and the same template printed
acceptably either way. That worked while colour was a small extra on an
otherwise monochrome label.

It stops working once colour is something you *design around*. A label going
out through the OS driver may well be landing on an inkjet or a laser, where a
filled shape in the spool's colour is the point of the label — and a design
built around one is not a design that degrades gracefully on a thermal head;
it is a design that arrives missing its subject.

So the target is declared, and the editor refuses colour elements on a design
declared thermal, rather than accepting them and dropping them at print time.

⚠️ **This deliberately reverses the earlier "one template, two backends"
decision, and the reversal is the point** — see the comment on
``RasterCanvas.swatch``, which still skips rather than refuses so that designs
predating this migration keep printing exactly as they did.

Existing rows are assigned by where they came from: the four built-ins have
always gone out as PDF, and the two starters were seeded for the device.
"""

from __future__ import annotations

import logging

from backend.app.migrations.helpers import add_column, column_exists

logger = logging.getLogger(__name__)

version = 149
name = "label_template_target"


async def upgrade(conn):
    if await column_exists(conn, "label_templates", "target"):
        return
    # 'driver' — printed through the OS print driver, may be in colour.
    # 'thermal' — sent to a one-bit label printer, colour cannot survive.
    #
    # ⚠️ Defaults to 'driver', which is the answer for every row that existed
    # before this column: a design nobody marked is a design that was printing
    # as PDF, and calling it thermal would strip elements from labels that
    # print them today.
    await add_column(conn, "label_templates", "target", "VARCHAR(16) NOT NULL DEFAULT 'driver'")


async def seed(session_factory):
    """Mark the two device starters as thermal.

    ⚠️ Matched on ``builtin_key IS NULL`` **and** the seeded names, not on name
    alone: a user's own design called "Label printer 50 × 30" would otherwise be
    quietly switched to thermal and lose whatever colour it carries. The
    starters are the only rows this installer created without a builtin key.
    """
    from sqlalchemy import update

    from backend.app.models.label_template import LabelTemplate

    starter_names = ("Label printer 40 × 20", "Label printer 50 × 30")

    async with session_factory() as session:
        result = await session.execute(
            update(LabelTemplate)
            .where(
                LabelTemplate.builtin_key.is_(None),
                LabelTemplate.name.in_(starter_names),
            )
            .values(target="thermal")
        )
        await session.commit()
        logger.info("m149: marked %s starter template(s) as thermal", result.rowcount)
