from datetime import datetime

from pydantic import BaseModel, Field


class CustomerCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    contact: str | None = None
    notes: str | None = None


class CustomerUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    contact: str | None = None
    notes: str | None = None


class CustomerResponse(BaseModel):
    id: int
    name: str
    contact: str | None
    notes: str | None
    created_at: datetime
    updated_at: datetime
    figures: dict

    class Config:
        from_attributes = True
