"""A label, its colour, and the two rules about how they are written.

Reads carry ``{id, name, color}``; writes carry ``tag_ids``. Same shape as
``schemas/printer_location.py`` so the frontend learns one contract.
"""

import re

from pydantic import BaseModel, Field, field_validator

_HEX_COLOUR = re.compile(r"^#[0-9a-f]{6}$")


class PrinterTagCreate(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    # ``#rrggbb`` or null. Lower-cased so two spellings of one colour compare equal.
    color: str | None = None

    @field_validator("name")
    @classmethod
    def _not_only_space(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("A tag needs a name.")
        return value

    @field_validator("color")
    @classmethod
    def _six_hex_digits(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip().lower()
        if not _HEX_COLOUR.match(value):
            raise ValueError("Colour must be a hex value like #ffcc00.")
        return value


class PrinterTagUpdate(PrinterTagCreate):
    """Name and colour. ``color`` absent keeps the current one; ``color: null`` clears it (the route reads ``model_fields_set``)."""


class PrinterTagOut(BaseModel):
    """What every response embeds: id, name, colour."""

    id: int
    name: str
    color: str | None = None

    model_config = {"from_attributes": True}


class PrinterTagListItem(PrinterTagOut):
    printer_count: int
    # Chosen as a staggered-start group in Settings: the manager badges it and
    # the delete route refuses while it is true (filled in by the stagger task).
    is_stagger_group: bool = False
