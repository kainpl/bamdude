"""Wire shapes for orders from files (spec 2026-09-06, Slice C)."""

from typing import Annotated, Literal

from pydantic import BaseModel, Field


class PartsPreviewRequest(BaseModel):
    file_ids: list[int] = Field(min_length=1, max_length=50)


class PreviewPlateOut(BaseModel):
    plate_index: int
    sliced: bool
    print_time_seconds: int | None


class PreviewFileOut(BaseModel):
    id: int
    filename: str
    sliced_for_model: str | None
    plates: list[PreviewPlateOut]


class PreviewYieldOut(BaseModel):
    library_file_id: int
    plate_index: int
    count: int


class PreviewPartOut(BaseModel):
    name_key: str
    name: str
    yields: list[PreviewYieldOut]


class PreviewCatalogPartOut(BaseModel):
    id: int
    name: str
    qty_per_unit: int


class PreviewCatalogProductOut(BaseModel):
    id: int
    name: str
    parts: list[PreviewCatalogPartOut]


class PartsPreviewResponse(BaseModel):
    files: list[PreviewFileOut]
    parts: list[PreviewPartOut]
    catalog_product: PreviewCatalogProductOut | None = None


class JobOrderIn(BaseModel):
    kind: Literal["job"]
    name: str = Field(min_length=1, max_length=255)
    file_ids: list[int] = Field(min_length=1, max_length=50)
    targets: dict[str, int]


class CatalogOrderIn(BaseModel):
    kind: Literal["catalog"]
    name: str = Field(min_length=1, max_length=255)
    product_id: int
    file_ids: list[int] = Field(min_length=1, max_length=50)
    quantity: int = Field(ge=1, le=9999)


class PlateCopiesIn(BaseModel):
    plate_index: int = Field(ge=0)
    copies: int = Field(ge=1, le=999)


class PlatesOrderIn(BaseModel):
    kind: Literal["plates"]
    library_file_id: int
    plates: list[PlateCopiesIn] = Field(min_length=1, max_length=64)
    name: str | None = Field(default=None, max_length=255)


OrderFromFilesRequest = Annotated[JobOrderIn | CatalogOrderIn | PlatesOrderIn, Field(discriminator="kind")]
