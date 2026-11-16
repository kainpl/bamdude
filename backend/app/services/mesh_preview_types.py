"""Which library file types get which preview path — by ``LibraryFile.file_type``, never by a display name.

STL/OBJ render synchronously through the legacy batch route; STEP/STP only through the background thumbnail jobs
(spec §3, §5); a PDF's first page renders as STL/OBJ do, in the same preview child. Nothing here imports OCP or a
renderer.
"""

from __future__ import annotations

LEGACY_TYPES = ("stl", "obj")
CAD_TYPES = ("step", "stp")
PDF_TYPES = ("pdf",)
#: what one request renders while it waits: the batch button's own selection
INLINE_TYPES = LEGACY_TYPES + PDF_TYPES


def is_legacy(file_type: str | None) -> bool:
    return (file_type or "").lower() in LEGACY_TYPES


def is_pdf(file_type: str | None) -> bool:
    return (file_type or "").lower() in PDF_TYPES


def is_cad(file_type: str | None) -> bool:
    return (file_type or "").lower() in CAD_TYPES
