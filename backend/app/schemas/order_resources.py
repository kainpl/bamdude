"""Order-local routing choices, independent of a product's sliced files."""

from typing import Literal

from pydantic import BaseModel, Field, StrictBool, StrictInt, field_validator

from backend.app.schemas.order_working_plan import WorkingEntry
from backend.app.utils.printer_models import normalize_model_name


class PrinterResources(BaseModel):
    models: list[str] | None = Field(default=None, min_length=1, max_length=64)
    printer_ids: list[StrictInt] | None = Field(default=None, min_length=1, max_length=256)
    include_paused_queues: StrictBool = False

    @field_validator("models")
    @classmethod
    def model_names(cls, value):
        if value is None:
            return None
        normalized = [normalize_model_name(m.strip()) or m.strip() for m in value]
        if any(not m or len(m) > 50 for m in normalized):
            raise ValueError("Choose a printer model")
        return sorted(set(normalized), key=str.casefold)

    @field_validator("printer_ids")
    @classmethod
    def identifiers(cls, value):
        if value is not None and any(i <= 0 for i in value):
            raise ValueError("Printer identifiers must be positive")
        return sorted(set(value)) if value is not None else None


class ResourceUpdate(BaseModel):
    expected_token: str = Field(min_length=64, max_length=64)
    resources: PrinterResources


class ResourceReplan(BaseModel):
    kind: Literal["resources"] = "resources"
    apply: bool = False
    proposal_token: str | None = Field(default=None, max_length=64)
    inputs_token: str | None = Field(default=None, max_length=64)
    expected_revision: int = Field(ge=0)
    proposal_entries: list[WorkingEntry] | None = Field(default=None, max_length=10000)
