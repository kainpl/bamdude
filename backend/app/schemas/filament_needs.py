"""Wire shapes of the filament needs advisor (spec 2026-09-07, Slice B)."""

from pydantic import BaseModel


class NeedRowOut(BaseModel):
    material: str
    colour: str | None
    need_g: float
    have_g: float | None
    have_type_g: float | None
    short_g: float | None
    unknown_prints: int


class FarmRowOut(NeedRowOut):
    orders_count: int


class OrderNeedsOut(BaseModel):
    project_id: int
    rows: list[NeedRowOut]
    unknown_prints: int
    stock_unavailable: bool
    assumptions: list[str]


class FarmNeedsOut(BaseModel):
    rows: list[FarmRowOut]
    orders_count: int
    unknown_prints: int
    stock_unavailable: bool
    assumptions: list[str]
