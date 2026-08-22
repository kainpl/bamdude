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
    await add_column(conn, "label_templates", "description", "VARCHAR(300) NOT NULL DEFAULT ''")


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
    """
    from sqlalchemy import update

    from backend.app.models.label_template import LabelTemplate

    filled = 0
    async with session_factory() as session:
        for key, text in _BUILTIN_DESCRIPTIONS.items():
            result = await session.execute(
                update(LabelTemplate)
                .where(LabelTemplate.builtin_key == key, LabelTemplate.description == "")
                .values(description=text)
            )
            filled += result.rowcount or 0
        await session.commit()
    logger.info("m150: described %s built-in label design(s)", filled)
