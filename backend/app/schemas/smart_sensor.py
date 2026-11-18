"""The sensor as the farm knows it: a name somebody chose, and where it stands.

Mirrors ``SmartPlug`` deliberately. What the radio knows about the same device
— its hardware name, its reporting parameters — lives in ``zigbee_devices`` and
is a different question with a different answer.
"""

from datetime import datetime

from pydantic import BaseModel, Field, model_validator

from backend.app.schemas.not_null import IgnoresNullForRequiredColumns
from backend.app.schemas.printer_location import PrinterLocationOut, reject_legacy_key


class SensorBindingIn(IgnoresNullForRequiredColumns):
    __orm_table__ = "smart_sensor_bindings"

    printer_id: int | None = None
    printer_location_id: int | None = None
    storage_location_id: int | None = None
    display_name: str | None = Field(default=None, max_length=100)
    visible: bool = True
    sort_order: int = 0
    notify_enabled: bool = False

    @model_validator(mode="after")
    def _one_target(self):
        if (
            sum(value is not None for value in (self.printer_id, self.printer_location_id, self.storage_location_id))
            != 1
        ):
            raise ValueError("A binding needs exactly one target.")
        return self


class SmartSensorCreate(BaseModel):
    zigbee_ieee: str = Field(min_length=1, max_length=23)
    name: str = Field(min_length=1, max_length=100)
    # The place it stands in — the same entity a printer points at, so a sensor
    # and the printers around it can be asked about together.
    location_id: int | None = None
    # Legacy 0/1-binding input. New clients use /sensors/{id}/bindings.
    printer_id: int | None = None
    initial_binding: SensorBindingIn | None = None

    @model_validator(mode="before")
    @classmethod
    def _no_legacy_location(cls, values):
        return reject_legacy_key(values, "location", "location_id")

    @model_validator(mode="after")
    def _single_initial_target(self):
        if self.initial_binding is not None and ({"location_id", "printer_id"} & self.model_fields_set):
            raise ValueError("initial_binding cannot be combined with legacy target fields")
        return self


class SmartSensorUpdate(IgnoresNullForRequiredColumns):
    __orm_table__ = "smart_sensors"

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
    bindings: list[dict] = Field(default_factory=list)
    zigbee_ieee: str
    created_at: datetime

    model_config = {"from_attributes": True}
