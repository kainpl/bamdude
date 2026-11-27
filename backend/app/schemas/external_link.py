from datetime import datetime

from pydantic import BaseModel, Field, field_validator

from backend.app.schemas.not_null import IgnoresNullForRequiredColumns

VALID_NAV_GROUPS = ("operations", "workshop", "resources", "care", "system", "external")


class ExternalLinkBase(BaseModel):
    """Base schema for external links."""

    name: str = Field(..., min_length=1, max_length=50, description="Display name for the link")
    url: str = Field(..., min_length=1, max_length=500, description="External URL")
    icon: str = Field(default="link", max_length=50, description="Lucide icon name")
    open_in_new_tab: bool = False
    nav_group: str = Field(
        default="external",
        max_length=20,
        description="Sidebar group: one of operations/workshop/resources/care/system/external",
    )

    @field_validator("nav_group")
    @classmethod
    def validate_nav_group(cls, v: str) -> str:
        if v not in VALID_NAV_GROUPS:
            raise ValueError(f"nav_group must be one of {VALID_NAV_GROUPS}")
        return v

    @field_validator("url")
    @classmethod
    def validate_url(cls, v: str) -> str:
        """Validate URL format."""
        if not v.startswith(("http://", "https://")):
            raise ValueError("URL must start with http:// or https://")
        return v


class ExternalLinkCreate(ExternalLinkBase):
    """Schema for creating an external link."""

    pass


class ExternalLinkUpdate(IgnoresNullForRequiredColumns):
    """Schema for updating an external link (all fields optional)."""

    __orm_table__ = "external_links"

    name: str | None = Field(default=None, min_length=1, max_length=50)
    url: str | None = Field(default=None, min_length=1, max_length=500)
    icon: str | None = Field(default=None, max_length=50)
    open_in_new_tab: bool | None = None
    nav_group: str | None = Field(default=None, max_length=20)

    @field_validator("url")
    @classmethod
    def validate_url(cls, v: str | None) -> str | None:
        """Validate URL format."""
        if v is not None and not v.startswith(("http://", "https://")):
            raise ValueError("URL must start with http:// or https://")
        return v

    @field_validator("nav_group")
    @classmethod
    def validate_nav_group(cls, v: str | None) -> str | None:
        if v is not None and v not in VALID_NAV_GROUPS:
            raise ValueError(f"nav_group must be one of {VALID_NAV_GROUPS}")
        return v


class ExternalLinkResponse(ExternalLinkBase):
    """Response schema for external links."""

    id: int
    open_in_new_tab: bool
    custom_icon: str | None = None
    sort_order: int
    created_at: datetime
    updated_at: datetime
    # Reported by the app the link points to, while it keeps reporting; None otherwise.
    badge_count: int | None = None

    model_config = {"from_attributes": True}


class ExternalLinkBadgeSet(BaseModel):
    """An app's count for the sidebar links that point at it (upstream 29a81520d)."""

    url: str = Field(..., min_length=1, max_length=500, description="The app's address, as the link has it")
    count: int = Field(..., ge=0, le=1_000_000)

    @field_validator("url")
    @classmethod
    def validate_url(cls, v: str) -> str:
        if not v.startswith(("http://", "https://")):
            raise ValueError("URL must start with http:// or https://")
        return v


class ExternalLinkReorder(BaseModel):
    """Schema for reordering external links."""

    ids: list[int] = Field(..., description="List of link IDs in desired order")
