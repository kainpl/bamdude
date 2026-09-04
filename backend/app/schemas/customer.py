from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator


def _clean_name(value: str) -> str:
    """Trim, and refuse a name that is nothing but whitespace.

    ``Field(min_length=1)`` already rejects ``""``; it cannot see that ``"   "``
    is the same thing. Both create and update run this, so the two paths cannot
    disagree about what a stored name looks like.
    """
    trimmed = value.strip()
    if not trimmed:
        raise ValueError("name cannot be blank")
    return trimmed


class CustomerCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    contact: str | None = None
    notes: str | None = None

    @field_validator("name")
    @classmethod
    def _name_is_clean(cls, value: str) -> str:
        return _clean_name(value)


class CustomerUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    contact: str | None = None
    notes: str | None = None

    @field_validator("name")
    @classmethod
    def _name_is_never_null_and_is_clean(cls, value: str | None) -> str | None:
        """An omitted ``name`` leaves it alone; an explicit ``null`` is a 422.

        PATCH clears a field by sending ``null`` — but ``customers.name`` is NOT
        NULL, so clearing it would surface as an IntegrityError from the flush,
        i.e. a 500 on malformed input. A validator answers 422 instead. It does
        not fire when the field is absent: pydantic does not validate defaults.
        """
        if value is None:
            raise ValueError("name cannot be null")
        return _clean_name(value)


class CustomerListFigures(BaseModel):
    """What the LIST endpoint promises: counts and a price sum, no archive work.

    ``extra="allow"`` on purpose — a status this build has never heard of is
    counted under its own key rather than dropped, which is the rule both
    figure builders follow. Declaring the four known statuses still documents
    what a client may rely on.
    """

    model_config = ConfigDict(extra="allow")

    projects: int
    active: int
    completed: int
    cancelled: int
    total_price: float


class CustomerFigures(CustomerListFigures):
    """The DETAIL endpoint's superset — the three keys that cost archive work.

    ``CustomerPage`` tells the two apart with ``'ordered' in figures``, so the
    list model must never grow these fields "for symmetry": an absent key means
    "not asked", a zero would mean "measured, and it is nothing".
    """

    ordered: int
    printed: int
    total_cost: float


class CustomerResponse(BaseModel):
    id: int
    name: str
    contact: str | None
    notes: str | None
    created_at: datetime
    updated_at: datetime
    # The detail model first: it is the more specific of the two, and a union
    # resolves left to right.
    figures: CustomerFigures | CustomerListFigures

    class Config:
        from_attributes = True
