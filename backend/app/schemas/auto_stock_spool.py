"""A declared interchangeable stock group, independent of any physical spool id."""

from pydantic import BaseModel, Field, field_validator, model_validator


class StockSpoolGroup(BaseModel):
    material: str = Field(min_length=1, max_length=50)
    rgba: str = Field(pattern=r"^[0-9A-Fa-f]{8}$")
    brand: str = Field(default="", max_length=100)
    subtype: str = Field(default="", max_length=50)
    filament_family_id: str = Field(default="", max_length=50)
    label_weight: int = Field(gt=0)

    @field_validator("rgba")
    @classmethod
    def normalize_color(cls, value: str) -> str:
        return value.upper()


class AutoStockSpoolPolicy(BaseModel):
    enabled: bool = False
    group: StockSpoolGroup | None = None

    @model_validator(mode="after")
    def needs_group(self):
        if self.enabled and self.group is None:
            raise ValueError("An enabled auto-stock policy needs an inventory group")
        return self


class AvailableStockSpoolGroup(StockSpoolGroup):
    available_count: int
