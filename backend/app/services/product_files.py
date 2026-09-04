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

from backend.app.core.config import settings

# The upload allowlists live HERE, in the service, and both the project routes
# and the product routes read them from here. They started out in
# ``routes/projects.py``; a service importing a route module was the wrong way
# round and made ``routes/projects.py`` unable to import this module back.
# Duplicating them instead was never an option: ``other`` IS the project
# attachments' allowlist and the picture set IS the project cover set (both
# answer "will a browser render this in an <img>"), so two copies would drift,
# and the drift would be silent.
ALLOWED_ATTACHMENT_EXTENSIONS = {
    # Images
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".webp",
    ".svg",
    ".bmp",
    ".ico",
    # Documents
    ".pdf",
    ".doc",
    ".docx",
    ".xls",
    ".xlsx",
    ".ppt",
    ".pptx",
    ".odt",
    ".ods",
    ".odp",
    ".txt",
    ".rtf",
    ".csv",
    ".md",
    # 3D/CAD files
    ".stl",
    ".obj",
    ".3mf",
    ".step",
    ".stp",
    ".iges",
    ".igs",
    ".f3d",
    ".scad",
    # Archives
    ".zip",
    ".rar",
    ".7z",
    ".tar",
    ".gz",
    # Code/scripts (for Klipper macros, scripts, etc.)
    ".py",
    ".sh",
    ".cfg",
    ".conf",
    ".gcode",
    ".ini",
    # Other common formats
    ".json",
    ".xml",
    ".yaml",
    ".yml",
}

# Cover / picture uploads accept only common web-renderable image types.
# Subset of ALLOWED_ATTACHMENT_EXTENSIONS minus .svg/.ico because those don't
# render well as a card thumbnail (#1155).
COVER_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
IMAGE_CONTENT_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
}

# One attachment's ceiling. Every path that moves these bytes — the upload
# route, the 3MF import, the card-file/card-download readers — buffers the WHOLE
# file in memory, so the cap is a memory bound before it is a policy: without it
# a 2 GB member inside a 3MF is a 2 GB allocation on a Raspberry Pi. 50 MB
# comfortably clears a photo, a slicer PDF and a spreadsheet.
MAX_ATTACHMENT_BYTES = 50 * 1024 * 1024

# A product export's ceiling — a different question from an attachment's, and a
# much larger answer: the archive carries every 3MF the product prints from, and
# a plated multi-material 3MF is routinely hundreds of megabytes. This is a
# TRANSPORT bound, not a policy — "what a farm will accept back in one request".
#
# The import route checks it twice: against the declared ``Content-Length``, a
# cheap refusal before any of our work but only the client's word; and then
# against the real size of the part FastAPI has ALREADY spooled to a
# ``SpooledTemporaryFile`` before the handler ran. Nothing is streamed here and
# nothing is buffered by us — by the time anything can refuse, the bytes are
# already on the server's disk. Bounding the ALLOCATION is a separate number,
# :data:`MAX_IMPORT_MEMBER_BYTES` below.
MAX_IMPORT_BYTES = 2 * 1024**3

# One MEMBER of that archive, and a much smaller number than the archive itself.
# ⚠️ This is a memory bound and the archive ceiling is not: ``import_zip`` hands
# a library member to ``store_library_upload``, whose signature is ``content:
# bytes`` — so whatever passes this gate is materialised whole. Teaching the
# library ingest to take a stream would remove the need for the cap and is a
# change to every upload path in the product (browser, Telegram, virtual
# printer), deliberately out of this pass. 200 MB clears a plated
# multi-material 3MF with room to spare.
MAX_IMPORT_MEMBER_BYTES = 200 * 1024 * 1024


def attachment_limit() -> int:
    """The ceiling, read at call time.

    ⚠️ Every caller asks through this and :func:`exceeds_attachment_limit`, never
    by importing the constant: a module that binds the name at import time keeps
    the value it saw, so the number a 413 REPORTS would drift from the number the
    gate ENFORCED the moment anything changed it. (That is also what lets a test
    lower the cap instead of building a 50 MB fixture — and the first version of
    this code shipped exactly that drift, caught by such a test.)
    """
    return MAX_ATTACHMENT_BYTES


def exceeds_attachment_limit(size: int | None) -> bool:
    """``True`` when a member is too big to move."""
    return size is not None and size > attachment_limit()


def import_limit() -> int:
    """The product-import ceiling, read at call time.

    Same rule as :func:`attachment_limit`, for the same reason: a module that
    binds the name at import time keeps the value it saw, so the number a 413
    REPORTS would drift from the number the gate ENFORCED. It is also what lets
    a test lower the ceiling instead of producing two gigabytes.
    """
    return MAX_IMPORT_BYTES


def import_member_limit() -> int:
    """The per-member ceiling of an import, read at call time. Same rule again."""
    return MAX_IMPORT_MEMBER_BYTES


# Order matters: it is the order the product page renders the sections in, and
# therefore the order ``GET /attachments`` answers in.
ATTACHMENT_CATEGORIES = ("pictures", "bom_docs", "assembly", "other")

# Where an attachment entry came from. A CLOSED set of three, and the frontend
# switches on it — so the constants live here and every writer uses them rather
# than spelling a string of its own. The three writers are the upload route
# (``manual``), ``product_card.fill_from_file`` (``3mf``) and
# ``product_card.import_zip`` (``import``); m158's converted project attachments
# were written as ``manual`` by a migration that is frozen and cannot import
# these.
#
# ⚠️ The wire type stays a plain ``str`` (``ProductAttachmentOut.source``), NOT a
# Literal. A hand-edited JSON column or a restored backup carrying something else
# must render the product page, not 500 it — the same tolerance ``size`` and
# ``uploaded_at`` are given two lines below it.
SOURCE_MANUAL = "manual"
SOURCE_3MF = "3mf"
SOURCE_IMPORT = "import"
SOURCE_VALUES = (SOURCE_MANUAL, SOURCE_3MF, SOURCE_IMPORT)

CATEGORY_EXTENSIONS: dict[str, set[str]] = {
    "pictures": set(COVER_EXTENSIONS),
    "bom_docs": {".xls", ".xlsx", ".pdf", ".csv"},
    # An assembly guide is a document or a set of steps shown as images.
    "assembly": {".pdf", ".md"} | COVER_EXTENSIONS,
    "other": set(ALLOWED_ATTACHMENT_EXTENSIONS),
}


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
