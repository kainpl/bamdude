"""Pydantic schemas for macros."""

import json
from datetime import datetime

from pydantic import BaseModel, Field, field_validator


class MacroResponse(BaseModel):
    """Response schema for a macro."""

    id: int
    name: str
    description: str | None = None
    printer_models: list[str]
    swap_mode_only: bool
    swap_profile: str | None = None
    event: str
    action_type: str = "gcode"
    mqtt_action: str | None = None
    delay_seconds: int = 0
    gcode: str
    is_custom: bool
    enabled: bool
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}

    @field_validator("printer_models", mode="before")
    @classmethod
    def parse_printer_models(cls, v: str | list) -> list[str]:
        if isinstance(v, str):
            try:
                return json.loads(v)
            except (json.JSONDecodeError, TypeError):
                return [v] if v else ["*"]
        return v


class MacroCreate(BaseModel):
    """Create schema for a custom macro."""

    name: str = Field(max_length=100)
    description: str | None = None
    printer_models: list[str] = Field(default=["*"])
    swap_mode_only: bool = False
    swap_profile: str | None = Field(default=None, max_length=50)
    event: str = Field(max_length=50)
    action_type: str = Field(default="gcode", max_length=20)
    mqtt_action: str | None = Field(default=None, max_length=50)
    delay_seconds: int = Field(default=0, ge=0, le=3600)
    gcode: str = ""
    enabled: bool = True


class MacroUpdate(BaseModel):
    """Update schema for a macro."""

    name: str | None = Field(default=None, max_length=100)
    description: str | None = None
    printer_models: list[str] | None = None
    swap_mode_only: bool | None = None
    # ``""`` from the client maps to ``None`` at route-level; Pydantic keeps
    # the distinction between "omitted" (do-not-change) and "null" (clear).
    swap_profile: str | None = Field(default=None, max_length=50)
    event: str | None = Field(default=None, max_length=50)
    action_type: str | None = Field(default=None, max_length=20)
    mqtt_action: str | None = Field(default=None, max_length=50)
    delay_seconds: int | None = Field(default=None, ge=0, le=3600)
    gcode: str | None = None
    enabled: bool | None = None
