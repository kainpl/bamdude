"""Pydantic schemas for ``LibraryFileMakerworldMeta``.

Surfaces the MakerWorld source metadata captured per library file. Used
by ``/makerworld/recent-imports`` (response gained the optional ``meta``
field in m056) and by the new ``GET /makerworld/imports/{id}`` endpoint.

Covers are served as files via the dedicated cover/variant-cover routes
rather than as URLs in the JSON payload — keeps the JSON tiny and lets
the frontend benefit from the existing stream-token wrapping for image
URLs (CSP + cache).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel


class LibraryFileMakerworldMetaResponse(BaseModel):
    """Read-only view of a MakerWorld metadata row."""

    library_file_id: int

    title: str | None = None
    description: str | None = None
    author_name: str | None = None
    author_profile_url: str | None = None
    license: str | None = None
    original_design_id: int | None = None

    variant_title: str | None = None
    variant_description: str | None = None
    variant_url: str | None = None
    profile_id: int | None = None

    sliced_for: str | None = None
    compatible_models: list[str] | None = None
    needs_ams: bool | None = None
    material_count: int | None = None
    materials: list[dict[str, Any]] | None = None

    model_id_alphanumeric: str | None = None

    has_cover: bool = False
    has_variant_cover: bool = False

    imported_at: datetime | None = None

    model_config = {"from_attributes": True}
