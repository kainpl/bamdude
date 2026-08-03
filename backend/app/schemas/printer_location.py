"""A place, and the one rule about how it may be written.

Reads carry ``{id, name}``; writes carry ``location_id``. The old free-text key
is refused rather than ignored — see :func:`reject_legacy_key`.
"""

from pydantic import BaseModel, Field, field_validator


class PrinterLocationCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    # Any level may hold printers and sensors, so any level may be a parent.
    parent_id: int | None = None

    @field_validator("name")
    @classmethod
    def _not_only_space(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("A location needs a name.")
        return value


class PrinterLocationUpdate(BaseModel):
    """Both fields optional, and "not sent" is distinguished from "sent as null".

    Renaming must not move a location, and moving one to the top level must be
    sayable — which it is not if an omitted ``parent_id`` and an explicit null
    look the same. The route reads ``model_fields_set``.
    """

    name: str | None = Field(default=None, min_length=1, max_length=100)
    parent_id: int | None = None

    @field_validator("name")
    @classmethod
    def _not_only_space(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("A location needs a name.")
        return value


class PrinterLocationOut(BaseModel):
    """What every response embeds. Nothing else travels with a location."""

    id: int
    name: str
    parent_id: int | None = None
    # Derived on every read, never stored: renaming a parent then costs nothing
    # and cannot leave a stale copy behind.
    path: str = ""

    model_config = {"from_attributes": True}

    @classmethod
    def from_location(cls, location) -> "PrinterLocationOut | None":
        """None-safe shorthand. The path itself comes from the model."""
        return None if location is None else cls.model_validate(location)


class PrinterLocationListItem(PrinterLocationOut):
    depth: int = 1
    printer_count: int
    sensor_count: int
    queued_count: int


def reject_legacy_key(values, legacy: str, replacement: str):
    """Refuse the old free-text key instead of dropping it.

    Pydantic ignores unknown fields by default, so a client doing GET → edit →
    PUT would send the location object back and its change would silently do
    nothing — the exact failure this whole stage exists to remove, reintroduced
    at the last step. Refusing it says which field to use instead.
    """
    if isinstance(values, dict) and legacy in values:
        raise ValueError(f"'{legacy}' is no longer accepted — send '{replacement}' instead.")
    return values
