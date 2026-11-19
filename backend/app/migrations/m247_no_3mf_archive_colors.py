"""Colours of archives made without their 3MF become ``#RRGGBB`` (upstream 879acdbf1).

A print whose 3MF could not be fetched takes its filament colours from the
printer's AMS report, and until now they were stored as the printer sends them:
bare ``RRGGBBAA`` tokens joined with commas (``FF8800FF,00FF00FF``). Every other
writer of ``print_archives.filament_color`` stores ``#RRGGBB``, and the Archives
page uses each token as a CSS colour, so those archives showed empty chips. The
writer stores ``#RRGGBB`` from here on; this repairs the rows written before.

Only a value that is unambiguously that old form is touched: every token a bare
eight-digit hex colour, except that the LAST token of a value cut at the column's
50 characters may be the fragment of one (six such tokens need 53) and is dropped.
Anything else -- ``#``-prefixed colours of a 3MF or a loaded spool, six-digit
tokens, free text -- is left exactly as it is. A second run finds nothing: a
repaired value starts with ``#``.

The table and columns are named here: an ORM model would bring today's columns
into a step of the chain where they may not exist.
"""

import logging
import re

logger = logging.getLogger(__name__)

version = 247
name = "no_3mf_archive_colors"

_COLUMN_WIDTH = 50
_BARE_RGBA = re.compile(r"[0-9A-Fa-f]{8}")
_HEX_FRAGMENT = re.compile(r"[0-9A-Fa-f]{0,7}")


def normalised(value: str | None) -> str | None:
    """The repaired value, or None when ``value`` is not the old MQTT form."""
    if not value or "#" in value:
        return None
    tokens = value.split(",")
    whole = tokens
    if len(value) >= _COLUMN_WIDTH and len(tokens) > 1 and _HEX_FRAGMENT.fullmatch(tokens[-1]):
        whole = tokens[:-1]  # the column cut the last colour in half
    if not whole or not all(_BARE_RGBA.fullmatch(token) for token in whole):
        return None
    return ",".join(f"#{token[:6].upper()}" for token in whole)


async def upgrade(conn):
    """No schema change; the stored colours are repaired by seed."""


async def seed(session_factory):
    from sqlalchemy import Integer, String, column, select, table, update

    archives = table("print_archives", column("id", Integer), column("filament_color", String))

    async with session_factory() as db:
        rows = (
            await db.execute(
                select(archives.c.id, archives.c.filament_color).where(
                    archives.c.filament_color.is_not(None),
                    archives.c.filament_color != "",
                    archives.c.filament_color.not_like("#%"),
                )
            )
        ).all()
        repaired = 0
        for archive_id, stored in rows:
            value = normalised(stored)
            if value is None:
                continue
            await db.execute(update(archives).where(archives.c.id == archive_id).values(filament_color=value))
            repaired += 1
        await db.commit()
    if repaired:
        logger.info("m247: stored the colours of %d archive(s) made without their 3MF as #RRGGBB", repaired)
