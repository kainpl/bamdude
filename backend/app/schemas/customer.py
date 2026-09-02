from datetime import datetime

from pydantic import BaseModel, Field, field_validator


class CustomerCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    contact: str | None = None
    notes: str | None = None


class CustomerUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    contact: str | None = None
    notes: str | None = None

    @field_validator("name")
    @classmethod
    def _name_is_never_null(cls, value: str | None) -> str | None:
        """An omitted ``name`` leaves it alone; an explicit ``null`` is a 422.

        PATCH clears a field by sending ``null`` — but ``customers.name`` is NOT
        NULL, so clearing it would surface as an IntegrityError from the flush,
        i.e. a 500 on malformed input. A validator answers 422 instead. It does
        not fire when the field is absent: pydantic does not validate defaults.
        """
        if value is None:
            raise ValueError("name cannot be null")
        return value


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
