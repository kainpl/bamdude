"""Filesystem locations and typed-attachment rules owned by products.

A product's attachments are ONE JSON list on the row (``Product.attachments``)
with the files under ``<archive_dir>/products/<id>/attachments/`` — m158's
layout, and the entry shape it already wrote for the project templates it
converted (``category, filename, original_name, size, sort_order, source,
uploaded_at``).

⚠️ **The per-category allowlists below are the only thing standing between the
upload route and an executable landing in that directory** (spec §Risks). Widen
a row for a named reason; there is deliberately no "any extension" fallback and
no way to reach the writer without passing one of them.
"""

from pathlib import Path
from typing import Any

from fastapi import HTTPException

# The project attachment routes own three constants this module refuses to
# copy: ``other`` IS the project attachments' allowlist, and the picture set IS
# the project cover set — both answer "will a browser render this in an <img>".
# A second copy would drift, and the drift would be silent.
from backend.app.api.routes.projects import (
    ALLOWED_ATTACHMENT_EXTENSIONS,
    COVER_IMAGE_CONTENT_TYPES,
    COVER_IMAGE_EXTENSIONS,
)
from backend.app.core.config import settings

# Order matters: it is the order the product page renders the sections in, and
# therefore the order ``GET /attachments`` answers in.
ATTACHMENT_CATEGORIES = ("pictures", "bom_docs", "assembly", "other")

COVER_EXTENSIONS = set(COVER_IMAGE_EXTENSIONS)

CATEGORY_EXTENSIONS: dict[str, set[str]] = {
    "pictures": set(COVER_EXTENSIONS),
    "bom_docs": {".xls", ".xlsx", ".pdf", ".csv"},
    # An assembly guide is a document or a set of steps shown as images.
    "assembly": {".pdf", ".md"} | COVER_EXTENSIONS,
    "other": set(ALLOWED_ATTACHMENT_EXTENSIONS),
}

IMAGE_CONTENT_TYPES = dict(COVER_IMAGE_CONTENT_TYPES)


def product_attachments_dir(product_id: int) -> Path:
    """``<archive_dir>/products/<id>/attachments`` — the product-side twin of
    ``routes/projects.py::get_project_attachments_dir``."""
    return Path(settings.archive_dir) / "products" / str(product_id) / "attachments"


def safe_attachment_name(filename: str) -> str:
    """The projects routes' literal path-traversal guard, in one place.

    Over the wire ``{filename}`` is a plain path parameter, so a name carrying a
    separator never reaches a handler at all — Starlette 404s it in the router.
    This is defence in depth for a future ``:path`` converter or a second
    caller, and it runs BEFORE the path join, always.
    """
    if not filename or "/" in filename or "\\" in filename or ".." in filename:
        raise HTTPException(status_code=400, detail="Invalid filename")
    return filename


def image_media_type(filename: str) -> str:
    return IMAGE_CONTENT_TYPES.get(Path(filename).suffix.lower(), "application/octet-stream")


def _rows(product: Any) -> list[dict]:
    """Every well-formed entry of the JSON column. A row without a filename
    names no file and is skipped rather than crashing the product page."""
    return [a for a in (product.attachments or []) if isinstance(a, dict) and a.get("filename")]


def sorted_attachments(product: Any) -> list[dict]:
    """Category order first, then the gallery's own ``sort_order``."""
    rank = {category: i for i, category in enumerate(ATTACHMENT_CATEGORIES)}
    return sorted(
        (dict(a) for a in _rows(product)),
        key=lambda a: (rank.get(a.get("category"), len(rank)), a.get("sort_order") or 0, a.get("filename")),
    )


def category_entries(product: Any, category: str) -> list[dict]:
    """One category's entries in gallery order."""
    return [a for a in sorted_attachments(product) if a.get("category") == category]


def attachment_entry(product: Any, filename: str) -> dict | None:
    return next((dict(a) for a in _rows(product) if a.get("filename") == filename), None)


def effective_cover(product: Any) -> str | None:
    """The cover rule (spec §Decisions 4): the explicit column when set, else
    the first ``pictures`` attachment by ``sort_order``, else nothing.

    Deliberately does NOT touch the filesystem — it runs once per row in two
    list endpoints. A column pointing at a vanished file is healed by the route
    that tried to serve it.
    """
    if product.cover_image_filename:
        return product.cover_image_filename
    pictures = category_entries(product, "pictures")
    return pictures[0]["filename"] if pictures else None


def next_sort_order(product: Any, category: str) -> int:
    """Append position within a category — the orders are per category, so two
    categories both start at 0."""
    orders = [a.get("sort_order") or 0 for a in _rows(product) if a.get("category") == category]
    return max(orders) + 1 if orders else 0
