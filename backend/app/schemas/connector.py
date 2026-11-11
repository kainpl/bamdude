"""The slicer connector's request and response bodies (OrcaBamDude spec r2 §7, protocol 1).

Strict on purpose. Every model forbids unknown fields, so a newer client field is a refusal, never silently dropped.
A target is either one named printer -- its numeric id together with its immutable connector identity -- or the
farm's AutoQueue by model. The bodies carry no order, line, raw path, archive override or command field.
"""

from __future__ import annotations

import re
import uuid
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

PROTOCOL_VERSION = 1
MAX_ENTRIES = 64
MAX_COPIES_PER_ENTRY = 999
MAX_TOTAL_JOBS = 999
_SHA256 = re.compile(r"^[0-9a-f]{64}$")

CalibrationChoice = Literal["off", "auto", "on"]


def _canonical_uuid(value: str) -> str:
    parsed = uuid.UUID(str(value))
    if str(parsed) != str(value).lower():
        raise ValueError("must be a canonical UUID")
    return str(parsed)


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PrinterTarget(_Strict):
    kind: Literal["printer"]
    printer_id: int = Field(gt=0)
    printer_identity: str

    @field_validator("printer_identity")
    @classmethod
    def _identity(cls, value: str) -> str:
        return _canonical_uuid(value)


class AutoTarget(_Strict):
    kind: Literal["auto"]
    target_model: str = Field(min_length=1, max_length=50)
    target_location_id: int | None = Field(default=None, gt=0)


Target = Annotated[PrinterTarget | AutoTarget, Field(discriminator="kind")]


class FilamentChoice(_Strict):
    """``auto`` lets the server route; ``pinned`` names physical slots a person chose; ``external_only`` feeds from the
    external holder. An array alone is never a pin (``filament_policy.choices_policy``)."""

    mode: Literal["auto", "pinned", "external_only"] = "auto"
    feed_policy: Literal["auto", "ams_only"] = "auto"
    ams_mapping: list[int] | None = Field(default=None, max_length=64)

    @model_validator(mode="after")
    def _consistent(self):
        if self.mode == "pinned":
            if not self.ams_mapping or not any(slot >= 0 for slot in self.ams_mapping):
                raise ValueError("A pinned choice names at least one slot")
        elif self.ams_mapping is not None:
            raise ValueError("Only a pinned choice carries an ams_mapping")
        if self.mode != "auto" and self.feed_policy != "auto":
            raise ValueError("feed_policy belongs to an automatic choice")
        return self


class PrintOptions(_Strict):
    bed_levelling: CalibrationChoice = "auto"
    flow_cali: CalibrationChoice = "auto"
    nozzle_offset_cali: CalibrationChoice = "auto"
    timelapse: bool = False
    layer_inspect: bool = False


class SubmissionEntry(_Strict):
    entry_id: str
    plate_id: int = Field(ge=1)
    quantity: int = Field(default=1, ge=1, le=MAX_COPIES_PER_ENTRY)
    target: Target
    action: Literal["queue_manual", "queue_asap"]
    filament: FilamentChoice = FilamentChoice()
    options: PrintOptions = PrintOptions()

    @field_validator("entry_id")
    @classmethod
    def _entry_id(cls, value: str) -> str:
        return _canonical_uuid(value)

    @model_validator(mode="after")
    def _pins_only_for_a_printer(self):
        if self.target.kind == "auto" and self.filament.mode == "pinned":
            raise ValueError("The AutoQueue chooses the printer; it takes no physical slots")
        if self.target.kind == "auto" and self.options.nozzle_offset_cali != "auto":
            # The AutoQueue keeps no per-printer nozzle offset choice; promotion uses the printer's profile.
            raise ValueError("The AutoQueue cannot keep a nozzle offset calibration choice")
        return self


class SubmissionCreate(_Strict):
    protocol_version: Literal[1]
    operation_id: str
    server_id: str = Field(min_length=1, max_length=36)
    storage_epoch: str = Field(min_length=1, max_length=36)
    library_file_id: int = Field(gt=0)
    source_sha256: str
    entries: list[SubmissionEntry] = Field(min_length=1, max_length=MAX_ENTRIES)

    @field_validator("operation_id")
    @classmethod
    def _operation_id(cls, value: str) -> str:
        return _canonical_uuid(value)

    @field_validator("source_sha256")
    @classmethod
    def _digest(cls, value: str) -> str:
        if not _SHA256.match(value):
            raise ValueError("must be 64 lowercase hex characters")
        return value

    @model_validator(mode="after")
    def _bounded(self):
        ids = [entry.entry_id for entry in self.entries]
        if len(set(ids)) != len(ids):
            raise ValueError("entry_id values must be unique")
        if sum(entry.quantity for entry in self.entries) > MAX_TOTAL_JOBS:
            raise ValueError(f"At most {MAX_TOTAL_JOBS} jobs in one operation")
        return self


class EntryError(BaseModel):
    error: str
    message: str
    retryable: bool


class EntryResult(BaseModel):
    entry_id: str
    state: Literal["pending", "processing", "published", "retryable_failure", "rejected", "cancelled"]
    queue_item_ids: list[int] = []
    auto_item_ids: list[int] = []
    error: EntryError | None = None


class SubmissionResult(BaseModel):
    operation_id: str
    state: Literal["processing", "complete", "partial", "cancelled", "rejected"]
    entries: list[EntryResult]
    replayed: bool = False
