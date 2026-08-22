"""A label design carries its own one-line description.

The print dialog used to offer six buttons hard-coded in the frontend, each
with a translated title and hint, while the catalogue those six were supposed
to represent sat in the database being ignored. Adding a design did not add a
button; renaming one changed nothing anybody saw.

Turning the dialog into a list of what actually exists needs the row to carry
the two things the button had: a name — it already did — and a sentence saying
what the label is for.

⚠️ **The text stops being translated, and that is the trade.** A description a
person can edit cannot also be a translation key, and the same choice made the
seeded designs editable rather than frozen. The four built-in descriptions are
backfilled here in English; anybody who wants them in their own words now has
somewhere to type them, which was the point.
"""

from __future__ import annotations

import logging

from backend.app.migrations.helpers import add_column, column_exists

logger = logging.getLogger(__name__)

version = 150
name = "label_template_description"


async def upgrade(conn):
    if await column_exists(conn, "label_templates", "description"):
        return
    await add_column(conn, "label_templates", "description VARCHAR(300) NOT NULL DEFAULT ''")


#: What each seeded design is for, in the words the dialog used to show.
_BUILTIN_DESCRIPTIONS = {
    "ams_holder_74x33": (
        "Single label per page; matches the printable label STL from MakerWorld "
        "model 752566 (AMS Filament Label Holder)."
    ),
    "ams_holder_75x55": (
        "Single label per page; fits the cardstock-insert variant of the AMS "
        "Filament Label Holder. Roomy enough for swatch, brand and QR."
    ),
    "box_40x30": "Single label per page; common DK/Brother roll size, good for filament bags and storage bins.",
    "box_62x29": "Single label per page; sized for Brother PT/QL and Dymo small labels.",
}


async def seed(session_factory):
    """Fill in the descriptions the four seeded designs used to show.

    ⚠️ Matched on ``builtin_key``, and only where the description is still
    empty. A row somebody has already described is a row somebody has already
    decided about.

    ⚠️ Written as SQL over named columns rather than ``update(LabelTemplate)``:
    the mapped class carries whatever the current code declares, so an
    entity-wide ORM statement in a seed can reference a column a later migration
    has not added yet — and that dies mid-chain on an install, not in a test.
    """
    from sqlalchemy import text

    filled = 0
    async with session_factory() as session:
        for key, description in _BUILTIN_DESCRIPTIONS.items():
            result = await session.execute(
                text(
                    "UPDATE label_templates SET description = :description "
                    "WHERE builtin_key = :key AND (description IS NULL OR description = '')"
                ),
                {"description": description, "key": key},
            )
            filled += result.rowcount or 0
        await session.commit()
    logger.info("m150: described %s built-in label design(s)", filled)
