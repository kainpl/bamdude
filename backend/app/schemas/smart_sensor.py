"""The sensor as the farm knows it: a name somebody chose, and where it stands.

Mirrors ``SmartPlug`` deliberately. What the radio knows about the same device
— its hardware name, its reporting parameters — lives in ``zigbee_devices`` and
is a different question with a different answer.
"""

from datetime import datetime

from pydantic import BaseModel, Field, model_validator

from backend.app.schemas.printer_location import PrinterLocationOut, reject_legacy_key


class SmartSensorCreate(BaseModel):
    zigbee_ieee: str = Field(min_length=1, max_length=23)
    name: str = Field(min_length=1, max_length=100)
    # The place it stands in — the same entity a printer points at, so a sensor
    # and the printers around it can be asked about together.
    location_id: int | None = None
    # ⚠️ Or the printer it belongs TO — an enclosure probe, a door contact.
    # Exclusive with ``location_id``: the route clears whichever was not sent.
    # See ``SmartSensor`` for why they cannot both hold.
    printer_id: int | None = None

    @model_validator(mode="before")
    @classmethod
    def _no_legacy_location(cls, values):
        return reject_legacy_key(values, "location", "location_id")


class SmartSensorUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    location_id: int | None = None
    printer_id: int | None = None

    @model_validator(mode="before")
    @classmethod
    def _no_legacy_location(cls, values):
        return reject_legacy_key(values, "location", "location_id")


class SmartSensorOut(BaseModel):
    id: int
    name: str
    location_id: int | None = None
    location: PrinterLocationOut | None = None
    printer_id: int | None = None
    # The printer's name, so a sensor list can say what it is bound to without
    # a second request per row.
    printer_name: str | None = None
    zigbee_ieee: str
    created_at: datetime

    model_config = {"from_attributes": True}
