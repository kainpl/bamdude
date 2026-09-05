"""A label, and the one rule about how it is written.

Reads carry ``{id, name}``; writes carry ``tag_ids``. Same shape as
``schemas/printer_location.py`` so the frontend learns one contract.
"""

from pydantic import BaseModel, Field, field_validator


class PrinterTagCreate(BaseModel):
    name: str = Field(min_length=1, max_length=64)

    @field_validator("name")
    @classmethod
    def _not_only_space(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("A tag needs a name.")
        return value


class PrinterTagUpdate(PrinterTagCreate):
    """Rename only — a tag has nothing else to change."""


class PrinterTagOut(BaseModel):
    """What every response embeds. Nothing else travels with a tag."""

    id: int
    name: str

    model_config = {"from_attributes": True}


class PrinterTagListItem(PrinterTagOut):
    printer_count: int
    # Chosen as a staggered-start group in Settings: the manager badges it and
    # the delete route refuses while it is true (filled in by the stagger task).
    is_stagger_group: bool = False
