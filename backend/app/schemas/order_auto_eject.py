"""Operator-selected camera policy, captured with each new job."""

from pydantic import BaseModel, ConfigDict, Field, model_validator


class AutoEjectSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    difference_threshold: float = Field(default=1.0, ge=0.1, le=10)
    skip_check: bool = False


class AutoEjectSelection(BaseModel):
    """Request-only acknowledgement; never stored as an order or queue column."""

    auto_eject_settings: AutoEjectSettings | None = None
    auto_eject_skip_acknowledged: bool = Field(default=False, exclude=True)

    @model_validator(mode="after")
    def _acknowledged(self):
        if self.auto_eject_settings and self.auto_eject_settings.skip_check and not self.auto_eject_skip_acknowledged:
            raise ValueError("Skipping the plate check requires explicit acknowledgement")
        return self
