"""The sensor as the farm knows it: a name somebody chose, and where it stands.

Mirrors ``SmartPlug`` deliberately. What the radio knows about the same device
— its hardware name, its reporting parameters — lives in ``zigbee_devices`` and
is a different question with a different answer.
"""

from datetime import datetime

from pydantic import BaseModel, Field


class SmartSensorCreate(BaseModel):
    zigbee_ieee: str = Field(min_length=1, max_length=23)
    name: str = Field(min_length=1, max_length=100)
    # The same free string as ``Printer.location`` — a group name an operator
    # types, not a foreign key. Nothing consumes it yet; it is here so the
    # cycle that binds a sensor to the printers around it needs no migration.
    location: str | None = Field(default=None, max_length=100)


class SmartSensorUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    location: str | None = Field(default=None, max_length=100)


class SmartSensorOut(BaseModel):
    id: int
    name: str
    location: str | None
    zigbee_ieee: str
    created_at: datetime

    model_config = {"from_attributes": True}
